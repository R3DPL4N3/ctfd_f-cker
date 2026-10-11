"""Scenario sessions — one persistent solver across a challenge chain.

The registry is solver-agnostic. Codex, Claude, or a later API-backed solver
can occupy `ScenarioSession.solver` as long as the orchestrator can keep that
object alive.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOG_PATH = Path("logs") / "scenario-events.jsonl"


def trace_scenario(kind: str, session: Any | None = None, **kwargs: Any) -> None:
    """Append a scenario event to the shared JSONL log and the solver trace."""
    payload: dict[str, Any] = {"type": kind, "ts": time.time(), **kwargs}
    if session is not None and "session_id" not in payload:
        payload["session_id"] = getattr(session, "id", None)
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        logger.debug("scenario trace write failed", exc_info=True)

    solver = getattr(session, "solver", None) if session is not None else None
    tracer = getattr(solver, "tracer", None) if solver is not None else None
    if tracer is not None:
        event_fields = {key: value for key, value in payload.items() if key not in {"type", "ts"}}
        try:
            tracer.event(kind, **event_fields)
        except Exception:
            logger.debug("solver scenario trace failed", exc_info=True)


@dataclass
class ScenarioSession:
    id: str
    solver: Any
    current_challenge_id: int | str | None
    current_challenge_name: str
    category: str | None
    solved_challenge_ids: list[int | str] = field(default_factory=list)
    solved_challenge_names: list[str] = field(default_factory=list)
    state: str = "running"
    sandbox_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ScenarioRegistry:
    """In-memory registry of scenario sessions for one competition run."""

    def __init__(self) -> None:
        self._sessions: dict[str, ScenarioSession] = {}
        self._seq = 0

    def create_session(
        self,
        *,
        solver: Any = None,
        current_challenge_id: int | str | None = None,
        current_challenge_name: str,
        category: str | None = None,
        sandbox_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ScenarioSession:
        self._seq += 1
        session = ScenarioSession(
            id=f"scenario-{self._seq}",
            solver=solver,
            current_challenge_id=current_challenge_id,
            current_challenge_name=current_challenge_name,
            category=category,
            sandbox_id=sandbox_id,
            metadata=dict(metadata or {}),
        )
        self._sessions[session.id] = session
        trace_scenario(
            "scenario_created",
            session,
            challenge=current_challenge_name,
            challenge_id=current_challenge_id,
            category=category,
        )
        logger.info("Scenario %s created for %s", session.id, current_challenge_name)
        return session

    def get(self, session_id: str) -> ScenarioSession | None:
        return self._sessions.get(session_id)

    def get_active_sessions(self) -> list[ScenarioSession]:
        return [session for session in self._sessions.values() if session.state != "stopped"]

    def find_session_by_challenge(
        self,
        *,
        challenge_id: int | str | None = None,
        challenge_name: str | None = None,
    ) -> ScenarioSession | None:
        """Find an active session by its current challenge or one it already solved."""
        for session in self.get_active_sessions():
            if challenge_name and (
                session.current_challenge_name == challenge_name
                or challenge_name in session.solved_challenge_names
            ):
                return session
            if challenge_id is not None and (
                session.current_challenge_id == challenge_id
                or challenge_id in session.solved_challenge_ids
            ):
                return session
        return None

    def mark_challenge_solved(
        self,
        session: ScenarioSession,
        challenge_id: int | str | None,
        challenge_name: str,
    ) -> None:
        if challenge_id is not None and challenge_id not in session.solved_challenge_ids:
            session.solved_challenge_ids.append(challenge_id)
        if challenge_name not in session.solved_challenge_names:
            session.solved_challenge_names.append(challenge_name)
        if challenge_id is not None:
            session.current_challenge_id = challenge_id
        session.current_challenge_name = challenge_name
        trace_scenario(
            "flag_accepted",
            session,
            challenge=challenge_name,
            challenge_id=challenge_id,
        )

    def mark_waiting_for_unlock(self, session: ScenarioSession) -> None:
        session.state = "waiting_for_unlock"
        session.metadata["waiting_since"] = time.time()
        trace_scenario(
            "waiting_for_unlock",
            session,
            challenge=session.current_challenge_name,
            challenge_id=session.current_challenge_id,
        )
        logger.info(
            "Scenario %s waiting for unlock after %s",
            session.id,
            session.current_challenge_name,
        )

    def attach_challenge(
        self,
        session: ScenarioSession,
        *,
        challenge_id: int | str | None,
        challenge_name: str,
        category: str | None,
        challenge_dir: str,
        tags: list[Any] | None = None,
        from_challenge: str | None = None,
    ) -> None:
        previous = from_challenge or session.current_challenge_name
        session.current_challenge_id = challenge_id
        session.current_challenge_name = challenge_name
        session.category = category
        session.state = "running"
        dirs = session.metadata.setdefault("challenge_dirs", {})
        dirs[challenge_name] = challenge_dir
        if tags:
            existing = list(session.metadata.get("tags") or [])
            for tag in tags:
                if tag not in existing:
                    existing.append(tag)
            session.metadata["tags"] = existing
        trace_scenario(
            "scenario_continued",
            session,
            from_challenge=previous,
            to_challenge=challenge_name,
            challenge_id=challenge_id,
        )
        logger.info(
            "Scenario %s continued from %s to %s",
            session.id,
            previous,
            challenge_name,
        )

    def record_stage(
        self,
        session: ScenarioSession,
        challenge_name: str,
        cost_usd: float,
        findings: str = "",
        writeup_path: str = "",
        step_count: int = 0,
    ) -> None:
        stages: list[dict[str, Any]] = session.metadata.setdefault("stage_costs", [])
        entry = {
            "challenge": challenge_name,
            "cost_usd": float(cost_usd),
            "step_count": int(step_count),
            "findings": findings,
            "writeup": writeup_path,
        }
        for index, existing in enumerate(stages):
            if existing.get("challenge") == challenge_name:
                stages[index] = entry
                return
        stages.append(entry)

    def stop_session(self, session_id: str, output_dir: str = "scenario-writeups") -> ScenarioSession | None:
        session = self._sessions.get(session_id)
        if session is None or session.state == "stopped":
            return session
        session.state = "stopped"
        if not session.solved_challenge_names and not session.metadata.get("stage_costs"):
            trace_scenario("scenario_stopped", session)
            logger.info("Scenario %s stopped without a solved stage", session.id)
            return session
        summary = format_scenario_cost(session)
        session.metadata["cost_summary"] = summary
        try:
            from backend.writeup import write_scenario_writeup

            path = write_scenario_writeup(session, output_dir)
            session.metadata["scenario_writeup"] = str(path)
        except Exception:
            logger.warning("Scenario writeup failed for %s", session.id, exc_info=True)
        trace_scenario("scenario_stopped", session, summary=summary)
        logger.info("Scenario %s stopped\n%s", session.id, summary)
        return session

    def cost_summaries(self) -> list[str]:
        return [
            format_scenario_cost(session)
            for session in self._sessions.values()
            if session.metadata.get("stage_costs")
        ]


def format_scenario_cost(session: ScenarioSession) -> str:
    """Per-stage and total scenario cost."""
    title = _scenario_title(session)
    lines = [f"Scenario: {title}", ""]
    total = 0.0
    stages = session.metadata.get("stage_costs") or []
    if not stages:
        for name in session.solved_challenge_names:
            lines.extend([name, "$0.00", ""])
    for stage in stages:
        cost = float(stage.get("cost_usd") or 0.0)
        total += cost
        steps = int(stage.get("step_count") or 0)
        lines.extend([
            str(stage.get("challenge") or "?"),
            f"steps: {steps}",
            f"${cost:.2f}",
            "",
        ])
    lines.extend(["Scenario total:", f"${total:.2f}"])
    return "\n".join(lines)


def _scenario_title(session: ScenarioSession) -> str:
    for tag in session.metadata.get("tags") or []:
        text = str(tag).strip()
        lowered = text.lower()
        if lowered.startswith("scenario:") or lowered.startswith("chain:"):
            return text.split(":", 1)[1].strip() or session.id
    names = session.solved_challenge_names or [session.current_challenge_name]
    if len(names) > 1 and session.category:
        return f"{session.category} chain"
    return names[0] if names else session.id
