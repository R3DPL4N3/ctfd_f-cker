"""Control-plane flag submission.

Solvers report candidates. This module talks to CTFd and classifies the
outcome. A transport or authentication failure is retryable and must not be
treated as an incorrect flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx

from backend.ctfd import SubmitResult


class FlagOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RETRYABLE_ERROR = "retryable_error"
    FATAL_ERROR = "fatal_error"


@dataclass(frozen=True)
class FlagSubmission:
    outcome: FlagOutcome
    display: str
    flag: str
    challenge_name: str

    @property
    def accepted(self) -> bool:
        return self.outcome == FlagOutcome.ACCEPTED


def classify_submit_result(result: SubmitResult) -> FlagOutcome:
    """Map a real CTFd attempt response. Unknown statuses are retryable."""
    if result.status in ("correct", "already_solved"):
        return FlagOutcome.ACCEPTED
    if result.status == "incorrect":
        return FlagOutcome.REJECTED
    return FlagOutcome.RETRYABLE_ERROR


def classify_exception(exc: BaseException) -> FlagOutcome:
    """Network, auth, and server failures are retryable. Missing challenges are fatal."""
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 404:
            return FlagOutcome.FATAL_ERROR
        return FlagOutcome.RETRYABLE_ERROR
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return FlagOutcome.RETRYABLE_ERROR
    text = str(exc).lower()
    if "not found" in text:
        return FlagOutcome.FATAL_ERROR
    return FlagOutcome.RETRYABLE_ERROR


async def submit_flag_candidate(ctfd, challenge_name: str, flag: str) -> FlagSubmission:
    """Submit one candidate. Callers must cache the flag only when outcome is REJECTED."""
    flag = flag.strip()
    if not flag:
        return FlagSubmission(
            outcome=FlagOutcome.FATAL_ERROR,
            display="FATAL_ERROR — empty flag.",
            flag=flag,
            challenge_name=challenge_name,
        )

    try:
        result = await ctfd.submit_flag(challenge_name, flag)
    except Exception as exc:
        outcome = classify_exception(exc)
        label = "RETRYABLE_ERROR" if outcome == FlagOutcome.RETRYABLE_ERROR else "FATAL_ERROR"
        hint = "safe to retry" if outcome == FlagOutcome.RETRYABLE_ERROR else "not a CTFd rejection"
        return FlagSubmission(
            outcome=outcome,
            display=f"{label} — {hint}. {exc}",
            flag=flag,
            challenge_name=challenge_name,
        )

    outcome = classify_submit_result(result)
    if outcome == FlagOutcome.RETRYABLE_ERROR:
        display = f"RETRYABLE_ERROR — CTFd did not explicitly reject this flag. {result.display}"
    else:
        display = result.display
    return FlagSubmission(
        outcome=outcome,
        display=display,
        flag=flag,
        challenge_name=challenge_name,
    )
