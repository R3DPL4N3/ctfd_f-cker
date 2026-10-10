"""End-to-end scenario continuation without Codex or Docker."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from backend.agents.coordinator_core import (
    _attach_scenario,
    ensure_challenge_materials,
    spawn_swarm_unlocked,
)
from backend.agents.scenario_orchestrator import handle_new_challenge
from backend.agents.swarm import ChallengeSwarm
from backend.cost_tracker import CostTracker
from backend.ctfd import SubmitResult, solved_names_from_stubs
from backend.deps import CoordinatorDeps
from backend.poller import CTFdPoller
from backend.prompts import ChallengeMeta
from backend.scenario_memory import ScenarioMemoryStore
from backend.solver_base import FLAG_FOUND, GAVE_UP, SolverResult

FLAGS = {
    "AD-01": "flag{ad1}",
    "AD-02": "flag{ad2}",
    "AD-03": "flag{ad3}",
}

STAGE_METRICS = {
    "AD-01": (3, 0.22),
    "AD-02": (5, 0.71),
    "AD-03": (8, 1.06),
}


class FakeSandbox:
    def __init__(self, workspace_dir: str, container_id: str = "SANDBOX_A") -> None:
        self.workspace_dir = workspace_dir
        self._container_id = container_id
        self.stopped = False
        self._container = object()

    @property
    def container_id(self) -> str:
        return self._container_id

    async def stop(self) -> None:
        self.stopped = True
        self._container = None


class FakeContinuingSolver:
    """Persistent solver double: one object, one thread, one sandbox."""

    def __init__(
        self,
        *,
        model_spec: str,
        meta: ChallengeMeta,
        challenge_dir: str,
        submit_fn,
        sandbox: FakeSandbox,
    ) -> None:
        self.model_spec = model_spec
        self.meta = meta
        self.challenge_dir = challenge_dir
        self.submit_fn = submit_fn
        self.sandbox = sandbox
        self.agent_name = f"{meta.name}/gpt-5.6-sol"
        self._thread_id = "THREAD_A"
        self._proc = object()
        self.start_calls = 0
        self.stop_calls = 0
        self.continue_calls: list[tuple[str, str]] = []
        self._step_count = 0
        self._cost_usd = 0.0
        self._confirmed = False
        self._flag = None
        self._memory = None
        self.tracer = SimpleNamespace(path="", event=lambda *_a, **_k: None, close=lambda: None)

    async def start(self) -> None:
        self.start_calls += 1
        Path(self.sandbox.workspace_dir, "persistence-test.txt").write_text(
            "keep-me", encoding="utf-8"
        )
        self._memory = ScenarioMemoryStore(
            self.sandbox.workspace_dir,
            current_stage=self.meta.name,
        )
        self._memory.ensure()
        self._memory.update({
            "targets": [{"host": "10.10.10.5", "hostname": "server01"}],
            "credentials": [{
                "username": "svc_sql",
                "domain": "corp.local",
                "password": "s3cret",
                "source": "AD-01",
            }],
            "networks": [{"cidr": "10.20.0.0/24"}],
            "findings": ["svc_sql appears reusable on internal hosts"],
        })

    async def run_until_done_or_gave_up(self) -> SolverResult:
        steps, cost = STAGE_METRICS[self.meta.name]
        self._step_count = steps
        self._cost_usd = cost
        flag = FLAGS[self.meta.name]
        _display, confirmed = await self.submit_fn(flag)
        if confirmed:
            self._confirmed = True
            self._flag = flag
            return SolverResult(
                flag=flag,
                status=FLAG_FOUND,
                findings_summary=f"solved {self.meta.name}",
                step_count=self._step_count,
                cost_usd=self._cost_usd,
                log_path="",
            )
        return SolverResult(
            flag=None,
            status=GAVE_UP,
            findings_summary="not confirmed",
            step_count=self._step_count,
            cost_usd=self._cost_usd,
            log_path="",
        )

    async def continue_with_challenge(self, challenge_meta: ChallengeMeta, challenge_dir: str) -> None:
        sentinel = Path(self.sandbox.workspace_dir) / "persistence-test.txt"
        assert sentinel.read_text(encoding="utf-8") == "keep-me"
        if getattr(self, "_memory", None) is not None:
            self._memory.advance_stage(self.meta.name, challenge_meta.name)
        self.continue_calls.append((self.meta.name, challenge_meta.name))
        self.meta = challenge_meta
        self.challenge_dir = challenge_dir
        self.agent_name = f"{challenge_meta.name}/gpt-5.6-sol"
        self._confirmed = False
        self._flag = None
        self._step_count = 0
        self._cost_usd = 0.0

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
        self.stop_calls += 1
        await self.sandbox.stop()


class FakeCTFd:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.hidden = {2, 3}
        self.challenges = {
            1: self._challenge(1, "AD-01", []),
            2: self._challenge(2, "AD-02", [1]),
            3: self._challenge(3, "AD-03", [2]),
        }
        self.submit_calls: list[tuple[str, str]] = []

    @staticmethod
    def _challenge(challenge_id: int, name: str, prerequisites: list[int]) -> dict:
        return {
            "id": challenge_id,
            "name": name,
            "category": "Windows",
            "type": "standard",
            "description": f"{name} objective",
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
        self.submit_calls.append((challenge_name, flag))
        expected = FLAGS[challenge_name]
        if flag.strip() != expected:
            return SubmitResult("incorrect", "no", f'INCORRECT — "{flag}" rejected.')
        challenge = next(ch for ch in self.challenges.values() if ch["name"] == challenge_name)
        challenge["solved_by_me"] = True
        if challenge["id"] == 1:
            self.hidden.discard(2)
        elif challenge["id"] == 2:
            self.hidden.discard(3)
        return SubmitResult("correct", "ok", f'CORRECT — "{flag}" accepted.')

    async def pull_challenge(self, challenge: dict, output_dir: str) -> str:
        slug = challenge["name"].lower()
        challenge_dir = Path(output_dir) / slug
        challenge_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": challenge["id"],
            "name": challenge["name"],
            "category": challenge["category"],
            "description": challenge["description"],
            "requirements": challenge["requirements"]["prerequisites"],
            "tags": challenge["tags"],
            "value": challenge["value"],
            "connection_info": "",
        }
        (challenge_dir / "metadata.yml").write_text(
            yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        return str(challenge_dir)

    async def close(self) -> None:
        return None


def _patch_solver(monkeypatch, sandbox: FakeSandbox, created: list[FakeContinuingSolver]) -> None:
    def _create(self, model_spec: str) -> FakeContinuingSolver:
        solver = FakeContinuingSolver(
            model_spec=model_spec,
            meta=self.meta,
            challenge_dir=self.challenge_dir,
            submit_fn=lambda flag: self.try_submit_flag(flag, model_spec),
            sandbox=sandbox,
        )
        created.append(solver)
        return solver

    monkeypatch.setattr(ChallengeSwarm, "_create_solver", _create)


async def _deps(tmp_path: Path, ctfd: FakeCTFd | None = None) -> tuple[CoordinatorDeps, FakeCTFd, CTFdPoller]:
    settings = SimpleNamespace(
        sandbox_image="ctf-sandbox",
        container_memory_limit="4g",
        scenario_unlock_wait_seconds=0.05,
    )
    client = ctfd or FakeCTFd(tmp_path / "challenges")
    deps = CoordinatorDeps(
        ctfd=client,
        cost_tracker=CostTracker(),
        settings=settings,
        model_specs=["codex/gpt-5.6-sol"],
        challenges_root=str(tmp_path / "challenges"),
        max_concurrent_challenges=10,
    )
    poller = CTFdPoller(ctfd=client, interval_s=60)
    deps.poller = poller
    await poller._seed()
    return deps, client, poller


@pytest.mark.asyncio
async def test_three_stage_chain_reuses_solver_thread_and_sandbox(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = FakeSandbox(str(workspace))
    created: list[FakeContinuingSolver] = []
    _patch_solver(monkeypatch, sandbox, created)

    spawn_names: list[str] = []
    real_spawn = spawn_swarm_unlocked

    async def _count_spawn(deps, challenge_name: str) -> str:
        spawn_names.append(challenge_name)
        return await real_spawn(deps, challenge_name)

    monkeypatch.setattr(
        "backend.agents.coordinator_core.spawn_swarm_unlocked",
        _count_spawn,
    )

    deps, _ctfd, _poller = await _deps(tmp_path)
    outcome = await handle_new_challenge(deps, "AD-01", {"id": 1})
    assert outcome == "spawned"

    task = next(iter(deps.swarm_tasks.values()))
    await asyncio.wait_for(task, timeout=5)

    assert spawn_names == ["AD-01"]
    assert len(created) == 1
    solver = created[0]
    assert solver.start_calls == 1
    assert solver.stop_calls == 1
    assert solver.continue_calls == [("AD-01", "AD-02"), ("AD-02", "AD-03")]
    assert solver._thread_id == "THREAD_A"
    assert solver.sandbox is sandbox
    assert sandbox.container_id == "SANDBOX_A"
    assert sandbox.workspace_dir == str(workspace)
    assert sandbox.stopped is True
    assert (workspace / "persistence-test.txt").read_text(encoding="utf-8") == "keep-me"
    memory = ScenarioMemoryStore(workspace).load()
    markdown = (workspace / "scenario-memory.md").read_text(encoding="utf-8")
    assert memory.current_stage == "AD-03"
    assert memory.completed_stages == ["AD-01", "AD-02"]
    assert memory.credentials[0].username == "svc_sql"
    assert memory.credentials[0].password == "s3cret"
    assert memory.targets[0].host == "10.10.10.5"
    assert "s3cret" not in markdown
    assert "password available" in markdown

    session = next(iter(deps.scenario_registry._sessions.values()))
    assert session.state == "stopped"
    assert session.solver is solver
    assert session.metadata.get("memory_path") == str(workspace / "scenario-state.json")
    assert session.solved_challenge_names == ["AD-01", "AD-02", "AD-03"]
    stages = {row["challenge"]: row for row in session.metadata["stage_costs"]}
    assert stages["AD-01"]["cost_usd"] == 0.22
    assert stages["AD-02"]["cost_usd"] == 0.71
    assert stages["AD-03"]["cost_usd"] == 1.06
    assert stages["AD-01"]["step_count"] == 3
    assert stages["AD-02"]["step_count"] == 5
    assert stages["AD-03"]["step_count"] == 8
    assert "Scenario total:" in session.metadata["cost_summary"]
    assert "$1.99" in session.metadata["cost_summary"]


@pytest.mark.asyncio
async def test_duplicate_unlock_is_routed_once(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = FakeSandbox(str(workspace))

    spawn_names: list[str] = []
    real_spawn = spawn_swarm_unlocked

    async def _count_spawn(deps, challenge_name: str) -> str:
        spawn_names.append(challenge_name)
        return await real_spawn(deps, challenge_name)

    monkeypatch.setattr(
        "backend.agents.coordinator_core.spawn_swarm_unlocked",
        _count_spawn,
    )

    deps, ctfd, poller = await _deps(tmp_path)
    challenge_dir, meta = await ensure_challenge_materials(deps, "AD-01")
    swarm = ChallengeSwarm(
        challenge_dir=challenge_dir,
        meta=meta,
        ctfd=ctfd,
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        model_specs=deps.model_specs,
    )
    _attach_scenario(deps, swarm, meta)
    deps.swarms["AD-01"] = swarm
    solver = FakeContinuingSolver(
        model_spec="codex/gpt-5.6-sol",
        meta=meta,
        challenge_dir=challenge_dir,
        submit_fn=lambda flag: swarm.try_submit_flag(flag, "codex/gpt-5.6-sol"),
        sandbox=sandbox,
    )
    swarm.solvers["codex/gpt-5.6-sol"] = solver
    swarm.scenario_session.solver = solver
    await solver.start()

    display, accepted = await swarm.try_submit_flag(FLAGS["AD-01"], "codex/gpt-5.6-sol")
    assert accepted is True
    assert display.startswith("CORRECT")
    assert swarm.scenario_session.state == "running"
    assert swarm.scenario_session.current_challenge_name == "AD-02"

    first, second = await asyncio.gather(
        handle_new_challenge(deps, "AD-02", {"id": 2}, unlocked=True),
        handle_new_challenge(deps, "AD-02", {"id": 2}, unlocked=True),
    )
    later = await handle_new_challenge(deps, "AD-02", {"id": 2}, unlocked=True)
    queued = await poller.refresh()

    assert first == "skipped"
    assert second == "skipped"
    assert later == "skipped"
    assert spawn_names == []
    assert "AD-02" not in spawn_names
    assert solver.continue_calls == []
    assert not any(event.challenge_name == "AD-02" and event.kind == "new_challenge" for event in queued)
    swarm.finish_scenario_wait()


@pytest.mark.asyncio
async def test_concurrent_unlock_routing_is_idempotent(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = FakeSandbox(str(workspace))
    spawn_names: list[str] = []
    real_spawn = spawn_swarm_unlocked

    async def _count_spawn(deps, challenge_name: str) -> str:
        spawn_names.append(challenge_name)
        return await real_spawn(deps, challenge_name)

    monkeypatch.setattr(
        "backend.agents.coordinator_core.spawn_swarm_unlocked",
        _count_spawn,
    )

    deps, ctfd, _poller = await _deps(tmp_path)
    challenge_dir, meta = await ensure_challenge_materials(deps, "AD-01")
    swarm = ChallengeSwarm(
        challenge_dir=challenge_dir,
        meta=meta,
        ctfd=ctfd,
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        model_specs=deps.model_specs,
    )
    _attach_scenario(deps, swarm, meta)
    deps.swarms["AD-01"] = swarm
    solver = FakeContinuingSolver(
        model_spec="codex/gpt-5.6-sol",
        meta=meta,
        challenge_dir=challenge_dir,
        submit_fn=lambda flag: swarm.try_submit_flag(flag, "codex/gpt-5.6-sol"),
        sandbox=sandbox,
    )
    swarm.solvers["codex/gpt-5.6-sol"] = solver
    swarm.scenario_session.solver = solver
    deps.scenario_registry.mark_challenge_solved(swarm.scenario_session, 1, "AD-01")
    deps.scenario_registry.mark_waiting_for_unlock(swarm.scenario_session)
    ctfd.hidden.discard(2)

    first, second = await asyncio.gather(
        handle_new_challenge(deps, "AD-02", {"id": 2}, unlocked=True),
        handle_new_challenge(deps, "AD-02", {"id": 2}, unlocked=True),
    )

    assert sorted([first, second]) == ["continued", "skipped"]
    assert spawn_names == []
    assert swarm._continuation_decided is True
    continuation = await swarm.wait_for_continuation()
    assert continuation is not None
    assert continuation[0].name == "AD-02"


class EmptyPrereqCTFd(FakeCTFd):
    @staticmethod
    def _challenge(challenge_id: int, name: str, prerequisites: list[int]) -> dict:
        return FakeCTFd._challenge(challenge_id, name, [])


@pytest.mark.asyncio
async def test_empty_requirements_chain_reuses_same_scenario(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = FakeSandbox(str(workspace))
    created: list[FakeContinuingSolver] = []
    _patch_solver(monkeypatch, sandbox, created)
    spawn_names: list[str] = []
    real_spawn = spawn_swarm_unlocked

    async def _count_spawn(deps, challenge_name: str) -> str:
        spawn_names.append(challenge_name)
        return await real_spawn(deps, challenge_name)

    monkeypatch.setattr(
        "backend.agents.coordinator_core.spawn_swarm_unlocked",
        _count_spawn,
    )

    deps, _ctfd, _poller = await _deps(tmp_path, ctfd=EmptyPrereqCTFd(tmp_path / "challenges"))
    outcome = await handle_new_challenge(deps, "AD-01", {"id": 1})
    assert outcome == "spawned"
    task = next(iter(deps.swarm_tasks.values()))
    await asyncio.wait_for(task, timeout=5)

    assert spawn_names == ["AD-01"]
    assert len(created) == 1
    solver = created[0]
    assert solver.continue_calls == [("AD-01", "AD-02"), ("AD-02", "AD-03")]
    assert solver.sandbox is sandbox
    assert sandbox.workspace_dir == str(workspace)
    session = next(iter(deps.scenario_registry._sessions.values()))
    assert session.solver is solver
    assert session.solved_challenge_names == ["AD-01", "AD-02", "AD-03"]
    memory = ScenarioMemoryStore(workspace).load()
    assert memory.current_stage == "AD-03"
    assert memory.completed_stages == ["AD-01", "AD-02"]
