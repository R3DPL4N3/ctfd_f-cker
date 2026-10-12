"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.agents.solver import Solver
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.flag_submit import FlagOutcome, submit_flag_candidate
from backend.message_bus import ChallengeMessageBus
from backend.models import DEFAULT_MODELS, provider_from_spec
from backend.prompts import ChallengeMeta
from backend.scenario import trace_scenario
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
)
from backend.writeup import write_solve_writeup

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class ChallengeSwarm:
    """Parallel solvers racing on one challenge."""

    challenge_dir: str
    meta: ChallengeMeta
    ctfd: CTFdClient
    cost_tracker: CostTracker
    settings: Settings
    model_specs: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    no_submit: bool = False
    coordinator_inbox: asyncio.Queue | None = None
    campaign: Any = None

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(default_factory=dict)  # per-model last submit timestamp
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)
    scenario_mode: bool = False
    scenario_session: Any = None
    scenario_registry: Any = None
    on_flag_accepted: Any = None
    on_stage_solved: Any = None
    routing_lock: asyncio.Lock | None = None
    scenario_writeup_dir: str = "scenario-writeups"
    _winner_spec: str | None = None
    _continuation_decided: bool = False
    _wait_generation: int = 0
    _continuation_event: asyncio.Event = field(default_factory=asyncio.Event)
    _continuation_payload: tuple[ChallengeMeta, str] | None = None
    _continue_barrier: asyncio.Barrier | None = None
    _stage_interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    _stage_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _solver_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    _stage_run_tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def _create_solver(self, model_spec: str):
        """Create the right solver type based on provider.

        - claude-sdk/* → ClaudeSolver (Claude Agent SDK, subscription-first)
        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - bedrock/*, azure/*, zen/*, google/* → Pydantic AI Solver (API, explicit only)
        """
        provider = provider_from_spec(model_spec)

        def _submit_fn(flag): return self.try_submit_flag(flag, model_spec)
        _notify = self._make_notify_fn(model_spec)

        if provider == "claude-sdk":
            from backend.agents.claude_solver import ClaudeSolver
            return ClaudeSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                campaign=self.campaign,
            )

        if provider == "codex":
            from backend.agents.codex_solver import CodexSolver
            return CodexSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                campaign=self.campaign,
            )

        return self._create_pydantic_solver(model_spec)

    def _make_notify_fn(self, model_spec: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""
        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(
                    f"[{self.meta.name}/{model_spec}] {message}"
                )
        return _notify

    def _create_pydantic_solver(self, model_spec: str, sandbox=None, owns_sandbox: bool | None = None) -> Solver:
        """Create a Pydantic AI solver for explicitly requested API-backed specs."""
        solver = Solver(
            model_spec=model_spec,
            challenge_dir=self.challenge_dir,
            meta=self.meta,
            ctfd=self.ctfd,
            cost_tracker=self.cost_tracker,
            settings=self.settings,
            cancel_event=self.cancel_event,
            sandbox=sandbox,
            owns_sandbox=owns_sandbox,
        )
        solver.deps.message_bus = self.message_bus
        solver.deps.model_spec = model_spec
        solver.deps.no_submit = self.no_submit
        solver.deps.submit_fn = lambda flag: self.try_submit_flag(flag, model_spec)
        solver.deps.notify_coordinator = self._make_notify_fn(model_spec)
        solver.deps.campaign = self.campaign
        return solver

    def _gather_sibling_insights(self, exclude_model: str) -> str:
        parts: list[str] = []
        for model, finding in self.findings.items():
            if model != exclude_model and finding:
                parts.append(f"[{model}]: {finding}")
        return "\n\n".join(parts) if parts else "No sibling insights available yet."

    # Escalating cooldowns after incorrect submissions (per model)
    SUBMISSION_COOLDOWNS = [0, 30, 120, 300, 600]  # 0s, 30s, 2min, 5min, 10min

    async def try_submit_flag(self, flag: str, model_spec: str) -> tuple[str, bool]:
        """Cooldown-gated flag submission through the control plane.

        Only an explicit CTFd rejection is cached. Transport and auth failures stay retryable.

        Locking: this method holds `routing_lock` for the CTFd call and, on
        ACCEPTED, the scenario refresh/continuation. That serializes unrelated
        challenge submits while a flag is in flight.

        TODO: split into a per-swarm `_flag_lock` around the CTFd attempt, then
        take `routing_lock` only after ACCEPTED. Left as-is for the GOAD
        benchmark — correctness over submit concurrency.
        """
        acquired = False
        try:
            if self.routing_lock is not None:
                await self.routing_lock.acquire()
                acquired = True
            return await self._try_submit_flag_inner(flag, model_spec)
        finally:
            if acquired and self.routing_lock is not None:
                self.routing_lock.release()

    async def _try_submit_flag_inner(self, flag: str, model_spec: str) -> tuple[str, bool]:
        accepted = False
        normalized = flag.strip()
        async with self._flag_lock:
            if self.confirmed_flag:
                return f"ALREADY SOLVED — flag already confirmed: {self.confirmed_flag}", True

            session_id = self.scenario_session.id if self.scenario_session else None
            if normalized in self._submitted_flags:
                return "INCORRECT — already tried this exact flag.", False

            wrong_count = self._submit_count.get(model_spec, 0)
            cooldown_idx = min(wrong_count, len(self.SUBMISSION_COOLDOWNS) - 1)
            cooldown = self.SUBMISSION_COOLDOWNS[cooldown_idx]
            if cooldown > 0:
                last_time = self._last_submit_time.get(model_spec, 0)
                elapsed = time.monotonic() - last_time
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN — wait {remaining}s before submitting again. "
                        f"You have {wrong_count} incorrect submissions. "
                        "Use this time to do deeper analysis and verify your flag.",
                        False,
                    )

            trace_scenario(
                "candidate_flag",
                self.scenario_session,
                challenge=self.meta.name,
                session_id=session_id,
                solver=model_spec,
            )
            submission = await submit_flag_candidate(self.ctfd, self.meta.name, normalized)
            if submission.outcome == FlagOutcome.ACCEPTED:
                self.confirmed_flag = normalized
                self._submitted_flags.add(normalized)
                self._winner_spec = model_spec
                solver = self.solvers.get(model_spec)
                if self.scenario_session is not None and solver is not None:
                    self.scenario_session.solver = solver
                    self._bind_solver_memory(solver)
                    try:
                        self.scenario_session.sandbox_id = solver.sandbox.container_id
                    except Exception:
                        logger.debug("Sandbox id unavailable", exc_info=True)
                if self.scenario_session is None:
                    trace_scenario(
                        "flag_accepted",
                        challenge=self.meta.name,
                        solver=model_spec,
                    )
                accepted = True
            elif submission.outcome == FlagOutcome.REJECTED:
                self._submitted_flags.add(normalized)
                self._submit_count[model_spec] = wrong_count + 1
                self._last_submit_time[model_spec] = time.monotonic()
                trace_scenario(
                    "flag_rejected",
                    self.scenario_session,
                    challenge=self.meta.name,
                    session_id=session_id,
                    solver=model_spec,
                )
            else:
                trace_scenario(
                    "flag_submission_error",
                    self.scenario_session,
                    challenge=self.meta.name,
                    session_id=session_id,
                    solver=model_spec,
                    outcome=submission.outcome.value,
                )
            display = submission.display

        if accepted and self.campaign is not None:
            self.campaign.store.mark_stage(self.meta.name, "accepted")
        if accepted and self.on_flag_accepted is not None:
            try:
                await self.on_flag_accepted(self.meta.name, normalized)
            except Exception:
                logger.exception("[%s] Flag-accepted hook failed", self.meta.name)
        return display, accepted

    def _live_solver_count(self) -> int:
        return sum(1 for task in self._solver_tasks.values() if not task.done())

    def _arm_continuation(self, payload: tuple[ChallengeMeta, str] | None) -> None:
        parties = max(self._live_solver_count(), 1)
        self._continue_barrier = asyncio.Barrier(parties)
        self._continuation_payload = payload
        self._continuation_decided = True
        self._continuation_event.set()

    def offer_continuation(self, meta: ChallengeMeta, challenge_dir: str) -> bool:
        """Broadcast the next stage to every retained solver. Safe before waiters exist."""
        if not self.scenario_mode or self._continuation_decided:
            return False
        self._arm_continuation((meta, challenge_dir))
        return True

    def finish_scenario_wait(self) -> None:
        """Unblock solvers waiting for an unlock. Does not destroy sandboxes."""
        if self._continuation_decided:
            return
        self._arm_continuation(None)

    async def wait_for_continuation(self) -> tuple[ChallengeMeta, str] | None:
        timeout_task: asyncio.Task | None = None
        if not self._continuation_decided:
            timeout_task = asyncio.create_task(
                self._expire_wait(self._wait_generation, self._unlock_wait_seconds()),
                name=f"scenario-wait-{self.meta.name}",
            )
        try:
            await self._continuation_event.wait()
            payload = self._continuation_payload
            if self.cancel_event.is_set():
                return payload
            barrier = self._continue_barrier
            if barrier is not None:
                try:
                    await barrier.wait()
                except asyncio.BrokenBarrierError:
                    pass
            return payload
        finally:
            if timeout_task is not None:
                timeout_task.cancel()
                try:
                    await timeout_task
                except asyncio.CancelledError:
                    pass

    async def _expire_wait(self, generation: int, delay: float) -> None:
        await asyncio.sleep(delay)
        if generation != self._wait_generation or self._continuation_decided:
            return
        trace_scenario(
            "scenario_timeout",
            self.scenario_session,
            challenge=self.meta.name,
            session_id=self.scenario_session.id if self.scenario_session else None,
            wait_seconds=delay,
        )
        logger.info("Scenario unlock wait expired for %s", self.meta.name)
        self.finish_scenario_wait()

    def _unlock_wait_seconds(self) -> float:
        return float(getattr(self.settings, "scenario_unlock_wait_seconds", 120))

    def _bind_solver_memory(self, solver: Any) -> None:
        session = self.scenario_session
        if session is None:
            return
        bind = getattr(solver, "bind_scenario", None)
        if callable(bind):
            bind(session.id)
        path = getattr(solver, "memory_path", None)
        if path:
            session.metadata["memory_path"] = path

    def _prepare_next_stage(self, meta: ChallengeMeta, challenge_dir: str) -> None:
        """Reset current-stage runtime state. Scenario history stays in the session."""
        self.meta = meta
        self.challenge_dir = challenge_dir
        self.winner = None
        self._winner_spec = None
        self.confirmed_flag = None
        self._submitted_flags.clear()
        self._submit_count.clear()
        self._last_submit_time.clear()
        self._continuation_decided = False
        self._continuation_payload = None
        self._continue_barrier = None
        self._continuation_event = asyncio.Event()
        self._stage_interrupt = asyncio.Event()
        self._wait_generation += 1

    async def _cancel_sibling_runs(self, winner_spec: str) -> None:
        """Cancel losing solvers' current challenge runs. Keep instances and sandboxes."""
        self._stage_interrupt.set()
        pending: list[asyncio.Task] = []
        session_id = self.scenario_session.id if self.scenario_session else "scenario"
        for spec, solver in self.solvers.items():
            if spec == winner_spec:
                continue
            cancel = getattr(solver, "cancel_current_run", None)
            if callable(cancel):
                cancel()
            task = self._stage_run_tasks.get(spec)
            if task is not None and not task.done() and not callable(cancel):
                task.cancel()
            if task is not None and not task.done():
                pending.append(task)
            logger.info(
                "[%s] Current stage run cancelled; solver retained for %s",
                getattr(solver, "agent_name", spec),
                session_id,
            )
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _finalize_scenario(self) -> None:
        session = self.scenario_session
        if session is None or self.scenario_registry is None or session.state == "stopped":
            return
        self.scenario_registry.stop_session(session.id, output_dir=self.scenario_writeup_dir)

    def _solver_closed(self, solver: Any) -> bool:
        return bool(getattr(solver, "_closed", False))

    def _scenario_id(self) -> str:
        session = self.scenario_session
        return session.id if session is not None else "scenario"

    async def _close_solver(self, solver: Any) -> None:
        closer = getattr(solver, "close", None)
        if callable(closer):
            await closer()
            return
        await solver.stop()

    async def _run_solver(self, model_spec: str) -> SolverResult | None:
        solver = self.solvers.get(model_spec)
        already_started = solver is not None and not self._solver_closed(solver)
        if solver is None or self._solver_closed(solver):
            solver = self._create_solver(model_spec)
            already_started = False
        self.solvers[model_spec] = solver
        if self.scenario_session is not None:
            self.scenario_session.metadata["solvers"] = self.solvers
        current = asyncio.current_task()
        if current is not None:
            self._solver_tasks[model_spec] = current

        try:
            result, final_solver = await self._run_solver_loop(
                solver, model_spec, already_started=already_started,
            )
            solver = final_solver
            return result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.meta.name}/{model_spec}] Fatal: {e}", exc_info=True)
            return None
        finally:
            await self._close_solver(solver)

    async def _await_stage_run(self, solver, model_spec: str) -> SolverResult:
        """Run one challenge attempt as a cancellable task, without closing the solver."""
        task = asyncio.create_task(
            solver.run_until_done_or_gave_up(),
            name=f"run-{self.meta.name}-{model_spec}",
        )
        self._stage_run_tasks[model_spec] = task
        try:
            return await task
        except asyncio.CancelledError:
            if not task.done():
                cancel = getattr(solver, "cancel_current_run", None)
                if callable(cancel):
                    cancel()
                else:
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            if self._stage_run_tasks.get(model_spec) is task:
                self._stage_run_tasks.pop(model_spec, None)

    async def _continue_solver(self, solver, model_spec: str) -> bool:
        """Adopt the next stage on this solver. Returns False when the scenario is over."""
        continuation = await self.wait_for_continuation()
        if continuation is None:
            return False
        next_meta, next_dir = continuation
        async with self._stage_lock:
            if self.meta.name != next_meta.name or self.challenge_dir != next_dir:
                self._prepare_next_stage(next_meta, next_dir)
        if not hasattr(solver, "continue_with_challenge"):
            logger.warning("[%s] Solver cannot continue an existing session", model_spec)
            return False
        await solver.continue_with_challenge(next_meta, next_dir)
        sandbox_id = None
        try:
            sandbox_id = solver.sandbox.container_id
        except Exception:
            logger.debug("Sandbox id unavailable after continuation", exc_info=True)
        logger.info("[%s] Continuing solver in %s", solver.agent_name, self._scenario_id())
        if sandbox_id:
            logger.info("[%s] Reused sandbox %s", solver.agent_name, sandbox_id)
        else:
            logger.info("[%s] Reused sandbox", solver.agent_name)
        return True

    async def _wait_for_stage_or_cancel(self, timeout: float) -> str:
        """Return 'timeout', 'shutdown', or 'stage'."""
        shutdown = asyncio.create_task(self.cancel_event.wait())
        stage = asyncio.create_task(self._stage_interrupt.wait())
        try:
            done, pending = await asyncio.wait(
                {shutdown, stage},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                return "timeout"
            if self.cancel_event.is_set():
                return "shutdown"
            return "stage"
        except asyncio.CancelledError:
            shutdown.cancel()
            stage.cancel()
            await asyncio.gather(shutdown, stage, return_exceptions=True)
            raise

    async def _run_solver_loop(
        self,
        solver,
        model_spec: str,
        *,
        already_started: bool = False,
    ) -> tuple[SolverResult, SolverProtocol]:
        """Inner loop: start → run → bump → run → ... Solver instance survives sibling cancel."""
        bump_count = 0
        consecutive_errors = 0
        result = SolverResult(
            flag=None, status=CANCELLED, findings_summary="",
            step_count=0, cost_usd=0.0, log_path="",
        )
        if not already_started:
            await solver.start()
        self._bind_solver_memory(solver)

        while not self.cancel_event.is_set():
            result = await self._await_stage_run(solver, model_spec)

            # Only broadcast useful findings — skip errors and broken solvers
            if (result.status not in (ERROR, QUOTA_ERROR)
                    and not (result.step_count == 0 and result.cost_usd == 0)
                    and result.findings_summary
                    and not result.findings_summary.startswith(("Error:", "Turn failed:"))):
                self.findings[model_spec] = result.findings_summary
                await self.message_bus.post(model_spec, result.findings_summary[:500])

            if result.status == FLAG_FOUND:
                stage_name = self.meta.name
                try:
                    writeup_path = write_solve_writeup(
                        self.challenge_dir,
                        self.meta,
                        result,
                        model_spec,
                    )
                    result.writeup_path = str(writeup_path)
                except Exception as e:
                    logger.warning(f"[{self.meta.name}] Writeup generation failed: {e}")
                self.winner = result
                if self.on_stage_solved is not None:
                    try:
                        self.on_stage_solved(stage_name, result)
                    except Exception:
                        logger.exception("[%s] Stage result hook failed", stage_name)
                msg = f"[{stage_name}] Flag found by {model_spec}: {result.flag}"
                if result.writeup_path:
                    msg += f" (writeup: {result.writeup_path})"
                logger.info(msg)

                if not self.scenario_mode or self.no_submit:
                    self.cancel_event.set()
                    return result, solver

                await self._cancel_sibling_runs(model_spec)
                if not await self._continue_solver(solver, model_spec):
                    return result, solver
                continue

            if result.status == CANCELLED:
                if self.scenario_mode and not self.cancel_event.is_set():
                    logger.info(
                        "[%s] Current stage run cancelled; solver retained for %s",
                        getattr(solver, "agent_name", model_spec),
                        self._scenario_id(),
                    )
                    if await self._continue_solver(solver, model_spec):
                        continue
                break

            if result.status == QUOTA_ERROR:
                logger.warning(
                    f"[{self.meta.name}/{model_spec}] Quota exhausted - no API fallback configured"
                )
                break

            if result.status in (GAVE_UP, ERROR):
                if result.step_count == 0 and result.cost_usd == 0:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Broken (0 steps, $0) — not bumping"
                    )
                    break

                # Track consecutive errors — stop after 3 in a row
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logger.warning(
                            f"[{self.meta.name}/{model_spec}] {consecutive_errors} consecutive errors — giving up"
                        )
                        break
                else:
                    consecutive_errors = 0

                bump_count += 1
                interrupt = await self._wait_for_stage_or_cancel(min(bump_count * 30, 300))
                if interrupt == "shutdown":
                    break
                if interrupt == "stage":
                    logger.info(
                        "[%s] Current stage run cancelled; solver retained for %s",
                        getattr(solver, "agent_name", model_spec),
                        self._scenario_id(),
                    )
                    if await self._continue_solver(solver, model_spec):
                        continue
                    break
                insights = self._gather_sibling_insights(model_spec)
                solver.bump(insights)
                logger.info(
                    f"[{self.meta.name}/{model_spec}] Bumped ({bump_count}), resuming"
                )
                continue

        return result, solver

    async def ensure_solver_tasks(self) -> int:
        """Start a missing outer solver task for each expected model. Idempotent."""
        if self.cancel_event.is_set():
            return 0
        started = 0
        for spec in self.model_specs:
            task = self._solver_tasks.get(spec)
            if task is not None and not task.done():
                continue
            self._solver_tasks[spec] = asyncio.create_task(
                self._run_solver(spec),
                name=f"solver-{spec}",
            )
            started += 1
        return started

    async def run(self) -> SolverResult | None:
        """Run all solvers in parallel. Returns the winner's result or None."""
        try:
            return await self._run_all()
        finally:
            pending = [task for task in self._solver_tasks.values() if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._finalize_scenario()

    async def _run_all(self) -> SolverResult | None:
        await self.ensure_solver_tasks()

        try:
            while True:
                live = [task for task in self._solver_tasks.values() if not task.done()]
                if not live:
                    break
                done, pending = await asyncio.wait(live, return_when=asyncio.FIRST_COMPLETED)

                for task in done:
                    try:
                        result = task.result()
                    except (Exception, asyncio.CancelledError):
                        continue
                    if result and result.status == FLAG_FOUND:
                        winner_task = self._solver_tasks.get(self._winner_spec or "")
                        if (
                            self.scenario_mode
                            and winner_task is not None
                            and winner_task is not task
                            and not winner_task.done()
                        ):
                            continue
                        self.cancel_event.set()
                        self.finish_scenario_wait()
                        waiting = [
                            other
                            for other in self._solver_tasks.values()
                            if other is not task and not other.done()
                        ]
                        for other in waiting:
                            other.cancel()
                        if waiting:
                            await asyncio.gather(*waiting, return_exceptions=True)
                        return result

            self.cancel_event.set()
            return self.winner
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.meta.name}] Swarm error: {e}", exc_info=True)
            self.cancel_event.set()
            waiting = [task for task in self._solver_tasks.values() if not task.done()]
            for task in waiting:
                task.cancel()
            if waiting:
                await asyncio.gather(*waiting, return_exceptions=True)
            return None

    def kill(self) -> None:
        """Cancel all agents for this scenario. Does not skip sandbox close on the outer tasks."""
        self.cancel_event.set()
        self.finish_scenario_wait()
        for solver in self.solvers.values():
            cancel = getattr(solver, "cancel_current_run", None)
            if callable(cancel):
                cancel()
        for task in self._stage_run_tasks.values():
            if not task.done():
                task.cancel()

    def get_status(self) -> dict:
        """Get per-agent progress and findings."""
        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "agents": {
                spec: {
                    "findings": self.findings.get(spec, ""),
                    "status": "running" if spec in self.solvers and not self.cancel_event.is_set()
                             else ("won" if self.winner and self.winner.flag else "finished"),
                }
                for spec in self.model_specs
            },
        }
