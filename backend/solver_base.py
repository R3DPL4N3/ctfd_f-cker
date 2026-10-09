"""Solver result type, status constants, and solver protocol — shared across all backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Status constants
FLAG_FOUND = "flag_found"
GAVE_UP = "gave_up"
CANCELLED = "cancelled"
ERROR = "error"
QUOTA_ERROR = "quota_error"

# Flag confirmation markers from CTFd. "INCORRECT" contains "CORRECT", so callers
# must use submission_accepted() instead of a raw substring test.
CORRECT_MARKERS = ("CORRECT", "ALREADY SOLVED")


def submission_accepted(display: str) -> bool:
    """True only when the control plane reported an explicit accept."""
    text = display.strip().upper()
    if text.startswith(("INCORRECT", "RETRYABLE_ERROR", "FATAL_ERROR", "COOLDOWN", "DRY RUN")):
        return False
    return text.startswith("CORRECT") or text.startswith("ALREADY SOLVED")


@dataclass
class SolverResult:
    flag: str | None
    status: str
    findings_summary: str
    step_count: int
    cost_usd: float
    log_path: str
    writeup_path: str = ""


class SolverProtocol(Protocol):
    """Common interface for all solver backends (Pydantic AI, Claude SDK, Codex)."""

    model_spec: str
    agent_name: str
    sandbox: object

    async def start(self) -> None: ...
    async def run_until_done_or_gave_up(self) -> SolverResult: ...
    def bump(self, insights: str) -> None: ...
    async def stop(self) -> None: ...
