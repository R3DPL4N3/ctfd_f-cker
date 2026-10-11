"""Persistent scenario memory: merge, projection, continuation, recovery."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents.codex_solver import CodexSolver
from backend.cost_tracker import CostTracker
from backend.prompts import ChallengeMeta, build_continuation_prompt, build_prompt
from backend.scenario_memory import (
    ScenarioMemoryStore,
    empty_state,
    redact_memory_payload,
    render_markdown,
)


def _store(tmp_path: Path, stage: str = "Linux 101") -> ScenarioMemoryStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = ScenarioMemoryStore(workspace, scenario_id="scenario-1", current_stage=stage)
    store.ensure()
    return store


def test_missing_state_initializes() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as raw:
        store = ScenarioMemoryStore(raw, current_stage="Linux 101")
        state = store.ensure()
        assert state.current_stage == "Linux 101"
        assert (Path(raw) / "scenario-state.json").is_file()
        assert (Path(raw) / "scenario-memory.md").is_file()
        assert store.load().targets == []


def test_malformed_json_recovers_without_crash(tmp_path) -> None:
    store = _store(tmp_path)
    store.json_path.write_text("{not-json", encoding="utf-8")
    state = store.load()
    assert state.current_stage == "Linux 101"
    assert store.json_path.is_file()
    corrupt = store.json_path.with_name("scenario-state.json.corrupt")
    assert corrupt.is_file()


def test_merge_deduplicates_hosts_credentials_networks_and_findings(tmp_path) -> None:
    store = _store(tmp_path)
    payload = {
        "targets": [{"host": "10.10.10.5", "hostname": "server01"}],
        "credentials": [{
            "username": "svc_sql",
            "domain": "corp.local",
            "password": "s3cret",
            "source": "Linux 101",
        }],
        "networks": [{"cidr": "10.20.0.0/24"}],
        "findings": ["server01 has access to internal 10.20.0.0/24 network"],
        "sessions": [{"type": "ssh", "host": "10.10.10.5", "username": "root"}],
        "artifacts": ["/challenge/workspace/pivot.sh"],
    }
    store.update(payload)
    store.update({
        "targets": [{"host": "10.10.10.5", "hostname": "server01", "notes": ["linux box"]}],
        "credentials": [{
            "username": "svc_sql",
            "domain": "CORP.local",
            "hash": "aad3b435b51404ee",
            "source": "Linux 101",
        }],
        "networks": ["10.20.0.0/24"],
        "findings": ["server01 has access to internal 10.20.0.0/24 network"],
        "sessions": [{"type": "SSH", "host": "10.10.10.5", "username": "root"}],
        "artifacts": ["/challenge/workspace/pivot.sh"],
    })
    state = store.load()
    assert len(state.targets) == 1
    assert state.targets[0].hostname == "server01"
    assert state.targets[0].notes == ["linux box"]
    assert len(state.credentials) == 1
    assert state.credentials[0].password == "s3cret"
    assert state.credentials[0].hash == "aad3b435b51404ee"
    assert len(state.networks) == 1
    assert len(state.findings) == 1
    assert len(state.sessions) == 1
    assert len(state.artifacts) == 1


def test_markdown_hides_plaintext_secrets(tmp_path) -> None:
    store = _store(tmp_path)
    store.update({
        "targets": [{"host": "10.10.10.5", "hostname": "server01"}],
        "credentials": [{
            "username": "svc_sql",
            "domain": "corp.local",
            "password": "s3cret",
            "hash": "31d6cfe0d16ae931b73c59d7e0c089c0",
            "source": "Linux 101",
        }],
        "networks": [{"cidr": "10.20.0.0/24"}],
        "sessions": [{"type": "ssh", "host": "10.10.10.5", "username": "root"}],
        "findings": ["svc_sql appears reusable on internal hosts"],
        "artifacts": ["/challenge/workspace/pivot.sh"],
    })
    markdown = store.markdown_path.read_text(encoding="utf-8")
    assert "10.10.10.5" in markdown
    assert "server01" in markdown
    assert "corp.local\\svc_sql" in markdown
    assert "password available" in markdown
    assert "hash available" in markdown
    assert "source: Linux 101" in markdown
    assert "10.20.0.0/24" in markdown
    assert "SSH root@10.10.10.5" in markdown
    assert "s3cret" not in markdown
    assert "31d6cfe0d16ae931b73c59d7e0c089c0" not in markdown
    assert "s3cret" in store.load().credentials[0].password


def test_redact_memory_payload_strips_secrets() -> None:
    redacted = redact_memory_payload({
        "credentials": [{"username": "root", "password": "hunter2", "hash": "aabb"}],
        "targets": [{"host": "10.0.0.1"}],
    })
    assert redacted["credentials"][0]["password"] == "present"
    assert redacted["credentials"][0]["hash"] == "present"
    assert "hunter2" not in str(redacted)
    assert "aabb" not in str(redacted)


def test_continuation_prompt_includes_memory_summary() -> None:
    prompt = build_continuation_prompt(
        ChallengeMeta(name="Linux 102", category="Linux", description="Move east."),
        "/challenge/workspace/stages/linux-102",
        ["notes.txt"],
        memory_summary=render_markdown(empty_state(current_stage="Linux 102")),
    )
    assert "PERSISTENT SCENARIO MEMORY" in prompt
    assert "Linux 102" in prompt
    assert "CURRENT position" in prompt


def test_continuation_prompt_omits_memory_when_empty() -> None:
    prompt = build_continuation_prompt(
        ChallengeMeta(name="Linux 102", category="Linux"),
        "/challenge/workspace/stages/linux-102",
    )
    assert "PERSISTENT SCENARIO MEMORY" not in prompt


def test_base_prompt_mentions_memory_tools_only_when_enabled() -> None:
    with_memory = build_prompt(ChallengeMeta(name="Linux 101"), [], include_memory=True)
    without = build_prompt(ChallengeMeta(name="Linux 101"), [], include_memory=False)
    assert "memory_update" in with_memory
    assert "memory_update" not in without


@pytest.mark.asyncio
async def test_memory_persists_across_codex_continuation(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    challenge = tmp_path / "linux-102"
    challenge.mkdir()
    (challenge / "metadata.yml").write_text("name: Linux 102\ncategory: Linux\n", encoding="utf-8")

    solver = CodexSolver(
        model_spec="codex/gpt-5.6-sol",
        challenge_dir=str(tmp_path / "linux-101"),
        meta=ChallengeMeta(name="Linux 101", category="Linux", id=1),
        ctfd=object(),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox", container_memory_limit="4g"),
    )
    solver._thread_id = "THREAD_A"
    solver._proc = object()
    solver.sandbox.workspace_dir = str(workspace)
    solver._init_memory()
    solver.bind_scenario("scenario-1")
    solver._memory.update({
        "targets": [{"host": "10.10.10.5", "hostname": "server01"}],
        "credentials": [{
            "username": "svc_sql",
            "domain": "corp.local",
            "password": "s3cret",
            "source": "Linux 101",
        }],
        "networks": [{"cidr": "10.20.0.0/24"}],
        "findings": ["initial host compromised"],
    })
    json_path = solver.memory_path

    await solver.continue_with_challenge(
        ChallengeMeta(name="Linux 102", category="Linux", description="east", id=2),
        str(challenge),
    )

    assert solver._thread_id == "THREAD_A"
    assert solver.sandbox.workspace_dir == str(workspace)
    assert solver.memory_path == json_path
    state = solver._memory.load()
    assert state.current_stage == "Linux 102"
    assert "Linux 101" in state.completed_stages
    assert state.credentials[0].password == "s3cret"
    assert state.targets[0].host == "10.10.10.5"
    assert "PERSISTENT SCENARIO MEMORY" in solver._pending_prompt
    assert "10.10.10.5" in solver._pending_prompt
    assert "s3cret" not in solver._pending_prompt
    solver.tracer.close()
