"""Scenario continuity: registry, routing, flag outcomes, and sandbox reuse."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.cost_tracker import CostTracker
from backend.ctfd import SubmitResult
from backend.flag_submit import FlagOutcome, classify_exception, submit_flag_candidate
from backend.poller import CTFdPoller
from backend.prompts import ChallengeMeta, build_continuation_prompt
from backend.sandbox import stage_challenge_into_workspace
from backend.scenario import ScenarioRegistry
from backend.scenario_router import (
    decide_continuation,
    parse_prerequisite_ids,
    parse_route_decision,
)
from backend.solver_base import submission_accepted


class _SubmitCTFd:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _swarm(ctfd) -> ChallengeSwarm:
    return ChallengeSwarm(
        challenge_dir=".",
        meta=ChallengeMeta(name="AD-01", category="Windows", id=1),
        ctfd=ctfd,
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(scenario_unlock_wait_seconds=0.01),
        model_specs=["codex/gpt-5.6-sol"],
    )


def _waiting(registry: ScenarioRegistry, name: str, challenge_id: int, tags: list[str] | None = None):
    session = registry.create_session(
        current_challenge_id=challenge_id,
        current_challenge_name=name,
        category="Windows",
        metadata={"tags": list(tags or [])},
    )
    registry.mark_challenge_solved(session, challenge_id, name)
    registry.mark_waiting_for_unlock(session)
    return session


def test_prerequisite_continues_one_waiting_session() -> None:
    registry = ScenarioRegistry()
    session = _waiting(registry, "AD-01", 1)
    decision = decide_continuation(
        registry.get_active_sessions(),
        prerequisite_ids=[1],
        tags=["Windows"],
    )
    assert decision.as_dict() == {
        "action": "continue",
        "confidence": 1.0,
        "reason": "CTFd prerequisite matches the waiting scenario",
        "session_id": session.id,
    }


def test_same_category_does_not_continue() -> None:
    registry = ScenarioRegistry()
    _waiting(registry, "AD-01", 1)
    decision = decide_continuation(
        registry.get_active_sessions(),
        prerequisite_ids=[],
        tags=["Windows"],
    )
    assert decision.action == "new"
    assert decision.session_id is None


def test_running_session_is_not_a_continuation_target() -> None:
    registry = ScenarioRegistry()
    session = registry.create_session(
        current_challenge_id=1,
        current_challenge_name="AD-01",
        category="Windows",
    )
    registry.mark_challenge_solved(session, 1, "AD-01")
    session.state = "running"
    decision = decide_continuation(registry.get_active_sessions(), prerequisite_ids=[1])
    assert decision.action == "new"


def test_ambiguous_prerequisites_stay_independent() -> None:
    registry = ScenarioRegistry()
    _waiting(registry, "AD-01", 1)
    _waiting(registry, "WEB-01", 1)
    decision = decide_continuation(registry.get_active_sessions(), prerequisite_ids=[1])
    assert decision.action == "new"


def test_explicit_scenario_tag_continues() -> None:
    registry = ScenarioRegistry()
    session = _waiting(registry, "AD-01", 1, tags=["scenario:ad-chain"])
    decision = decide_continuation(
        registry.get_active_sessions(),
        prerequisite_ids=[],
        tags=["scenario:ad-chain", "Windows"],
    )
    assert decision.action == "continue"
    assert decision.session_id == session.id


def test_registry_finds_previous_challenge_and_stop_is_clean(tmp_path) -> None:
    registry = ScenarioRegistry()
    session = _waiting(registry, "AD-01", 1)
    assert registry.find_session_by_challenge(challenge_name="AD-01") is session
    assert registry.find_session_by_challenge(challenge_id=1) is session
    registry.attach_challenge(
        session,
        challenge_id=2,
        challenge_name="AD-02",
        category="Windows",
        challenge_dir="/tmp/ad-02",
        from_challenge="AD-01",
    )
    assert session.state == "running"
    assert session.current_challenge_name == "AD-02"
    assert registry.find_session_by_challenge(challenge_name="AD-01") is session
    registry.record_stage(session, "AD-01", 0.22, "initial access")
    registry.stop_session(session.id, output_dir=str(tmp_path / "scenario-writeups"))
    # output dir is created; stop twice is a no-op
    again = registry.stop_session(session.id)
    assert again is session
    assert session.state == "stopped"
    assert registry.get_active_sessions() == []
    assert "AD-01" in session.metadata["cost_summary"]
    assert "$0.22" in session.metadata["cost_summary"]


def test_parse_prerequisites_and_route_payload() -> None:
    assert parse_prerequisite_ids({"requirements": {"prerequisites": [2, "3"]}}) == [2, 3]
    parsed = parse_route_decision(
        {"action": "continue", "session_id": "scenario-1", "confidence": 0.98, "reason": "chain"}
    )
    assert parsed.action == "continue"
    assert parsed.session_id == "scenario-1"
    with pytest.raises(ValueError):
        parse_route_decision({"action": "continue", "confidence": 1, "reason": "missing session"})


def test_submission_display_does_not_treat_incorrect_as_accepted() -> None:
    assert submission_accepted('CORRECT — "flag" accepted.')
    assert submission_accepted("ALREADY SOLVED — done")
    assert not submission_accepted('INCORRECT — "flag" rejected.')
    assert not submission_accepted("RETRYABLE_ERROR — safe to retry. timed out")


def test_http_failures_are_retryable_and_missing_challenge_is_fatal() -> None:
    request = httpx.Request("POST", "https://ctf.example/api/v1/challenges/attempt")
    response = httpx.Response(401, request=request)
    unauthorized = httpx.HTTPStatusError("unauthorized", request=request, response=response)
    assert classify_exception(unauthorized) == FlagOutcome.RETRYABLE_ERROR
    assert classify_exception(httpx.ConnectError("down")) == FlagOutcome.RETRYABLE_ERROR
    missing = httpx.HTTPStatusError(
        "missing",
        request=request,
        response=httpx.Response(404, request=request),
    )
    assert classify_exception(missing) == FlagOutcome.FATAL_ERROR


@pytest.mark.asyncio
async def test_retryable_submission_is_not_cached() -> None:
    request = httpx.Request("POST", "https://ctf.example/api/v1/challenges/attempt")
    error = httpx.HTTPStatusError(
        "unavailable",
        request=request,
        response=httpx.Response(503, request=request),
    )
    ctfd = _SubmitCTFd(error)
    swarm = _swarm(ctfd)

    first, confirmed = await swarm.try_submit_flag("flag{one}", "codex/gpt-5.6-sol")
    second, confirmed_again = await swarm.try_submit_flag("flag{one}", "codex/gpt-5.6-sol")

    assert confirmed is False
    assert confirmed_again is False
    assert first.startswith("RETRYABLE_ERROR")
    assert second.startswith("RETRYABLE_ERROR")
    assert "flag{one}" not in swarm._submitted_flags
    assert ctfd.calls == 2
    assert swarm._submit_count == {}


@pytest.mark.asyncio
async def test_explicit_rejection_is_cached_and_accept_is_not_retried() -> None:
    rejected = _SubmitCTFd(SubmitResult("incorrect", "nope", 'INCORRECT — "flag{no}" rejected.'))
    swarm = _swarm(rejected)
    display, confirmed = await swarm.try_submit_flag("flag{no}", "codex/gpt-5.6-sol")
    assert confirmed is False
    assert display.startswith("INCORRECT")
    again, _ = await swarm.try_submit_flag("flag{no}", "codex/gpt-5.6-sol")
    assert "already tried" in again
    assert rejected.calls == 1

    accepted = _SubmitCTFd(SubmitResult("correct", "ok", 'CORRECT — "flag{yes}" accepted.'))
    swarm = _swarm(accepted)
    display, confirmed = await swarm.try_submit_flag("flag{yes}", "codex/gpt-5.6-sol")
    assert confirmed is True
    assert display.startswith("CORRECT")
    display, confirmed = await swarm.try_submit_flag("flag{other}", "codex/gpt-5.6-sol")
    assert confirmed is True
    assert display.startswith("ALREADY SOLVED")
    assert accepted.calls == 1


@pytest.mark.asyncio
async def test_submit_flag_candidate_classifies_ctfd_statuses() -> None:
    unknown = _SubmitCTFd(SubmitResult("ratelimited", "slow down", "Unknown status: ratelimited"))
    submission = await submit_flag_candidate(unknown, "AD-01", "flag{x}")
    assert submission.outcome == FlagOutcome.RETRYABLE_ERROR
    incorrect = _SubmitCTFd(SubmitResult("incorrect", "no", "INCORRECT — no"))
    submission = await submit_flag_candidate(incorrect, "AD-01", "flag{x}")
    assert submission.outcome == FlagOutcome.REJECTED


@pytest.mark.asyncio
async def test_offer_before_wait_reuses_the_same_swarm() -> None:
    swarm = _swarm(_SubmitCTFd(SubmitResult("incorrect", "", "INCORRECT")))
    swarm.scenario_mode = True
    meta = ChallengeMeta(name="AD-02", category="Windows", id=2)
    assert swarm.offer_continuation(meta, "/tmp/ad-02")
    continuation = await asyncio.wait_for(swarm.wait_for_continuation(), timeout=1)
    assert continuation is not None
    assert continuation[0].name == "AD-02"
    assert continuation[1] == "/tmp/ad-02"


@pytest.mark.asyncio
async def test_unlock_wait_expires() -> None:
    swarm = _swarm(_SubmitCTFd(SubmitResult("incorrect", "", "INCORRECT")))
    swarm.scenario_mode = True
    assert await swarm.wait_for_continuation() is None


@pytest.mark.asyncio
async def test_refresh_reports_unlocked_challenge_once() -> None:
    class _CTFd:
        def __init__(self):
            self.stubs = [{"name": "AD-01", "id": 1, "type": "standard", "category": "Windows"}]
            self.solved: set[str] = set()

        async def fetch_challenge_stubs(self):
            return list(self.stubs)

        async def fetch_solved_names(self):
            return set(self.solved)

    ctfd = _CTFd()
    poller = CTFdPoller(ctfd=ctfd)
    await poller._seed()
    ctfd.stubs.append({"name": "AD-02", "id": 2, "type": "standard", "category": "Windows"})
    ctfd.solved.add("AD-01")
    events = await poller.refresh()
    kinds = {(event.kind, event.challenge_name) for event in events}
    assert ("new_challenge", "AD-02") in kinds
    assert ("challenge_solved", "AD-01") in kinds
    unlocked = next(event for event in events if event.challenge_name == "AD-02")
    assert unlocked.details["id"] == 2
    assert await poller.refresh() == []


def test_stage_challenge_preserves_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("keep me", encoding="utf-8")
    challenge = tmp_path / "ad-02"
    (challenge / "distfiles").mkdir(parents=True)
    (challenge / "distfiles" / "payload.bin").write_bytes(b"abc")
    (challenge / "metadata.yml").write_text("name: AD-02\n", encoding="utf-8")

    container_path = stage_challenge_into_workspace(str(workspace), str(challenge), "AD-02")

    assert container_path == "/challenge/workspace/stages/ad-02"
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (workspace / "stages" / "ad-02" / "distfiles" / "payload.bin").read_bytes() == b"abc"
    assert (workspace / "current-challenge").is_symlink()


def test_continuation_prompt_does_not_repeat_a_previous_flag() -> None:
    prompt = build_continuation_prompt(
        ChallengeMeta(name="AD-02", category="Windows", description="Move north."),
        "/challenge/workspace/stages/ad-02",
        ["loot.txt"],
    )
    assert "CURRENT position" in prompt
    assert "AD-02" in prompt
    assert "/challenge/workspace/stages/ad-02/distfiles/loot.txt" in prompt
    assert "flag{" not in prompt


def test_coordinator_llm_has_no_submit_flag_tool() -> None:
    from backend.agents.codex_coordinator import COORDINATOR_TOOLS

    assert "submit_flag" not in {tool["name"] for tool in COORDINATOR_TOOLS}


@pytest.mark.asyncio
async def test_coordinator_submit_without_swarm_does_not_call_ctfd() -> None:
    from backend.agents.coordinator_core import do_submit_flag
    from backend.deps import CoordinatorDeps

    ctfd = _SubmitCTFd(SubmitResult("correct", "ok", 'CORRECT — "flag{ad1}" accepted.'))
    deps = CoordinatorDeps(
        ctfd=ctfd,
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(scenario_unlock_wait_seconds=0.01),
        model_specs=["codex/gpt-5.6-sol"],
    )
    display = await do_submit_flag(deps, "AD-01", "flag{ad1}")
    assert display.startswith("REFUSED")
    assert ctfd.calls == 0


@pytest.mark.asyncio
async def test_coordinator_submit_routes_through_swarm_lifecycle() -> None:
    from backend.agents.coordinator_core import _attach_scenario, do_submit_flag
    from backend.deps import CoordinatorDeps

    ctfd = _SubmitCTFd(SubmitResult("correct", "ok", 'CORRECT — "flag{ad1}" accepted.'))
    swarm = _swarm(ctfd)
    deps = CoordinatorDeps(
        ctfd=ctfd,
        cost_tracker=CostTracker(),
        settings=swarm.settings,
        model_specs=list(swarm.model_specs),
    )
    _attach_scenario(deps, swarm, swarm.meta)
    deps.swarms["AD-01"] = swarm

    display = await do_submit_flag(deps, "AD-01", "flag{ad1}")

    assert display.startswith("CORRECT")
    assert swarm.scenario_session is not None
    assert swarm.scenario_session.state == "waiting_for_unlock"
    assert swarm.scenario_session.solved_challenge_names == ["AD-01"]
    assert ctfd.calls == 1


def test_prepare_next_stage_clears_current_winner_not_history() -> None:
    from backend.solver_base import FLAG_FOUND, SolverResult

    registry = ScenarioRegistry()
    swarm = _swarm(_SubmitCTFd(SubmitResult("incorrect", "", "INCORRECT")))
    session = registry.create_session(
        current_challenge_id=1,
        current_challenge_name="AD-01",
        category="Windows",
    )
    registry.record_stage(session, "AD-01", 0.22, "initial access", step_count=4)
    swarm.scenario_session = session
    swarm.winner = SolverResult(
        flag="flag{ad1}",
        status=FLAG_FOUND,
        findings_summary="done",
        step_count=4,
        cost_usd=0.22,
        log_path="",
    )
    swarm._winner_spec = "codex/gpt-5.6-sol"
    swarm.confirmed_flag = "flag{ad1}"

    swarm._prepare_next_stage(ChallengeMeta(name="AD-02", category="Windows", id=2), "/tmp/ad-02")

    assert swarm.meta.name == "AD-02"
    assert swarm.winner is None
    assert swarm._winner_spec is None
    assert swarm.confirmed_flag is None
    assert swarm.get_status()["winner"] is None
    assert session.metadata["stage_costs"][0]["challenge"] == "AD-01"
    assert session.metadata["stage_costs"][0]["cost_usd"] == 0.22


def test_solved_by_me_empty_set_is_legitimate() -> None:
    from backend.ctfd import solved_names_from_stubs

    stubs = [
        {"name": "AD-01", "id": 1, "solved_by_me": False},
        {"name": "WEB-01", "id": 9, "solved_by_me": False},
    ]
    assert solved_names_from_stubs(stubs) == set()
    assert solved_names_from_stubs([{"name": "AD-01", "id": 1}]) is None
    assert solved_names_from_stubs([]) is None


@pytest.mark.asyncio
async def test_fetch_solved_names_uses_solved_by_me_when_users_me_fails() -> None:
    from backend.ctfd import CTFdClient

    class TokenCTFd(CTFdClient):
        async def _get(self, path: str):
            if path.startswith("/challenges/") or path.startswith("/users/me"):
                raise RuntimeError("redirect to /login")
            if path.startswith("/challenges"):
                return {
                    "success": True,
                    "data": [
                        {
                            "name": "AD-01",
                            "id": 1,
                            "type": "standard",
                            "solved_by_me": True,
                        },
                        {
                            "name": "WEB-01",
                            "id": 9,
                            "type": "standard",
                            "solved_by_me": False,
                        },
                    ],
                }
            raise AssertionError(path)

    solved = await TokenCTFd().fetch_solved_names()
    assert solved == {"AD-01"}


@pytest.mark.asyncio
async def test_fetch_solved_names_falls_back_when_solved_by_me_absent() -> None:
    from backend.ctfd import CTFdClient

    class ProfileCTFd(CTFdClient):
        async def _get(self, path: str):
            if path.startswith("/challenges"):
                return {
                    "success": True,
                    "data": [{"name": "AD-01", "id": 1, "type": "standard"}],
                }
            if path.startswith("/users/me"):
                return {"success": True, "data": {"id": 7, "team_id": None}}
            if path.startswith("/users/7/solves"):
                return {
                    "success": True,
                    "data": [{"challenge": {"name": "AD-01"}}],
                }
            raise AssertionError(path)

    solved = await ProfileCTFd().fetch_solved_names()
    assert solved == {"AD-01"}


@pytest.mark.asyncio
async def test_codex_continue_resets_stage_metrics_and_keeps_thread(tmp_path) -> None:
    from backend.agents.codex_solver import CodexSolver

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    challenge = tmp_path / "ad-02"
    challenge.mkdir()
    (challenge / "metadata.yml").write_text("name: AD-02\ncategory: Windows\n", encoding="utf-8")

    solver = CodexSolver(
        model_spec="codex/gpt-5.6-sol",
        challenge_dir=str(tmp_path / "ad-01"),
        meta=ChallengeMeta(name="AD-01", category="Windows", id=1),
        ctfd=object(),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox", container_memory_limit="4g"),
    )
    solver._thread_id = "THREAD_A"
    solver._proc = object()
    solver.sandbox.workspace_dir = str(workspace)
    solver._step_count = 17
    solver._cost_usd = 0.22

    await solver.continue_with_challenge(
        ChallengeMeta(name="AD-02", category="Windows", description="north", id=2),
        str(challenge),
    )

    assert solver._thread_id == "THREAD_A"
    assert solver._proc is not None
    assert solver._step_count == 0
    assert solver._cost_usd == 0.0
    assert solver.sandbox.workspace_dir == str(workspace)
    assert solver.meta.name == "AD-02"
    solver.tracer.close()

