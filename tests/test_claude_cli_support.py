"""Claude Code provider routing and persistent solver behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents import claude_solver
from backend.agents.swarm import ChallengeSwarm
from backend.cost_tracker import CostTracker
from backend.models import provider_from_spec
from backend.prompts import ChallengeMeta


class _Sandbox:
    def __init__(self, workspace_dir):
        self.workspace_dir = str(workspace_dir)
        self.container_id = "container123"

    async def start(self):
        pass

    async def exec(self, command, timeout_s=10):
        return SimpleNamespace(stdout="x86_64")


class _Tracer:
    def __init__(self, *args):
        self.events = []

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))

    def tool_call(self, *args):
        pass

    def tool_result(self, *args):
        pass

    def close(self):
        pass


@pytest.mark.asyncio
async def test_claude_cli_uses_sdk_backend_and_reuses_scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_solver, "SolverTracer", _Tracer)
    assert provider_from_spec("claude-cli/claude-opus-4-6/high") == "claude-cli"
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="AD-01"),
        ctfd=SimpleNamespace(),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(),
        model_specs=["claude-cli/claude-opus-4-6/high"],
    )
    solver = swarm._create_solver(swarm.model_specs[0])
    assert isinstance(solver, claude_solver.ClaudeSolver)

    solver.sandbox = _Sandbox(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    solver._client = object()
    solver._session_id = "claude-session"
    solver._init_memory()
    solver.bind_scenario("scenario-1")
    solver._memory_update({"findings": ["first stage complete"]})

    next_dir = tmp_path / "AD-02"
    next_dir.mkdir()
    (next_dir / "metadata.yml").write_text("name: AD-02\n")
    await solver.continue_with_challenge(ChallengeMeta(name="AD-02"), str(next_dir))

    assert solver.sandbox.container_id == "container123"
    assert solver._client is not None
    assert solver._session_id == "claude-session"
    assert solver._pending_prompt is not None and "AD-02" in solver._pending_prompt
    assert "first stage complete" in solver._memory_get()
    assert solver._memory.load().completed_stages == ["AD-01"]
    assert solver.memory_path == str(tmp_path / "workspace" / "scenario-state.json")


@pytest.mark.asyncio
async def test_solver_hook_redirects_bash_and_fails_closed(tmp_path, monkeypatch):
    class _Client:
        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            return self

    monkeypatch.setattr(claude_solver, "ClaudeSDKClient", _Client)
    monkeypatch.setattr(claude_solver, "SolverTracer", _Tracer)
    solver = claude_solver.ClaudeSolver(
        model_spec="claude-cli/claude-opus-4-6",
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="test"),
        ctfd=SimpleNamespace(),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(),
    )
    solver.sandbox = _Sandbox(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    await solver.start()

    options = solver._client.options
    assert options.tools == ["Bash", "WebFetch", "WebSearch"]
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == []
    assert options.env["ENABLE_TOOL_SEARCH"] == "false"
    hook = options.hooks["PreToolUse"][0].hooks[0]
    redirected = await hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls /challenge"}},
        None, None,
    )
    assert redirected["hookSpecificOutput"]["updatedInput"]["command"].startswith(
        "docker exec -i container123 bash -c "
    )
    denied = await hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}},
        None, None,
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    solver._container_id = ""
    failed = await hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "pwd"}},
        None, None,
    )
    assert failed["hookSpecificOutput"]["permissionDecision"] == "deny"
