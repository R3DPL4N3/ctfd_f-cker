"""Deterministic continuation routing.

A direct CTFd prerequisite, or an explicit scenario/chain tag, continues the
waiting session. Same category is not a relationship. Ambiguous cases stay
independent until a coordinator model is asked to judge them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.scenario import ScenarioSession


@dataclass(frozen=True)
class RouteDecision:
    action: str  # "continue" | "new"
    session_id: str | None = None
    confidence: float = 0.0
    reason: str = ""
    deterministic: bool = True

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "confidence": self.confidence,
            "reason": self.reason,
        }
        if self.action == "continue" and self.session_id:
            payload["session_id"] = self.session_id
        return payload


def parse_route_decision(payload: dict[str, Any]) -> RouteDecision:
    """Parse the structured continue/new decision used by the control plane."""
    action = payload.get("action")
    if action not in ("continue", "new"):
        raise ValueError(f"invalid routing action: {action!r}")
    session_id = payload.get("session_id")
    if action == "continue" and not session_id:
        raise ValueError("continue requires session_id")
    return RouteDecision(
        action=action,
        session_id=str(session_id) if session_id else None,
        confidence=float(payload.get("confidence", 0)),
        reason=str(payload.get("reason", "")),
        deterministic=bool(payload.get("deterministic", False)),
    )


def parse_prerequisite_ids(challenge: dict[str, Any] | None) -> list[int]:
    """Read CTFd prerequisite challenge ids from a challenge payload."""
    if not challenge:
        return []
    raw = challenge.get("requirements")
    if isinstance(raw, str):
        return []
    if isinstance(raw, dict):
        raw = raw.get("prerequisites") or []
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("id")
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def scenario_tags(tags: list[Any] | None) -> set[str]:
    """Explicit chain tags only. Plain category tags are ignored."""
    found: set[str] = set()
    for tag in tags or []:
        text = str(tag).strip().lower()
        if text.startswith("scenario:") or text.startswith("chain:"):
            found.add(text)
    return found


def _solved_ids(session: ScenarioSession) -> set[int]:
    values = list(session.solved_challenge_ids)
    if session.state == "waiting_for_unlock" and session.current_challenge_id is not None:
        values.append(session.current_challenge_id)
    solved: set[int] = set()
    for value in values:
        try:
            solved.add(int(value))
        except (TypeError, ValueError):
            continue
    return solved


def decide_continuation(
    sessions: list[ScenarioSession],
    *,
    prerequisite_ids: list[int],
    tags: list[Any] | None = None,
) -> RouteDecision:
    """Continue only on a direct prerequisite or an explicit scenario tag.

    Category, description wording, and timing are not enough on their own.
    Those signals are reserved for a later coordinator decision.
    """
    waiting = [session for session in sessions if session.state == "waiting_for_unlock"]
    prereq_ids = set(prerequisite_ids)

    prereq_matches = [
        session for session in waiting if prereq_ids and (_solved_ids(session) & prereq_ids)
    ]
    if len(prereq_matches) == 1:
        session = prereq_matches[0]
        return RouteDecision(
            action="continue",
            session_id=session.id,
            confidence=1.0,
            reason="CTFd prerequisite matches the waiting scenario",
            deterministic=True,
        )
    if len(prereq_matches) > 1:
        return RouteDecision(
            action="new",
            confidence=0.5,
            reason="multiple waiting scenarios match the prerequisite",
            deterministic=True,
        )

    new_tags = scenario_tags(tags)
    if new_tags:
        tag_matches = [
            session
            for session in waiting
            if new_tags & scenario_tags(session.metadata.get("tags"))
        ]
        if len(tag_matches) == 1:
            session = tag_matches[0]
            return RouteDecision(
                action="continue",
                session_id=session.id,
                confidence=0.99,
                reason="explicit scenario tag matches the waiting scenario",
                deterministic=True,
            )
        if len(tag_matches) > 1:
            return RouteDecision(
                action="new",
                confidence=0.4,
                reason="multiple waiting scenarios share the scenario tag",
                deterministic=True,
            )

    return RouteDecision(
        action="new",
        confidence=0.9,
        reason="no deterministic scenario relationship",
        deterministic=True,
    )


def has_explicit_relationship(prerequisite_ids: list[int], tags: list[Any] | None = None) -> bool:
    """True when CTFd prerequisites or scenario/chain tags can route the challenge."""
    return bool(prerequisite_ids) or bool(scenario_tags(tags))


def causal_continuation_decision(
    session: ScenarioSession | None,
    *,
    new_challenge_count: int,
    prerequisite_ids: list[int],
    tags: list[Any] | None = None,
) -> RouteDecision | None:
    """Continue a waiting session when exactly one challenge appears after a solve.

    Returns None when causal fallback must not override explicit routing.
    """
    if has_explicit_relationship(prerequisite_ids, tags):
        return None
    if new_challenge_count != 1:
        return None
    if session is None or session.state != "waiting_for_unlock":
        return None
    return RouteDecision(
        action="continue",
        session_id=session.id,
        confidence=0.85,
        reason="single challenge appeared in immediate post-solve refresh",
        deterministic=True,
    )
