"""Route newly visible challenges before the normal auto-spawn path.

Deterministic CTFd prerequisites continue the waiting solver. Everything else
spawns an independent swarm, which preserves single-challenge behavior.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.deps import CoordinatorDeps
from backend.scenario import ScenarioSession, trace_scenario
from backend.scenario_router import (
    RouteDecision,
    causal_continuation_decision,
    decide_continuation,
    has_explicit_relationship,
    parse_prerequisite_ids,
    scenario_tags,
)

logger = logging.getLogger(__name__)


@dataclass
class ChallengeFacts:
    id: int | None
    name: str
    category: str
    description: str
    tags: list[str]
    prerequisite_ids: list[int]


@dataclass(frozen=True)
class ImmediateUnlockContext:
    """Post-solve poller.refresh() that is still holding the routing lock."""

    source_session_id: str
    source_challenge_id: int | str | None
    source_challenge_name: str
    new_challenge_count: int


async def handle_new_challenge(
    deps: CoordinatorDeps,
    challenge_name: str,
    details: dict[str, Any] | None = None,
    *,
    unlocked: bool = False,
    causal: ImmediateUnlockContext | None = None,
) -> str:
    """Return 'continued', 'spawned', 'skipped', or 'failed'."""
    async with deps.routing_lock:
        return await handle_new_challenge_locked(
            deps,
            challenge_name,
            details or {},
            unlocked=unlocked,
            causal=causal,
        )


async def handle_new_challenge_locked(
    deps: CoordinatorDeps,
    challenge_name: str,
    details: dict[str, Any] | None = None,
    *,
    unlocked: bool = False,
    causal: ImmediateUnlockContext | None = None,
) -> str:
    """Route one challenge. Caller must hold deps.routing_lock."""
    details = details or {}
    if challenge_name in deps.handled_challenges or challenge_name in deps.swarms:
        return "skipped"

    if unlocked:
        trace_scenario("challenge_unlocked", challenge=challenge_name, challenge_id=details.get("id"))

    try:
        facts = await load_challenge_facts(deps, challenge_name, details)
    except Exception as exc:
        logger.warning("Routing facts unavailable for %s: %s", challenge_name, exc)
        facts = ChallengeFacts(
            id=details.get("id"),
            name=challenge_name,
            category=str(details.get("category") or ""),
            description="",
            tags=[],
            prerequisite_ids=[],
        )

    decision = decide_continuation(
        deps.scenario_registry.get_active_sessions(),
        prerequisite_ids=facts.prerequisite_ids,
        tags=facts.tags,
    )
    if decision.action == "new" and causal is not None:
        source = deps.scenario_registry.get(causal.source_session_id)
        fallback = causal_continuation_decision(
            source,
            new_challenge_count=causal.new_challenge_count,
            prerequisite_ids=facts.prerequisite_ids,
            tags=facts.tags,
        )
        if fallback is not None:
            decision = fallback
    _log_route_decision(decision, facts, causal)
    trace_scenario("continuation_decision", challenge=challenge_name, **decision.as_dict())

    if decision.action == "continue" and decision.session_id:
        continued = await _continue_session(deps, decision, facts)
        if continued:
            deps.handled_challenges.add(challenge_name)
            return "continued"
        logger.info("Continuation unavailable for %s; spawning an independent solver", challenge_name)
        session = deps.scenario_registry.get(decision.session_id)
        waiting = session.metadata.get("swarm") if session is not None else None
        if waiting is not None:
            waiting.finish_scenario_wait()

    from backend.agents.coordinator_core import spawn_swarm_unlocked

    result = await spawn_swarm_unlocked(deps, challenge_name)
    logger.info("Auto-spawn %s: %s", challenge_name, result[:100])
    if result.startswith("Swarm spawned") or "still running" in result:
        deps.handled_challenges.add(challenge_name)
        return "spawned"
    return "failed"


async def on_flag_accepted(deps: CoordinatorDeps, swarm: Any, challenge_name: str, flag: str) -> None:
    """Mark the scenario solved and refresh CTFd before any other spawn runs.

    Caller must hold deps.routing_lock. `flag` is accepted already; it is not logged.
    """
    del flag  # the candidate itself stays in the per-challenge writeup
    session = getattr(swarm, "scenario_session", None)
    if session is None:
        return

    challenge_id = session.current_challenge_id
    try:
        challenge_id = await deps.ctfd.get_challenge_id(challenge_name)
    except Exception:
        logger.warning("Could not resolve CTFd id for %s", challenge_name, exc_info=True)

    deps.scenario_registry.mark_challenge_solved(session, challenge_id, challenge_name)
    deps.scenario_registry.mark_waiting_for_unlock(session)

    poller = deps.poller
    if poller is None:
        await _maybe_finish_terminal_wait(deps, swarm, session)
        return
    try:
        events = await poller.refresh()
    except Exception:
        logger.warning("Immediate CTFd refresh failed after %s", challenge_name, exc_info=True)
        await _maybe_finish_terminal_wait(deps, swarm, session)
        return

    new_challenges = [event for event in events if event.kind == "new_challenge"]
    causal = ImmediateUnlockContext(
        source_session_id=session.id,
        source_challenge_id=challenge_id,
        source_challenge_name=challenge_name,
        new_challenge_count=len(new_challenges),
    )
    for event in events:
        if event.kind != "new_challenge":
            continue
        await handle_new_challenge_locked(
            deps,
            event.challenge_name,
            event.details,
            unlocked=True,
            causal=causal,
        )
    await _maybe_finish_terminal_wait(deps, swarm, session)


async def load_challenge_facts(
    deps: CoordinatorDeps,
    challenge_name: str,
    details: dict[str, Any],
) -> ChallengeFacts:
    challenge_id = details.get("id")
    if challenge_id is None:
        challenge_id = await deps.ctfd.get_challenge_id(challenge_name)
    detail = await deps.ctfd.fetch_challenge_detail(int(challenge_id))
    tags = [
        tag["value"] if isinstance(tag, dict) else str(tag)
        for tag in (detail.get("tags") or [])
    ]
    return ChallengeFacts(
        id=int(detail["id"]),
        name=detail.get("name") or challenge_name,
        category=detail.get("category") or "",
        description=detail.get("description") or "",
        tags=tags,
        prerequisite_ids=parse_prerequisite_ids(detail),
    )


async def _continue_session(
    deps: CoordinatorDeps,
    decision: RouteDecision,
    facts: ChallengeFacts,
) -> bool:
    session = deps.scenario_registry.get(decision.session_id or "")
    if session is None or session.state == "stopped":
        return False
    swarm = session.metadata.get("swarm")
    solver = session.solver
    if swarm is None or solver is None or not hasattr(solver, "continue_with_challenge"):
        return False

    from backend.agents.coordinator_core import ensure_challenge_materials

    _challenge_dir, meta = await ensure_challenge_materials(deps, facts.name)
    if facts.id is not None:
        meta.id = facts.id
    if facts.category:
        meta.category = facts.category
    if facts.tags:
        meta.tags = list(facts.tags)
    if facts.prerequisite_ids:
        meta.requirements = list(facts.prerequisite_ids)

    previous = session.current_challenge_name
    if not swarm.offer_continuation(meta, _challenge_dir):
        return False

    deps.scenario_registry.attach_challenge(
        session,
        challenge_id=facts.id,
        challenge_name=facts.name,
        category=facts.category or meta.category,
        challenge_dir=_challenge_dir,
        tags=facts.tags,
        from_challenge=previous,
    )
    _rebind_swarm(deps, previous, facts.name, swarm)
    try:
        deps.coordinator_inbox.put_nowait(
            f"SCENARIO CONTINUED: {session.id} {previous} -> {facts.name}"
        )
    except Exception:
        logger.debug("Could not notify coordinator of continuation", exc_info=True)
    return True


def _log_route_decision(
    decision: RouteDecision,
    facts: ChallengeFacts,
    causal: ImmediateUnlockContext | None,
) -> None:
    source = causal
    causal_ok = (
        causal is not None
        and causal.new_challenge_count == 1
        and not has_explicit_relationship(facts.prerequisite_ids, facts.tags)
    )
    logger.info(
        "Continuation decision for %s: %s reason=%s session_id=%s "
        "challenge_id=%s prerequisite_ids=%s scenario_tags=%s "
        "source_scenario_id=%s source_challenge_id=%s causal_fallback_candidate=%s deterministic=%s",
        facts.name,
        decision.action,
        decision.reason,
        decision.session_id,
        facts.id,
        facts.prerequisite_ids,
        sorted(scenario_tags(facts.tags)),
        source.source_session_id if source else None,
        source.source_challenge_id if source else None,
        causal_ok,
        decision.deterministic,
    )


async def inspect_unlock_horizon(deps: CoordinatorDeps, session: ScenarioSession) -> str:
    """Return 'open', 'closed', or 'unknown'. Hidden listings stay unknown."""
    includes_hidden = bool(getattr(deps.ctfd, "listing_includes_hidden", False))
    try:
        challenges = await deps.ctfd.fetch_all_challenges()
    except Exception:
        logger.debug("Unlock horizon probe failed for %s", session.id, exc_info=True)
        return "unknown"

    solved_ids: set[int] = set()
    for value in session.solved_challenge_ids:
        try:
            solved_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    solved_names = set(session.solved_challenge_names)
    session_tags = scenario_tags(session.metadata.get("tags"))

    for challenge in challenges:
        name = str(challenge.get("name") or "")
        if not name or name in solved_names:
            continue
        prereqs = parse_prerequisite_ids(challenge)
        tags = [
            tag["value"] if isinstance(tag, dict) else str(tag)
            for tag in (challenge.get("tags") or [])
        ]
        if prereqs and solved_ids & set(prereqs):
            return "open"
        if session_tags and session_tags & scenario_tags(tags):
            return "open"

    if includes_hidden:
        return "closed"
    return "unknown"


async def _maybe_finish_terminal_wait(deps: CoordinatorDeps, swarm: Any, session: ScenarioSession) -> None:
    if session.state != "waiting_for_unlock":
        return
    horizon = await inspect_unlock_horizon(deps, session)
    if horizon != "closed":
        return
    logger.info("Scenario %s is terminal; skipping unlock wait", session.id)
    finish = getattr(swarm, "finish_scenario_wait", None)
    if callable(finish):
        finish()


def _rebind_swarm(deps: CoordinatorDeps, old_name: str, new_name: str, swarm: Any) -> None:
    """Keep the same swarm task, but index it by the challenge it is solving now."""
    task = deps.swarm_tasks.get(old_name)
    if old_name != new_name and deps.swarms.get(old_name) is swarm:
        deps.swarms.pop(old_name, None)
        deps.swarm_tasks.pop(old_name, None)
    deps.swarms[new_name] = swarm
    if task is not None:
        deps.swarm_tasks[new_name] = task


def record_stage_result(deps: CoordinatorDeps, swarm: Any, challenge_name: str, result: Any) -> None:
    deps.results[challenge_name] = {
        "flag": result.flag,
        "submit": "DRY RUN" if deps.no_submit else "confirmed by solver",
        "writeup": result.writeup_path,
        "scenario_id": swarm.scenario_session.id if swarm.scenario_session else None,
        "cost_usd": result.cost_usd,
        "step_count": result.step_count,
    }
    if swarm.scenario_registry is not None and swarm.scenario_session is not None:
        swarm.scenario_registry.record_stage(
            swarm.scenario_session,
            challenge_name,
            result.cost_usd,
            result.findings_summary,
            result.writeup_path,
            step_count=getattr(result, "step_count", 0) or 0,
        )
