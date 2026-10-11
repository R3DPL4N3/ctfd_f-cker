"""CTFd client wrapper that exposes only coordinator-selected categories."""

from __future__ import annotations

from typing import Any

from backend.category_filter import filter_challenges
from backend.ctfd import CTFdClient


class FilteredCTFdClient(CTFdClient):
    """Apply a category whitelist to coordinator discovery.

    An empty whitelist preserves the existing behavior and exposes every category.
    Direct challenge detail and submission APIs are unchanged; discovery and spawn
    guards are the enforcement boundary.
    """

    def __init__(self, *args: Any, allowed_categories: list[str] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.allowed_categories = list(allowed_categories or [])

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        challenges = await super().fetch_challenge_stubs()
        return filter_challenges(self.allowed_categories, challenges)

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        challenges: list[dict[str, Any]] = []
        for stub in await self.fetch_challenge_stubs():
            detail = await self.fetch_challenge_detail(int(stub["id"]))
            challenges.append(detail)
        return filter_challenges(self.allowed_categories, challenges)
