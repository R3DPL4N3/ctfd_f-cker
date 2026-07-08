"""Helpers for launching the Codex CLI across platforms."""

from __future__ import annotations

import os
import shutil


def resolve_codex_executable() -> str:
    """Return the Codex CLI executable path, preferring the Windows npm shim."""
    candidates = ["codex.cmd", "codex"] if os.name == "nt" else ["codex"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path

    raise FileNotFoundError(
        "Codex CLI not found on PATH. Install it and run `codex login`, then retry."
    )
