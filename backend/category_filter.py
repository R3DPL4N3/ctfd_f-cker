"""Category whitelist helpers for coordinator challenge discovery."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def normalize_category(value: str | None) -> str:
    """Normalize a CTFd category for case-insensitive comparisons."""
    return " ".join(str(value or "").split()).casefold()


def normalize_categories(values: Iterable[str] | None) -> list[str]:
    """Return a de-duplicated display list while preserving user order."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = " ".join(str(raw).split())
        key = normalize_category(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def category_allowed(allowed_categories: Iterable[str] | None, category: str | None) -> bool:
    """Empty whitelist means allow all; otherwise compare categories case-insensitively."""
    allowed = {normalize_category(item) for item in allowed_categories or [] if normalize_category(item)}
    if not allowed:
        return True
    return normalize_category(category) in allowed


def filter_challenges(allowed_categories: Iterable[str] | None, challenges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter CTFd challenge dictionaries by the configured category whitelist."""
    if not list(allowed_categories or []):
        return challenges
    return [
        challenge
        for challenge in challenges
        if category_allowed(allowed_categories, challenge.get("category"))
    ]
