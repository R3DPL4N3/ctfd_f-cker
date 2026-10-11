"""Persistent scenario memory — JSON on the sandbox workspace, markdown projection.

The workspace files are the source of truth. Conversation history is not.
Secrets may live in JSON; markdown and logs must not echo them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILENAME = "scenario-state.json"
MARKDOWN_FILENAME = "scenario-memory.md"
CONTAINER_STATE = "/challenge/workspace/scenario-state.json"
CONTAINER_MARKDOWN = "/challenge/workspace/scenario-memory.md"
PROMPT_MEMORY_MAX_BYTES = 10_240


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _notes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _merge_notes(existing: list[str], incoming: list[str]) -> list[str]:
    merged = list(existing)
    seen = {note.lower() for note in existing}
    for note in incoming:
        key = note.lower()
        if key not in seen:
            merged.append(note)
            seen.add(key)
    return merged


def _as_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


@dataclass
class Target:
    host: str
    hostname: str | None = None
    domain: str | None = None
    notes: list[str] = field(default_factory=list)

    def key(self) -> str:
        return _norm(self.host).lower()


@dataclass
class Credential:
    username: str
    domain: str | None = None
    password: str | None = None
    hash: str | None = None
    source: str | None = None
    notes: list[str] = field(default_factory=list)

    def key(self) -> tuple[str, str]:
        return (_norm(self.username).lower(), _norm(self.domain).lower())


@dataclass
class AccessSession:
    type: str
    host: str
    username: str | None = None
    notes: list[str] = field(default_factory=list)

    def key(self) -> tuple[str, str, str]:
        return (_norm(self.type).lower(), _norm(self.host).lower(), _norm(self.username).lower())


@dataclass
class Network:
    cidr: str
    notes: list[str] = field(default_factory=list)

    def key(self) -> str:
        return _norm(self.cidr).lower()


@dataclass
class ScenarioState:
    scenario_id: str = ""
    current_stage: str = ""
    completed_stages: list[str] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    credentials: list[Credential] = field(default_factory=list)
    sessions: list[AccessSession] = field(default_factory=list)
    networks: list[Network] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def empty_state(*, scenario_id: str = "", current_stage: str = "") -> ScenarioState:
    return ScenarioState(
        scenario_id=scenario_id,
        current_stage=current_stage,
        updated_at=_now(),
    )


def state_from_dict(data: dict[str, Any], *, scenario_id: str = "", current_stage: str = "") -> ScenarioState:
    return ScenarioState(
        scenario_id=_norm(data.get("scenario_id")) or scenario_id,
        current_stage=_norm(data.get("current_stage")) or current_stage,
        completed_stages=_unique_strings(data.get("completed_stages")),
        targets=_merge_targets([], data.get("targets")),
        credentials=_merge_credentials([], data.get("credentials")),
        sessions=_merge_sessions([], data.get("sessions")),
        networks=_merge_networks([], data.get("networks")),
        findings=_merge_strings([], data.get("findings")),
        artifacts=_merge_strings([], data.get("artifacts")),
        updated_at=_norm(data.get("updated_at")) or _now(),
    )


def _unique_strings(value: Any) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw in _as_items(value):
        text = _finding_text(raw)
        key = text.lower()
        if text and key not in seen:
            items.append(text)
            seen.add(key)
    return items


def _finding_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for key in ("text", "finding", "summary", "path", "name"):
            if raw.get(key):
                return str(raw[key]).strip()
        return json.dumps(raw, sort_keys=True)
    return str(raw).strip()


def _target_from(raw: Any) -> Target | None:
    if isinstance(raw, str):
        host = raw.strip()
        return Target(host=host) if host else None
    if not isinstance(raw, dict):
        return None
    host = _norm(raw.get("host") or raw.get("ip") or raw.get("address"))
    if not host:
        return None
    return Target(
        host=host,
        hostname=_norm(raw.get("hostname")) or None,
        domain=_norm(raw.get("domain")) or None,
        notes=_notes(raw.get("notes")),
    )


def _credential_from(raw: Any) -> Credential | None:
    if not isinstance(raw, dict):
        return None
    username = _norm(raw.get("username") or raw.get("user"))
    if not username:
        return None
    return Credential(
        username=username,
        domain=_norm(raw.get("domain")) or None,
        password=_norm(raw.get("password")) or None,
        hash=_norm(raw.get("hash") or raw.get("ntlm") or raw.get("nthash")) or None,
        source=_norm(raw.get("source")) or None,
        notes=_notes(raw.get("notes")),
    )


def _session_from(raw: Any) -> AccessSession | None:
    if not isinstance(raw, dict):
        return None
    kind = _norm(raw.get("type") or raw.get("protocol"))
    host = _norm(raw.get("host") or raw.get("ip"))
    if not kind or not host:
        return None
    return AccessSession(
        type=kind,
        host=host,
        username=_norm(raw.get("username") or raw.get("user")) or None,
        notes=_notes(raw.get("notes")),
    )


def _network_from(raw: Any) -> Network | None:
    if isinstance(raw, str):
        cidr = raw.strip()
        return Network(cidr=cidr) if cidr else None
    if not isinstance(raw, dict):
        return None
    cidr = _norm(raw.get("cidr") or raw.get("network") or raw.get("range"))
    if not cidr:
        return None
    return Network(cidr=cidr, notes=_notes(raw.get("notes")))


def _merge_targets(existing: list[Target], incoming: Any) -> list[Target]:
    by_key = {item.key(): item for item in existing if item.key()}
    for raw in _as_items(incoming):
        item = _target_from(raw)
        if item is None or not item.key():
            continue
        current = by_key.get(item.key())
        if current is None:
            by_key[item.key()] = item
            continue
        if item.hostname:
            current.hostname = item.hostname
        if item.domain:
            current.domain = item.domain
        current.notes = _merge_notes(current.notes, item.notes)
    return list(by_key.values())


def _merge_credentials(existing: list[Credential], incoming: Any) -> list[Credential]:
    by_key = {item.key(): item for item in existing if item.username}
    for raw in _as_items(incoming):
        item = _credential_from(raw)
        if item is None:
            continue
        current = by_key.get(item.key())
        if current is None:
            by_key[item.key()] = item
            continue
        secret_updated = False
        if item.password:
            current.password = item.password
            secret_updated = True
        if item.hash:
            current.hash = item.hash
            secret_updated = True
        if item.source and (secret_updated or not current.source):
            current.source = item.source
        current.notes = _merge_notes(current.notes, item.notes)
    return list(by_key.values())


def _merge_sessions(existing: list[AccessSession], incoming: Any) -> list[AccessSession]:
    by_key = {item.key(): item for item in existing if item.host}
    for raw in _as_items(incoming):
        item = _session_from(raw)
        if item is None:
            continue
        current = by_key.get(item.key())
        if current is None:
            by_key[item.key()] = item
            continue
        current.notes = _merge_notes(current.notes, item.notes)
    return list(by_key.values())


def _merge_networks(existing: list[Network], incoming: Any) -> list[Network]:
    by_key = {item.key(): item for item in existing if item.cidr}
    for raw in _as_items(incoming):
        item = _network_from(raw)
        if item is None or not item.key():
            continue
        current = by_key.get(item.key())
        if current is None:
            by_key[item.key()] = item
            continue
        current.notes = _merge_notes(current.notes, item.notes)
    return list(by_key.values())


def _merge_strings(existing: list[str], incoming: Any) -> list[str]:
    return _unique_strings([*existing, *(_as_items(incoming))])


def merge_state(state: ScenarioState, payload: dict[str, Any]) -> ScenarioState:
    """Merge structured updates. Lists are additive with dedup, never a blind replace."""
    if payload.get("scenario_id"):
        state.scenario_id = _norm(payload["scenario_id"])
    if payload.get("current_stage"):
        state.current_stage = _norm(payload["current_stage"])
    if payload.get("completed_stages"):
        state.completed_stages = _unique_strings([*state.completed_stages, *payload["completed_stages"]])
    state.targets = _merge_targets(state.targets, payload.get("targets"))
    state.credentials = _merge_credentials(state.credentials, payload.get("credentials"))
    state.sessions = _merge_sessions(state.sessions, payload.get("sessions"))
    state.networks = _merge_networks(state.networks, payload.get("networks"))
    state.findings = _merge_strings(state.findings, payload.get("findings"))
    state.artifacts = _merge_strings(state.artifacts, payload.get("artifacts"))
    state.updated_at = _now()
    return state


def render_markdown(state: ScenarioState) -> str:
    """Human/LLM projection. Secrets are mentioned, not printed."""
    lines = ["# Scenario Memory", ""]
    lines += ["## Current stage", state.current_stage or "_none_", ""]
    lines += ["## Completed stages"]
    if state.completed_stages:
        lines.extend(f"- {name}" for name in state.completed_stages)
    else:
        lines.append("_none_")
    lines.append("")
    lines += ["## Targets"]
    if state.targets:
        for target in state.targets:
            label = target.host
            extra = [part for part in (target.hostname, target.domain) if part]
            if extra:
                label += " — " + ", ".join(extra)
            lines.append(f"- {label}")
    else:
        lines.append("_none_")
    lines.append("")
    lines += ["## Credentials"]
    if state.credentials:
        for cred in state.credentials:
            ident = f"{cred.domain}\\{cred.username}" if cred.domain else cred.username
            bits: list[str] = []
            if cred.password:
                bits.append("password available")
            if cred.hash:
                bits.append("hash available")
            suffix = " — " + " — ".join(bits) if bits else " — recorded"
            if cred.source:
                suffix += f" — source: {cred.source}"
            lines.append(f"- {ident}{suffix}")
    else:
        lines.append("_none_")
    lines.append("")
    lines += ["## Networks"]
    if state.networks:
        lines.extend(f"- {net.cidr}" for net in state.networks)
    else:
        lines.append("_none_")
    lines.append("")
    lines += ["## Sessions / Access"]
    if state.sessions:
        for session in state.sessions:
            who = f"{session.username}@" if session.username else ""
            lines.append(f"- {session.type.upper()} {who}{session.host}")
    else:
        lines.append("_none_")
    lines.append("")
    lines += ["## Findings"]
    if state.findings:
        lines.extend(f"- {item}" for item in state.findings)
    else:
        lines.append("_none_")
    lines.append("")
    lines += ["## Artifacts"]
    if state.artifacts:
        lines.extend(f"- `{item}`" for item in state.artifacts)
    else:
        lines.append("_none_")
    lines.append("")
    return "\n".join(lines)


def prompt_projection(markdown: str, max_bytes: int = PROMPT_MEMORY_MAX_BYTES) -> str:
    encoded = markdown.encode("utf-8")
    if len(encoded) <= max_bytes:
        return markdown
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return clipped + "\n\n_Memory truncated for the prompt. Full state is on disk._\n"


def memory_payload_trace_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Trace/log view of memory_update. Counts only — never payload contents."""
    summary: dict[str, Any] = {
        "targets_count": len(_as_items(payload.get("targets"))),
        "credentials_count": len(_as_items(payload.get("credentials"))),
        "sessions_count": len(_as_items(payload.get("sessions"))),
        "networks_count": len(_as_items(payload.get("networks"))),
        "findings_count": len(_as_items(payload.get("findings"))),
        "artifacts_count": len(_as_items(payload.get("artifacts"))),
    }
    stage = _norm(payload.get("current_stage"))
    if stage:
        summary["current_stage"] = stage
    return summary


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class ScenarioMemoryStore:
    """Load/save scenario memory under an existing persistent workspace directory."""

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        scenario_id: str = "",
        current_stage: str = "",
    ) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.scenario_id = scenario_id
        self.current_stage = current_stage

    @property
    def json_path(self) -> Path:
        return self.workspace_dir / STATE_FILENAME

    @property
    def markdown_path(self) -> Path:
        return self.workspace_dir / MARKDOWN_FILENAME

    def ensure(self) -> ScenarioState:
        return self.load()

    def load(self) -> ScenarioState:
        if not self.json_path.exists():
            state = empty_state(scenario_id=self.scenario_id, current_stage=self.current_stage)
            self.save(state)
            return state
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("scenario memory is not an object")
            state = state_from_dict(data, scenario_id=self.scenario_id, current_stage=self.current_stage)
            if self.scenario_id and not state.scenario_id:
                state.scenario_id = self.scenario_id
            if self.current_stage and not state.current_stage:
                state.current_stage = self.current_stage
            return state
        except Exception:
            logger.warning("Malformed scenario memory at %s — resetting", self.json_path, exc_info=True)
            try:
                corrupt = self.json_path.with_name(STATE_FILENAME + ".corrupt")
                self.json_path.replace(corrupt)
            except Exception:
                logger.debug("Could not preserve corrupt scenario memory", exc_info=True)
            state = empty_state(scenario_id=self.scenario_id, current_stage=self.current_stage)
            self.save(state)
            return state

    def save(self, state: ScenarioState) -> None:
        state.updated_at = _now()
        _atomic_write(self.json_path, json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n")
        _atomic_write(self.markdown_path, render_markdown(state))

    def update(self, payload: dict[str, Any]) -> ScenarioState:
        state = merge_state(self.load(), payload)
        self.save(state)
        return state

    def advance_stage(self, previous: str, current: str) -> ScenarioState:
        state = self.load()
        if previous and previous not in state.completed_stages:
            state.completed_stages.append(previous)
        state.current_stage = current
        self.current_stage = current
        self.save(state)
        return state

    def set_scenario_id(self, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        state = self.load()
        if state.scenario_id != scenario_id:
            state.scenario_id = scenario_id
            self.save(state)

    def compact_markdown(self, max_bytes: int = PROMPT_MEMORY_MAX_BYTES) -> str:
        return prompt_projection(render_markdown(self.load()), max_bytes=max_bytes)

    def structured_text(self) -> str:
        return json.dumps(self.load().to_dict(), indent=2, ensure_ascii=False)
