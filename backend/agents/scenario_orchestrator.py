"""Route newly visible challenges before the normal auto-spawn path.

Deterministic CTFd prerequisites continue the waiting solver. Everything else
spawns an independent swarm, which preserves single-challenge behavior.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.deps import CoordinatorDeps
from backend.scenario import trace_scenario
from backend.scenario_router import RouteDecision, decide_continuation, parse_prerequisite_ids

logger = logging.getLogger(__name__)


@dataclass
class ChallengeFacts:
    id: int | None
    name: str
    category: str
    description: str
    tags: list[str]
    prerequisite_ids: list[int]


async def handle_new_challenge(
    deps: CoordinatorDeps,
    challenge_name: str,
    details: dict[str, Any] | None = None,
    *,
    unlocked: bool = False,
) -> str:
    """Return 'continued', 'spawned', 'skipped', or 'failed'."""
    async with deps.routing_lock:
        return await handle_new_challenge_locked(
            deps,
            challenge_name,
            details or {},
            unlocked=unlocked,
        )


async def handle_new_challenge_locked(
    deps: CoordinatorDeps,
    challenge_name: str,
    details: dict[str, Any] | None = None,
    *,
    unlocked: bool = False,
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
    trace_scenario("continuation_decision", challenge=challenge_name, **decision.as_dict())
    logger.info(
        "Continuation decision for %s: %s (%s)",
        challenge_name,
        decision.action,
        decision.reason,
    )

    if decision.action == "continue" and decision.session_id:
        continued = await _continue_session(deps, decision, facts)
        if continued:
            deps.handled_challenges.add(challenge_name)
            return "continued"
        logger.info("Continuation unavailable for %s; spawning an independent solver", challenge_name)

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
        return
    try:
        events = await poller.refresh()
    except Exception:
        logger.warning("Immediate CTFd refresh failed after %s", challenge_name, exc_info=True)
        return

    for event in events:
        if event.kind != "new_challenge":
            continue
        await handle_new_challenge_locked(
            deps,
            event.challenge_name,
            event.details,
            unlocked=True,
        )


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
    }
    if swarm.scenario_registry is not None and swarm.scenario_session is not None:
        swarm.scenario_registry.record_stage(
            swarm.scenario_session,
            challenge_name,
            result.cost_usd,
            result.findings_summary,
            result.writeup_path,
        )
