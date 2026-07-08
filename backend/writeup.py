"""Generate challenge writeups from solver traces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.prompts import ChallengeMeta
from backend.solver_base import SolverResult


def write_solve_writeup(
    challenge_dir: str | Path,
    meta: ChallengeMeta,
    result: SolverResult,
    model_spec: str,
) -> Path:
    """Write a Markdown solve note as soon as a flag is confirmed."""
    out_path = Path(challenge_dir) / "writeup.md"
    trace_events = _read_trace_events(result.log_path)
    timeline = _format_timeline(trace_events)

    lines = [
        f"# {meta.name} Writeup",
        "",
        "## Summary",
        f"- Category: {meta.category or 'Unknown'}",
        f"- Points: {meta.value or '?'}",
        f"- Solver: `{model_spec}`",
        f"- Flag: `{result.flag or 'unknown'}`",
        "",
        "## Challenge",
        meta.description.strip() or "_No description provided._",
        "",
        "## Solve Notes",
        result.findings_summary.strip() or "_No solver summary captured._",
        "",
        "## Reproduction Timeline",
        timeline or "_No trace events captured._",
        "",
        "## Artifacts",
        f"- Solver trace: `{result.log_path}`" if result.log_path else "- Solver trace: _not captured_",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _read_trace_events(log_path: str) -> list[dict[str, Any]]:
    if not log_path:
        return []

    path = Path(log_path)
    if not path.exists():
        return []

    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _format_timeline(events: list[dict[str, Any]], limit: int = 40) -> str:
    interesting = [
        event
        for event in events
        if event.get("type") in {"tool_call", "tool_result", "flag_confirmed", "finish", "turn_failed"}
    ]
    if not interesting:
        return ""

    selected = interesting[-limit:]
    lines: list[str] = []
    for event in selected:
        kind = event.get("type", "?")
        if kind == "tool_call":
            step = event.get("step", "?")
            tool = event.get("tool", "?")
            args = _truncate(str(event.get("args", "")))
            lines.append(f"- Step {step}: called `{tool}` with `{args}`")
        elif kind == "tool_result":
            step = event.get("step", "?")
            tool = event.get("tool", "?")
            result = _truncate(str(event.get("result", "")))
            lines.append(f"- Step {step}: `{tool}` returned `{result}`")
        elif kind == "flag_confirmed":
            lines.append(f"- Flag confirmed: `{event.get('flag', '')}`")
        elif kind == "finish":
            status = event.get("status", "?")
            flag = event.get("flag") or ""
            lines.append(f"- Solver finished with `{status}`" + (f" and flag `{flag}`" if flag else ""))
        elif kind == "turn_failed":
            lines.append(f"- Turn failed: `{_truncate(str(event.get('error', 'unknown')))}`")
    return "\n".join(lines)


def _truncate(value: str, limit: int = 240) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
