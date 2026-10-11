"""Causal continuation after a solve when CTFd omits prerequisite metadata."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from backend.agents.coordinator_core import _attach_scenario
from backend.agents.scenario_orchestrator import ImmediateUnlockContext, handle_new_challenge
from backend.agents.swarm import ChallengeSwarm
from backend.cost_tracker import CostTracker
from backend.ctfd import SubmitResult, solved_names_from_stubs
from backend.deps import CoordinatorDeps
from backend.poller import CTFdPoller
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND, SolverResult


class _Sandbox:
    def __init__(self, workspace_dir: str) -> None:
        self.workspace_dir = workspace_dir
        self._container_id = "SANDBOX_A"
        self._container = object()

    @property
    def container_id(self) -> str:
        return self._container_id

    async def stop(self) -> None:
        self._container = None


class _Solver:
    def __init__(self, meta: ChallengeMeta, sandbox: _Sandbox) -> None:
        self.meta = meta
        self.sandbox = sandbox
        self.continue_calls: list[tuple[str, str]] = []
        self.tracer = SimpleNamespace(path="", event=lambda *_a, **_k: None, close=lambda: None)

    async def start(self) -> None:
        return None

    async def run_until_done_or_gave_up(self) -> SolverResult:
        return SolverResult(flag=None, status=FLAG_FOUND, findings_summary="", step_count=1, cost_usd=0, log_path="")

    async def continue_with_challenge(self, challenge_meta: ChallengeMeta, challenge_dir: str) -> None:
        self.continue_calls.append((self.meta.name, challenge_meta.name))
        self.meta = challenge_meta

    def bump(self, insights: str) -> None:
        del insights

    async def stop(self) -> None:
        return None


class _CTFd:
    def __init__(self, output_dir: Path, *, hidden: set[int] | None = None) -> None:
        self.output_dir = output_dir
        self.hidden = hidden or {2, 3}
        self.challenges = {
            1: self._ch(1, "Kral Toprakları", []),
            2: self._ch(2, "Kışyarı", []),
            3: self._ch(3, "Kara Kale", []),
        }

    @staticmethod
    def _ch(cid: int, name: str, prereqs: list[int], tags: list[str] | None = None) -> dict:
        return {
            "id": cid,
            "name": name,
            "category": "Windows",
            "type": "standard",
            "description": name,
            "tags": tags or [],
            "requirements": {"prerequisites": prereqs},
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
            {"id": ch["id"], "name": ch["name"], "category": ch["category"], "type": ch["type"],
             "solved_by_me": ch["solved_by_me"]}
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
        del flag
        challenge = next(ch for ch in self.challenges.values() if ch["name"] == challenge_name)
        challenge["solved_by_me"] = True
        return SubmitResult("correct", "ok", "CORRECT")

    async def pull_challenge(self, challenge: dict, output_dir: str) -> str:
        slug = f"ch-{challenge['id']}"
        challenge_dir = Path(output_dir) / slug
        challenge_dir.mkdir(parents=True, exist_ok=True)
        (challenge_dir / "metadata.yml").write_text(
            yaml.dump({"id": challenge["id"], "name": challenge["name"], "category": "Windows"},
                      allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return str(challenge_dir)

    async def close(self) -> None:
        return None


async def _setup(tmp_path: Path, ctfd: _CTFd):
    settings = SimpleNamespace(
        sandbox_image="ctf-sandbox",
        container_memory_limit="4g",
        scenario_unlock_wait_seconds=0.05,
    )
    deps = CoordinatorDeps(
        ctfd=ctfd,
        cost_tracker=CostTracker(),
        settings=settings,
        model_specs=["codex/gpt-5.6-sol"],
        challenges_root=str(tmp_path / "challenges"),
    )
    poller = CTFdPoller(ctfd=ctfd, interval_s=60)
    deps.poller = poller
    await poller._seed()
    sandbox = _Sandbox(str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(exist_ok=True)
    meta = ChallengeMeta(name="Kral Toprakları", category="Windows", id=1)
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path / "challenges" / "ch-1"),
        meta=meta,
        ctfd=ctfd,
        cost_tracker=deps.cost_tracker,
        settings=settings,
        model_specs=deps.model_specs,
    )
    _attach_scenario(deps, swarm, meta)
    solver = _Solver(meta, sandbox)
    swarm.solvers["codex/gpt-5.6-sol"] = solver
    swarm.scenario_session.solver = solver
    deps.swarms["Kral Toprakları"] = swarm
    return deps, swarm, solver, poller


@pytest.mark.asyncio
async def test_single_immediate_unlock_without_requirements_continues(tmp_path) -> None:
    ctfd = _CTFd(tmp_path / "challenges")
    deps, swarm, solver, poller = await _setup(tmp_path, ctfd)
    deps.scenario_registry.mark_challenge_solved(swarm.scenario_session, 1, "Kral Toprakları")
    deps.scenario_registry.mark_waiting_for_unlock(swarm.scenario_session)
    ctfd.hidden.discard(2)
    events = await poller.refresh()
    new = [event for event in events if event.kind == "new_challenge"]
    assert [event.challenge_name for event in new] == ["Kışyarı"]
    causal = ImmediateUnlockContext(
        source_session_id=swarm.scenario_session.id,
        source_challenge_id=1,
        source_challenge_name="Kral Toprakları",
        new_challenge_count=1,
    )
    outcome = await handle_new_challenge(
        deps, "Kışyarı", {"id": 2}, unlocked=True, causal=causal,
    )
    assert outcome == "continued"
    assert swarm.scenario_session.current_challenge_name == "Kışyarı"
    continuation = await swarm.wait_for_continuation()
    assert continuation is not None
    assert continuation[0].name == "Kışyarı"
    assert solver.sandbox is swarm.solvers["codex/gpt-5.6-sol"].sandbox


@pytest.mark.asyncio
async def test_background_unlock_without_requirements_stays_independent(tmp_path, monkeypatch) -> None:
    ctfd = _CTFd(tmp_path / "challenges")
    deps, swarm, solver, _poller = await _setup(tmp_path, ctfd)
    deps.scenario_registry.mark_challenge_solved(swarm.scenario_session, 1, "Kral Toprakları")
    deps.scenario_registry.mark_waiting_for_unlock(swarm.scenario_session)
    spawn_names: list[str] = []

    async def _spawn(deps_inner, name: str) -> str:
        spawn_names.append(name)
        return f"Swarm spawned for {name} with 1 models"

    monkeypatch.setattr("backend.agents.coordinator_core.spawn_swarm_unlocked", _spawn)
    outcome = await handle_new_challenge(deps, "Kışyarı", {"id": 2}, unlocked=True)
    assert outcome == "spawned"
    assert spawn_names == ["Kışyarı"]
    assert solver.continue_calls == []


@pytest.mark.asyncio
async def test_two_simultaneous_unlinked_unlocks_skip_causal(tmp_path, monkeypatch) -> None:
    ctfd = _CTFd(tmp_path / "challenges")
    deps, swarm, solver, _poller = await _setup(tmp_path, ctfd)
    deps.scenario_registry.mark_challenge_solved(swarm.scenario_session, 1, "Kral Toprakları")
    deps.scenario_registry.mark_waiting_for_unlock(swarm.scenario_session)
    ctfd.hidden.clear()
    spawn_names: list[str] = []

    async def _spawn(deps_inner, name: str) -> str:
        spawn_names.append(name)
        return f"Swarm spawned for {name} with 1 models"

    monkeypatch.setattr("backend.agents.coordinator_core.spawn_swarm_unlocked", _spawn)
    causal = ImmediateUnlockContext(
        source_session_id=swarm.scenario_session.id,
        source_challenge_id=1,
        source_challenge_name="Kral Toprakları",
        new_challenge_count=2,
    )
    first = await handle_new_challenge(deps, "Kışyarı", {"id": 2}, unlocked=True, causal=causal)
    second = await handle_new_challenge(deps, "Kara Kale", {"id": 3}, unlocked=True, causal=causal)
    assert first == "spawned"
    assert second == "spawned"
    assert spawn_names == ["Kışyarı", "Kara Kale"]
    assert solver.continue_calls == []


@pytest.mark.asyncio
async def test_explicit_prerequisite_wins_over_causal(tmp_path) -> None:
    ctfd = _CTFd(tmp_path / "challenges")
    ctfd.challenges[2]["requirements"] = {"prerequisites": [1]}
    deps, swarm, solver, _poller = await _setup(tmp_path, ctfd)
    deps.scenario_registry.mark_challenge_solved(swarm.scenario_session, 1, "Kral Toprakları")
    deps.scenario_registry.mark_waiting_for_unlock(swarm.scenario_session)
    ctfd.hidden.discard(2)
    causal = ImmediateUnlockContext(
        source_session_id=swarm.scenario_session.id,
        source_challenge_id=1,
        source_challenge_name="Kral Toprakları",
        new_challenge_count=1,
    )
    outcome = await handle_new_challenge(deps, "Kışyarı", {"id": 2}, unlocked=True, causal=causal)
    assert outcome == "continued"
    continuation = await swarm.wait_for_continuation()
    assert continuation is not None
    assert continuation[0].name == "Kışyarı"


@pytest.mark.asyncio
async def test_source_not_waiting_skips_causal(tmp_path, monkeypatch) -> None:
    ctfd = _CTFd(tmp_path / "challenges")
    deps, swarm, solver, _poller = await _setup(tmp_path, ctfd)
    swarm.scenario_session.state = "running"
    spawn_names: list[str] = []

    async def _spawn(deps_inner, name: str) -> str:
        spawn_names.append(name)
        return f"Swarm spawned for {name} with 1 models"

    monkeypatch.setattr("backend.agents.coordinator_core.spawn_swarm_unlocked", _spawn)
    causal = ImmediateUnlockContext(
        source_session_id=swarm.scenario_session.id,
        source_challenge_id=1,
        source_challenge_name="Kral Toprakları",
        new_challenge_count=1,
    )
    outcome = await handle_new_challenge(deps, "Kışyarı", {"id": 2}, unlocked=True, causal=causal)
    assert outcome == "spawned"
    assert solver.continue_calls == []
    assert spawn_names == ["Kışyarı"]


@pytest.mark.asyncio
async def test_explicit_tag_wins_over_causal(tmp_path) -> None:
    ctfd = _CTFd(tmp_path / "challenges")
    ctfd.challenges[1]["tags"] = ["scenario:kral"]
    ctfd.challenges[2]["tags"] = ["scenario:kral"]
    deps, swarm, solver, _poller = await _setup(tmp_path, ctfd)
    swarm.scenario_session.metadata["tags"] = ["scenario:kral"]
    deps.scenario_registry.mark_challenge_solved(swarm.scenario_session, 1, "Kral Toprakları")
    deps.scenario_registry.mark_waiting_for_unlock(swarm.scenario_session)
    ctfd.hidden.discard(2)
    causal = ImmediateUnlockContext(
        source_session_id=swarm.scenario_session.id,
        source_challenge_id=1,
        source_challenge_name="Kral Toprakları",
        new_challenge_count=1,
    )
    outcome = await handle_new_challenge(deps, "Kışyarı", {"id": 2}, unlocked=True, causal=causal)
    assert outcome == "continued"
    continuation = await swarm.wait_for_continuation()
    assert continuation is not None
    assert continuation[0].name == "Kışyarı"
