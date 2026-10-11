"""Scenario-scoped solver lifecycle: sibling cancel must not destroy losing solvers."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from backend.agents.coordinator_core import spawn_swarm_unlocked
from backend.agents.scenario_orchestrator import handle_new_challenge
from backend.agents.swarm import ChallengeSwarm
from backend.cost_tracker import CostTracker
from backend.ctfd import SubmitResult, solved_names_from_stubs
from backend.deps import CoordinatorDeps
from backend.poller import CTFdPoller
from backend.prompts import ChallengeMeta
from backend.scenario_memory import ScenarioMemoryStore
from backend.solver_base import CANCELLED, FLAG_FOUND, GAVE_UP, SolverResult
from tests.test_scenario_e2e import FakeSandbox

STAGE1 = "İlk Temas"
STAGE2 = "Derinlere"
FLAGS = {STAGE1: "flag{ilk}", STAGE2: "flag{derin}"}
SOL = "codex/gpt-5.6-sol"
GLM = "openai/glm-5.3"
logger = logging.getLogger("backend.agents.swarm")


class LifecycleSolver:
    """Winner submits immediately; loser hangs until cancel_current_run."""

    def __init__(
        self,
        *,
        model_spec: str,
        meta: ChallengeMeta,
        challenge_dir: str,
        submit_fn,
        sandbox: FakeSandbox,
        hang: bool,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_spec.split("/", 1)[-1]
        self.meta = meta
        self.challenge_dir = challenge_dir
        self.submit_fn = submit_fn
        self.sandbox = sandbox
        self.hang = hang
        self.agent_name = f"{meta.name}/{self.model_id}"
        self.start_calls = 0
        self.stop_calls = 0
        self.continue_calls: list[tuple[str, str]] = []
        self.stage_runs: list[str] = []
        self.cancelled_stages: list[str] = []
        self._active_run: asyncio.Task | None = None
        self._closed = False
        self._memory = None
        self.tracer = SimpleNamespace(path="", event=lambda *_a, **_k: None, close=lambda: None)

    async def start(self) -> None:
        self.start_calls += 1
        logger.info("[%s] Solver started", self.agent_name)
        self._memory = ScenarioMemoryStore(
            self.sandbox.workspace_dir,
            current_stage=self.meta.name,
        )
        self._memory.ensure()
        if self.hang:
            self._memory.update({
                "credentials": [{
                    "username": "glm-user",
                    "password": "glm-secret",
                    "source": self.meta.name,
                }],
            })

    async def run_until_done_or_gave_up(self) -> SolverResult:
        self._active_run = asyncio.current_task()
        self.stage_runs.append(self.meta.name)
        try:
            if self.hang:
                await asyncio.Event().wait()
            flag = FLAGS[self.meta.name]
            _display, confirmed = await self.submit_fn(flag)
            if confirmed:
                return SolverResult(
                    flag=flag,
                    status=FLAG_FOUND,
                    findings_summary=f"solved {self.meta.name}",
                    step_count=2,
                    cost_usd=0.1,
                    log_path="",
                )
            return SolverResult(
                flag=None,
                status=GAVE_UP,
                findings_summary="not confirmed",
                step_count=1,
                cost_usd=0.0,
                log_path="",
            )
        except asyncio.CancelledError:
            self.cancelled_stages.append(self.meta.name)
            return SolverResult(
                flag=None,
                status=CANCELLED,
                findings_summary="cancelled",
                step_count=1,
                cost_usd=0.05,
                log_path="",
            )
        finally:
            self._active_run = None

    def cancel_current_run(self) -> None:
        task = self._active_run
        if task is not None and not task.done():
            task.cancel()

    async def continue_with_challenge(self, challenge_meta: ChallengeMeta, challenge_dir: str) -> None:
        if self._memory is not None:
            self._memory.advance_stage(self.meta.name, challenge_meta.name)
        self.continue_calls.append((self.meta.name, challenge_meta.name))
        self.meta = challenge_meta
        self.challenge_dir = challenge_dir
        self.agent_name = f"{challenge_meta.name}/{self.model_id}"

    def bump(self, insights: str) -> None:
        del insights

    def bind_scenario(self, scenario_id: str) -> None:
        if self._memory is not None:
            self._memory.set_scenario_id(scenario_id)

    @property
    def memory_path(self) -> str | None:
        if self._memory is None:
            return None
        return str(self._memory.json_path)

    async def stop(self) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_calls += 1
        await self.sandbox.stop()


class ChainCTFd:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.hidden = {2}
        self.challenges = {
            1: self._challenge(1, STAGE1, []),
            2: self._challenge(2, STAGE2, [1]),
        }

    @staticmethod
    def _challenge(challenge_id: int, name: str, prerequisites: list[int]) -> dict:
        return {
            "id": challenge_id,
            "name": name,
            "category": "Windows",
            "type": "standard",
            "description": name,
            "tags": [],
            "requirements": {"prerequisites": prerequisites},
            "solved_by_me": False,
            "files": [],
            "hints": [],
            "value": 100,
            "solves": 0,
            "connection_info": "",
        }

    def _visible(self) -> list[dict]:
        return [dict(ch) for cid, ch in self.challenges.items() if cid not in self.hidden]

    async def fetch_challenge_stubs(self) -> list[dict]:
        return [
            {
                "id": ch["id"],
                "name": ch["name"],
                "category": ch["category"],
                "type": ch["type"],
                "solved_by_me": ch["solved_by_me"],
            }
            for ch in self._visible()
        ]

    async def fetch_all_challenges(self) -> list[dict]:
        return self._visible()

    async def fetch_challenge_detail(self, challenge_id: int) -> dict:
        return dict(self.challenges[int(challenge_id)])

    async def fetch_solved_names(self) -> set[str]:
        names = solved_names_from_stubs(await self.fetch_challenge_stubs())
        return names if names is not None else set()

    async def get_challenge_id(self, name: str) -> int:
        for challenge in self.challenges.values():
            if challenge["name"] == name:
                return int(challenge["id"])
        raise RuntimeError(name)

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        expected = FLAGS[challenge_name]
        if flag.strip() != expected:
            return SubmitResult("incorrect", "no", "INCORRECT")
        challenge = next(ch for ch in self.challenges.values() if ch["name"] == challenge_name)
        challenge["solved_by_me"] = True
        if challenge["id"] == 1:
            self.hidden.discard(2)
        return SubmitResult("correct", "ok", "CORRECT")

    async def pull_challenge(self, challenge: dict, output_dir: str) -> str:
        slug = f"ch-{challenge['id']}"
        challenge_dir = Path(output_dir) / slug
        challenge_dir.mkdir(parents=True, exist_ok=True)
        (challenge_dir / "metadata.yml").write_text(
            yaml.dump(
                {"id": challenge["id"], "name": challenge["name"], "category": "Windows"},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return str(challenge_dir)

    async def close(self) -> None:
        return None


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _patch_lifecycle(
    monkeypatch,
    created: dict[str, LifecycleSolver],
    workspaces: Path,
    *,
    hang_specs: set[str] | None = None,
) -> None:
    hang_on = hang_specs if hang_specs is not None else {GLM}

    def _create(self, model_spec: str) -> LifecycleSolver:
        workspace = workspaces / model_spec.replace("/", "_")
        workspace.mkdir(parents=True, exist_ok=True)
        solver = LifecycleSolver(
            model_spec=model_spec,
            meta=self.meta,
            challenge_dir=self.challenge_dir,
            submit_fn=lambda flag, spec=model_spec: self.try_submit_flag(flag, spec),
            sandbox=FakeSandbox(str(workspace), container_id=f"box-{model_spec.split('/')[-1]}"),
            hang=model_spec in hang_on,
        )
        created[model_spec] = solver
        return solver

    monkeypatch.setattr(ChallengeSwarm, "_create_solver", _create)


async def _lifecycle_deps(tmp_path: Path) -> tuple[CoordinatorDeps, ChainCTFd]:
    settings = SimpleNamespace(
        sandbox_image="ctf-sandbox",
        container_memory_limit="4g",
        scenario_unlock_wait_seconds=0.05,
    )
    ctfd = ChainCTFd(tmp_path / "challenges")
    deps = CoordinatorDeps(
        ctfd=ctfd,
        cost_tracker=CostTracker(),
        settings=settings,
        model_specs=[SOL, GLM],
        challenges_root=str(tmp_path / "challenges"),
        max_concurrent_challenges=10,
    )
    poller = CTFdPoller(ctfd=ctfd, interval_s=60)
    deps.poller = poller
    await poller._seed()
    return deps, ctfd


@pytest.mark.asyncio
async def test_cancelled_sibling_rejoins_next_stage(tmp_path, monkeypatch, caplog) -> None:
    created: dict[str, LifecycleSolver] = {}
    _patch_lifecycle(monkeypatch, created, tmp_path / "workspaces")
    deps, _ctfd = await _lifecycle_deps(tmp_path)

    caplog.set_level(logging.INFO)
    outcome = await handle_new_challenge(deps, STAGE1, {"id": 1})
    assert outcome == "spawned"

    await _wait_until(lambda: SOL in created and GLM in created)
    sol, glm = created[SOL], created[GLM]
    await _wait_until(lambda: STAGE1 in glm.stage_runs and STAGE2 in sol.stage_runs)

    assert glm in deps.swarms[STAGE2].solvers.values()
    swarm = deps.swarms[STAGE2]
    assert swarm.solvers[SOL] is sol
    assert swarm.solvers[GLM] is glm
    assert glm.sandbox.stopped is False
    assert sol.sandbox.stopped is False
    assert glm.stop_calls == 0
    assert STAGE1 in glm.cancelled_stages
    assert glm.continue_calls == [(STAGE1, STAGE2)]
    assert sol.continue_calls == [(STAGE1, STAGE2)]
    assert STAGE2 in glm.stage_runs
    assert glm._memory is not None
    state = glm._memory.load()
    assert state.current_stage == STAGE2
    assert STAGE1 in state.completed_stages
    assert state.credentials[0].username == "glm-user"

    first = await spawn_swarm_unlocked(deps, STAGE2)
    second = await spawn_swarm_unlocked(deps, STAGE2)
    assert "still running" in first
    assert "still running" in second
    assert sum(1 for task in swarm._solver_tasks.values() if not task.done()) == 2
    assert len(created) == 2

    logs = caplog.text
    assert f"[{STAGE1}/gpt-5.6-sol]" in logs
    assert f"[{STAGE1}/glm-5.3]" in logs
    assert "Current stage run cancelled; solver retained for scenario-1" in logs
    assert "Scenario scenario-1 continued from İlk Temas to Derinlere" in logs
    assert f"[{STAGE2}/glm-5.3] Continuing solver in scenario-1" in logs
    assert f"[{STAGE2}/glm-5.3] Reused sandbox" in logs
    assert f"[{STAGE2}/gpt-5.6-sol] Continuing solver in scenario-1" in logs

    task = next(iter(deps.swarm_tasks.values()))
    await asyncio.wait_for(task, timeout=5)

    assert glm.stop_calls == 1
    assert sol.stop_calls == 1
    assert glm.sandbox.stopped is True
    assert sol.sandbox.stopped is True
    session = next(iter(deps.scenario_registry._sessions.values()))
    assert session.state == "stopped"
    assert session.metadata["solvers"][GLM] is glm
    assert session.metadata["solvers"][SOL] is sol


@pytest.mark.asyncio
async def test_kill_closes_all_scenario_solvers(tmp_path, monkeypatch) -> None:
    created: dict[str, LifecycleSolver] = {}
    _patch_lifecycle(monkeypatch, created, tmp_path / "workspaces", hang_specs={SOL, GLM})
    deps, _ctfd = await _lifecycle_deps(tmp_path)
    outcome = await handle_new_challenge(deps, STAGE1, {"id": 1})
    assert outcome == "spawned"
    await _wait_until(
        lambda: SOL in created and GLM in created
        and STAGE1 in created[SOL].stage_runs
        and STAGE1 in created[GLM].stage_runs
    )
    swarm = next(iter(deps.swarms.values()))
    swarm.kill()
    task = next(iter(deps.swarm_tasks.values()))
    await asyncio.wait_for(task, timeout=5)
    assert created[SOL].stop_calls == 1
    assert created[GLM].stop_calls == 1
    assert created[SOL].sandbox.stopped is True
    assert created[GLM].sandbox.stopped is True


@pytest.mark.asyncio
async def test_generic_cancel_current_run_keeps_sandbox_and_reruns(tmp_path) -> None:
    from backend.agents.solver import Solver

    sandbox = FakeSandbox(str(tmp_path / "ws"))
    solver = Solver(
        model_spec=GLM,
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name=STAGE1, category="Windows", id=1),
        ctfd=object(),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox", container_memory_limit="4g"),
        sandbox=sandbox,
        owns_sandbox=True,
    )

    async def hang() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(hang())
    solver._active_run = task
    solver.cancel_current_run()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sandbox.stopped is False
    assert solver._closed is False

    solver._active_run = None
    await solver.close()
    assert sandbox.stopped is True
    await solver.close()
    assert solver._closed is True
    solver.tracer.close()
