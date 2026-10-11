"""OpenAI-compatible / GLM solver usage capture, including cancellation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai.usage import RunUsage

from backend.agents.solver import Solver
from backend.cost_tracker import CostTracker
from backend.prompts import ChallengeMeta
from backend.solver_base import CANCELLED, GAVE_UP


def _solver(tmp_path) -> Solver:
    solver = Solver(
        model_spec="openai/glm-5.3",
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="Linux 101", category="Linux"),
        ctfd=object(),
        cost_tracker=CostTracker(),
        settings=SimpleNamespace(sandbox_image="ctf-sandbox", container_memory_limit="4g"),
    )
    solver._agent = SimpleNamespace()
    return solver


def _fill(usage: RunUsage | None, *, inp: int, out: int, cache: int = 0, requests: int = 1) -> SimpleNamespace:
    assert usage is not None
    usage.input_tokens = inp
    usage.output_tokens = out
    usage.cache_read_tokens = cache
    usage.requests = requests
    return SimpleNamespace(
        usage=usage,
        all_messages=lambda: [],
        new_messages=lambda: [],
        output=None,
    )


@pytest.mark.asyncio
async def test_openai_compatible_completion_usage_is_recorded(tmp_path) -> None:
    solver = _solver(tmp_path)

    async def fake_run(*_a, usage=None, **_k):
        return _fill(usage, inp=900, out=40, cache=12, requests=1)

    solver._agent.run = fake_run
    result = await solver.run_until_done_or_gave_up()
    recorded = solver.cost_tracker.by_agent[solver.agent_name].usage
    assert result.status == GAVE_UP
    assert recorded.input_tokens == 900
    assert recorded.output_tokens == 40
    assert recorded.cache_read_tokens == 12
    assert recorded.requests == 1
    solver.tracer.close()


@pytest.mark.asyncio
async def test_multi_request_usage_is_recorded_once_per_run(tmp_path) -> None:
    solver = _solver(tmp_path)

    async def fake_run(*_a, usage=None, **_k):
        return _fill(usage, inp=3100, out=220, cache=50, requests=6)

    solver._agent.run = fake_run
    await solver.run_until_done_or_gave_up()
    recorded = solver.cost_tracker.by_agent[solver.agent_name].usage
    assert recorded.input_tokens == 3100
    assert recorded.requests == 6
    solver.tracer.close()


@pytest.mark.asyncio
async def test_cancelled_solver_keeps_accumulated_usage(tmp_path) -> None:
    solver = _solver(tmp_path)

    async def fake_run(*_a, usage=None, **_k):
        _fill(usage, inp=1500, out=90, cache=20, requests=4)
        raise asyncio.CancelledError()

    solver._agent.run = fake_run
    result = await solver.run_until_done_or_gave_up()
    recorded = solver.cost_tracker.by_agent[solver.agent_name].usage
    assert result.status == CANCELLED
    assert recorded.input_tokens == 1500
    assert recorded.output_tokens == 90
    assert recorded.cache_read_tokens == 20
    assert recorded.requests == 4
    assert result.cost_usd == solver.cost_tracker.by_agent[solver.agent_name].cost_usd
    solver.tracer.close()


@pytest.mark.asyncio
async def test_usage_is_not_double_counted_across_commits(tmp_path) -> None:
    solver = _solver(tmp_path)
    solver._run_usage = RunUsage(input_tokens=100, output_tokens=10, requests=1)
    solver._usage_committed = False
    solver._commit_run_usage()
    solver._commit_run_usage()
    recorded = solver.cost_tracker.by_agent[solver.agent_name].usage
    assert recorded.input_tokens == 100
    assert recorded.output_tokens == 10
    solver.tracer.close()


@pytest.mark.asyncio
async def test_successive_runs_add_increments_not_cumulative_snapshots(tmp_path) -> None:
    solver = _solver(tmp_path)
    values = iter([(100, 10), (50, 5)])

    async def fake_run(*_a, usage=None, **_k):
        inp, out = next(values)
        return _fill(usage, inp=inp, out=out, requests=1)

    solver._agent.run = fake_run
    await solver.run_until_done_or_gave_up()
    await solver.run_until_done_or_gave_up()
    recorded = solver.cost_tracker.by_agent[solver.agent_name].usage
    assert recorded.input_tokens == 150
    assert recorded.output_tokens == 15
    solver.tracer.close()
