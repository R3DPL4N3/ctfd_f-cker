"""Shared coordinator tool logic — called by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from backend.deps import CoordinatorDeps
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND

logger = logging.getLogger(__name__)


async def do_fetch_challenges(deps: CoordinatorDeps) -> str:
    challenges = await deps.ctfd.fetch_all_challenges()
    solved = await deps.ctfd.fetch_solved_names()
    result = [
        {
            "name": ch.get("name", "?"),
            "category": ch.get("category", "?"),
            "value": ch.get("value", 0),
            "solves": ch.get("solves", 0),
            "status": "SOLVED" if ch.get("name") in solved else "unsolved",
            "description": (ch.get("description") or "")[:200],
        }
        for ch in challenges
    ]
    return json.dumps(result, indent=2)


async def do_get_solve_status(deps: CoordinatorDeps) -> str:
    solved = await deps.ctfd.fetch_solved_names()
    swarm_status = {name: swarm.get_status() for name, swarm in deps.swarms.items()}
    return json.dumps({"solved": sorted(solved), "active_swarms": swarm_status}, indent=2)


async def do_spawn_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    async with deps.routing_lock:
        result = await spawn_swarm_unlocked(deps, challenge_name)
        if result.startswith("Swarm spawned"):
            deps.handled_challenges.add(challenge_name)
        return result


async def ensure_challenge_materials(deps: CoordinatorDeps, challenge_name: str) -> tuple[str, ChallengeMeta]:
    """Pull a challenge into the local tree once, including id and prerequisites."""
    if challenge_name not in deps.challenge_dirs:
        challenges = await deps.ctfd.fetch_all_challenges()
        challenge_data = next((c for c in challenges if c.get("name") == challenge_name), None)
        if not challenge_data:
            raise RuntimeError(f"Challenge '{challenge_name}' not found on CTFd")
        output_dir = str(Path(deps.challenges_root))
        challenge_dir = await deps.ctfd.pull_challenge(challenge_data, output_dir)
        deps.challenge_dirs[challenge_name] = challenge_dir
        deps.challenge_metas[challenge_name] = ChallengeMeta.from_yaml(Path(challenge_dir) / "metadata.yml")

    meta = deps.challenge_metas[challenge_name]
    if meta.id is None:
        try:
            meta.id = await deps.ctfd.get_challenge_id(challenge_name)
        except Exception:
            logger.debug("Could not resolve id for %s", challenge_name, exc_info=True)
    return deps.challenge_dirs[challenge_name], meta


def _retire_finished_swarms(deps: CoordinatorDeps) -> None:
    """Drop swarms whose tasks are done. A live scenario session is not finished."""
    finished: list[str] = []
    for name, swarm in deps.swarms.items():
        session = getattr(swarm, "scenario_session", None)
        if session is not None and session.state != "stopped":
            continue
        task = deps.swarm_tasks.get(name)
        if swarm.cancel_event.is_set() or (task is not None and task.done()):
            finished.append(name)
    for name in finished:
        deps.swarms.pop(name, None)
        deps.swarm_tasks.pop(name, None)


async def spawn_swarm_unlocked(deps: CoordinatorDeps, challenge_name: str) -> str:
    """Create a swarm. Caller must hold deps.routing_lock."""
    _retire_finished_swarms(deps)

    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"

    active_count = len(deps.swarms)
    if active_count >= deps.max_concurrent_challenges:
        return (
            f"At capacity ({active_count}/{deps.max_concurrent_challenges} challenges running). "
            "Wait for one to finish."
        )

    try:
        _challenge_dir, meta = await ensure_challenge_materials(deps, challenge_name)
    except RuntimeError as exc:
        return str(exc)

    from backend.agents.swarm import ChallengeSwarm

    swarm = ChallengeSwarm(
        challenge_dir=_challenge_dir,
        meta=meta,
        ctfd=deps.ctfd,
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        model_specs=deps.model_specs,
        no_submit=deps.no_submit,
        coordinator_inbox=deps.coordinator_inbox,
    )
    if not deps.no_submit:
        _attach_scenario(deps, swarm, meta)
    deps.swarms[challenge_name] = swarm

    async def _run_and_cleanup() -> None:
        result = await swarm.run()
        # Stages are recorded as they are accepted. This covers a swarm with no scenario session.
        if result and result.status == FLAG_FOUND and challenge_name not in deps.results:
            deps.results[challenge_name] = {
                "flag": result.flag,
                "submit": "DRY RUN" if deps.no_submit else "confirmed by solver",
                "writeup": result.writeup_path,
            }

    task = asyncio.create_task(_run_and_cleanup(), name=f"swarm-{challenge_name}")
    deps.swarm_tasks[challenge_name] = task
    return f"Swarm spawned for {challenge_name} with {len(deps.model_specs)} models"


def _attach_scenario(deps: CoordinatorDeps, swarm: Any, meta: ChallengeMeta) -> None:
    """Bind a new swarm to a scenario session without assuming a Codex solver."""
    from backend.agents.scenario_orchestrator import on_flag_accepted, record_stage_result

    session = deps.scenario_registry.create_session(
        solver=None,
        current_challenge_id=meta.id,
        current_challenge_name=meta.name,
        category=meta.category,
        metadata={"tags": list(meta.tags), "swarm": swarm},
    )
    session.metadata["challenge_dirs"] = {meta.name: swarm.challenge_dir}
    swarm.scenario_mode = True
    swarm.scenario_session = session
    swarm.scenario_registry = deps.scenario_registry
    swarm.routing_lock = deps.routing_lock
    swarm.scenario_writeup_dir = str(Path(deps.challenges_root).parent / "scenario-writeups")
    swarm.on_flag_accepted = lambda challenge_name, flag: on_flag_accepted(
        deps, swarm, challenge_name, flag
    )
    swarm.on_stage_solved = lambda challenge_name, result: record_stage_result(
        deps, swarm, challenge_name, result
    )


async def do_check_swarm_status(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    return json.dumps(swarm.get_status(), indent=2)


def find_swarm_for_challenge(deps: CoordinatorDeps, challenge_name: str) -> Any | None:
    """Find the living swarm for a challenge, including a rebound scenario swarm."""
    swarm = deps.swarms.get(challenge_name)
    if swarm is not None:
        return swarm
    session = deps.scenario_registry.find_session_by_challenge(challenge_name=challenge_name)
    if session is None:
        return None
    return session.metadata.get("swarm")


async def do_submit_flag(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    """Submit only through an active swarm. Never call CTFd independently.

    The coordinator LLM no longer exposes this tool. The function remains so any
    leftover caller still hits the scenario lifecycle instead of bypassing it.
    """
    swarm = find_swarm_for_challenge(deps, challenge_name)
    if swarm is None:
        return (
            "REFUSED — flags are submitted by solvers through the control plane. "
            f"No active swarm for {challenge_name}."
        )
    if deps.no_submit:
        return f'DRY RUN — would submit "{flag.strip()}" for {challenge_name}'
    spec = swarm._winner_spec or (swarm.model_specs[0] if swarm.model_specs else "coordinator")
    display, _confirmed = await swarm.try_submit_flag(flag, spec)
    return display


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    swarm.kill()
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
    solver.bump(insights)
    return f"Bumped {model_spec} on {challenge_name}"


async def do_read_solver_trace(deps: CoordinatorDeps, challenge_name: str, model_spec: str, last_n: int = 20) -> str:
    """Read the last N trace events from a solver's JSONL log."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec}"
    trace_path = getattr(solver, "tracer", None)
    if not trace_path:
        return "No tracer on solver"
    path = trace_path.path if hasattr(trace_path, "path") else str(trace_path)
    try:
        lines = Path(path).read_text().strip().split("\n")
        recent = lines[-last_n:]
        summary = []
        for line in recent:
            try:
                d = json.loads(line)
                t = d.get("type", "?")
                if t == "tool_call":
                    args_str = str(d.get("args", ""))[:100]
                    summary.append(f"step {d.get('step','?')} CALL {d.get('tool','?')}: {args_str}")
                elif t == "tool_result":
                    result_str = str(d.get("result", ""))[:100]
                    summary.append(f"step {d.get('step','?')} RESULT {d.get('tool','?')}: {result_str}")
                elif t in ("finish", "error", "bump", "turn_failed"):
                    summary.append(f"** {t}: {json.dumps({k:v for k,v in d.items() if k != 'ts'})}")
                elif t == "usage":
                    summary.append(f"usage: in={d.get('input_tokens',0)} out={d.get('output_tokens',0)} cost=${d.get('cost_usd',0):.4f}")
                else:
                    summary.append(f"{t}: {str(d)[:80]}")
            except Exception:
                summary.append(line[:100])
        return "\n".join(summary)
    except FileNotFoundError:
        return f"Trace file not found: {path}"
    except Exception as e:
        return f"Error reading trace: {e}"


async def do_broadcast(deps: CoordinatorDeps, challenge_name: str, message: str) -> str:
    """Broadcast a message to all solvers working on a challenge."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    await swarm.message_bus.broadcast(message)
    return f"Broadcast to all solvers on {challenge_name}"
