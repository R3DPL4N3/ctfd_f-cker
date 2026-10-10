"""Per-model solver agent — one model, one container, one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from copy import copy
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset
from pydantic_ai.usage import RunUsage

from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.deps import SolverDeps
from backend.loop_detect import LOOP_WARNING_MESSAGE, LoopDetector
from backend.models import (
    model_id_from_spec,
    provider_from_spec,
    resolve_model,
    resolve_model_settings,
    supports_vision,
)
from backend.output_types import FlagFound
from backend.prompts import ChallengeMeta, build_prompt, list_distfiles
from backend.sandbox import DockerSandbox
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    SolverResult,
    submission_accepted,
)
from backend.tools.flag import submit_flag
from backend.tools.sandbox import (
    bash,
    check_findings,
    list_files,
    memory_get,
    memory_update,
    notify_coordinator,
    read_file,
    web_fetch,
    webhook_create,
    webhook_get_requests,
    write_file,
)
from backend.tools.vision import view_image
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)


@dataclass
class TracingToolset(WrapperToolset[SolverDeps]):
    """Wraps a toolset to add per-call tracing and loop detection."""

    tracer: SolverTracer = field(repr=False)
    loop_detector: LoopDetector = field(repr=False)
    step_counter: list[int] = field(repr=False)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[SolverDeps], tool: ToolsetTool[SolverDeps]
    ) -> Any:
        self.step_counter[0] += 1
        step = self.step_counter[0]

        traced_args = tool_args
        if name == "memory_update":
            from backend.scenario_memory import memory_payload_trace_summary
            traced_args = memory_payload_trace_summary(tool_args if isinstance(tool_args, dict) else {})
        self.tracer.tool_call(name, traced_args, step)

        if name in {"memory_get", "memory_update"}:
            loop_status = None
        else:
            loop_status = self.loop_detector.check(name, traced_args)
        if loop_status == "break":
            logger.warning(f"Loop break on {name} at step {step}")
            self.tracer.event("loop_break", tool=name, step=step)
            # Inject loop warning by returning it as the tool result
            return LOOP_WARNING_MESSAGE

        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)

        result_str = str(result) if result is not None else ""
        traced_result = result_str
        if name in {"memory_get", "memory_update"}:
            traced_result = "scenario memory updated" if name == "memory_update" else "scenario memory read"
        self.tracer.tool_result(name, traced_result, step)

        # Inject loop warning alongside result on "warn" level
        if loop_status == "warn":
            result = f"{result}\n\n{LOOP_WARNING_MESSAGE}" if isinstance(result, str) else result

        # Check for confirmed flag
        if name == "submit_flag" and submission_accepted(result_str):
            self.tracer.event("flag_confirmed", tool=name, step=step)

        if step % 5 == 0 and ctx.deps.message_bus and isinstance(result, str):
            from backend.tools.core import do_check_findings
            findings_text = await do_check_findings(ctx.deps.message_bus, ctx.deps.model_spec)
            if findings_text and "No new findings" not in findings_text:
                result = f"{result}\n\n---\n{findings_text}"
                self.tracer.event("findings_injected", step=step)

        return result


def _build_toolset(deps: SolverDeps) -> FunctionToolset[SolverDeps]:
    """Build the raw toolset for a solver agent."""
    tools = [bash, read_file, write_file, list_files, submit_flag, web_fetch,
             webhook_create, webhook_get_requests, check_findings, notify_coordinator,
             memory_get, memory_update]
    if deps.use_vision:
        tools.append(view_image)
    return FunctionToolset(tools=tools, max_retries=4)


class Solver:
    """A single solver: one model, one container, one challenge."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        ctfd: CTFdClient,
        cost_tracker: CostTracker,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        sandbox: DockerSandbox | None = None,
        owns_sandbox: bool | None = None,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.ctfd = ctfd
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self._owns_sandbox = owns_sandbox if owns_sandbox is not None else (sandbox is None)

        self.sandbox = sandbox or DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
        )
        self.use_vision = supports_vision(model_spec)
        self.deps = SolverDeps(
            sandbox=self.sandbox,
            ctfd=ctfd,
            challenge_dir=challenge_dir,
            challenge_name=meta.name,
            workspace_dir="",
            use_vision=self.use_vision,
            cost_tracker=cost_tracker,
        )
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"
        self._agent: Agent[SolverDeps, FlagFound] | None = None
        self._messages: list = []
        self._step_count = [0]  # mutable ref shared with TracingToolset
        self._flag: str | None = None
        self._confirmed: bool = False
        self._findings: str = ""
        self._pending_prompt: str | None = None
        self._run_usage = RunUsage()
        self._usage_committed = False
        self._memory = None

    async def start(self) -> None:
        """Start the sandbox and build the agent."""
        if not self.sandbox._container:
            await self.sandbox.start()
        self.deps.workspace_dir = self.sandbox.workspace_dir
        self._init_memory()

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"

        distfile_names = list_distfiles(self.challenge_dir)
        system_prompt = build_prompt(
            self.meta,
            distfile_names,
            container_arch=container_arch,
            include_memory=True,
        )

        model = resolve_model(self.model_spec, self.settings)
        model_settings = resolve_model_settings(self.model_spec)
        raw_toolset = _build_toolset(self.deps)
        toolset = TracingToolset(
            wrapped=raw_toolset,
            tracer=self.tracer,
            loop_detector=self.loop_detector,
            step_counter=self._step_count,
        )

        self._agent = Agent(
            model,
            deps_type=SolverDeps,
            system_prompt=system_prompt,
            model_settings=model_settings,
            toolsets=[toolset],
            output_type=FlagFound,
        )

        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        logger.info(f"[{self.agent_name}] Solver started")

    async def run_until_done_or_gave_up(self) -> SolverResult:
        """Run the solver loop until flag found, gave up, or cancelled."""
        if not self._agent:
            await self.start()
        assert self._agent is not None

        t0 = time.monotonic()
        self._run_usage = RunUsage()
        self._usage_committed = False

        try:
            from pydantic_ai.usage import UsageLimits
            if self._pending_prompt:
                prompt = self._pending_prompt
                self._pending_prompt = None
            elif not self._messages:
                prompt = "Solve this CTF challenge."
            else:
                prompt = "Continue solving."
            result = await self._agent.run(
                prompt,
                deps=self.deps,
                message_history=self._messages if self._messages else None,
                usage_limits=UsageLimits(request_limit=None),
                usage=self._run_usage,
            )

            duration = time.monotonic() - t0
            self._commit_run_usage(duration_seconds=duration)

            self._messages = result.all_messages()

            # Trace model responses from new messages
            from pydantic_ai.messages import ModelResponse, TextPart
            for msg in result.new_messages():
                if isinstance(msg, ModelResponse):
                    text_parts = [p.content for p in msg.parts if isinstance(p, TextPart)]
                    text = " ".join(text_parts)
                    msg_usage = msg.usage
                    self.tracer.model_response(
                        text[:500], self._step_count[0],
                        input_tokens=msg_usage.input_tokens if msg_usage else 0,
                        output_tokens=msg_usage.output_tokens if msg_usage else 0,
                    )

            output = result.output
            if isinstance(output, FlagFound):
                self._flag = output.flag
                self._findings = f"Flag found via {output.method}: {output.flag}"
                # In dry-run mode, structured output is sufficient (can't verify via CTFd)
                if self.deps.no_submit:
                    self._confirmed = True
            # CTFd confirmation always counts (the primary path when not in dry-run)
            if self.deps.confirmed_flag:
                self._confirmed = True
                self._flag = self._flag or self.deps.confirmed_flag

            if self._confirmed and self._flag:
                return self._result(FLAG_FOUND)
            return self._result(GAVE_UP)

        except asyncio.CancelledError:
            self._commit_run_usage(duration_seconds=time.monotonic() - t0)
            return self._result(CANCELLED)
        except Exception as e:
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=str(e))
            self._commit_run_usage(duration_seconds=time.monotonic() - t0)
            return self._result(ERROR)

    async def continue_with_challenge(self, challenge_meta: ChallengeMeta, challenge_dir: str) -> None:
        """Keep this agent, its message history, and its sandbox. Do not start a new container."""
        from backend.prompts import build_continuation_prompt, list_distfiles
        from backend.sandbox import stage_challenge_into_workspace

        container_path = stage_challenge_into_workspace(
            self.sandbox.workspace_dir,
            challenge_dir,
            challenge_meta.name,
        )
        previous = self.meta.name
        self.challenge_dir = challenge_dir
        self.meta = challenge_meta
        self.deps.challenge_dir = challenge_dir
        self.deps.challenge_name = challenge_meta.name
        self.deps.confirmed_flag = None
        self._confirmed = False
        self._flag = None
        memory_summary = None
        if self._memory is not None:
            self._memory.advance_stage(previous, challenge_meta.name)
            memory_summary = self._memory.compact_markdown()
        else:
            self._init_memory()
            if self._memory is not None:
                self._memory.advance_stage(previous, challenge_meta.name)
                memory_summary = self._memory.compact_markdown()
        self._pending_prompt = build_continuation_prompt(
            challenge_meta,
            container_path,
            list_distfiles(challenge_dir),
            memory_summary=memory_summary,
        )
        self.agent_name = f"{challenge_meta.name}/{self.model_id}"
        self._step_count[0] = 0
        self._run_usage = RunUsage()
        self._usage_committed = False
        self.loop_detector.reset()
        self.tracer.event(
            "scenario_continued",
            from_challenge=previous,
            to_challenge=challenge_meta.name,
            sandbox_reused=True,
        )
        logger.info("[%s] Continuing on %s in the existing sandbox", self.agent_name, challenge_meta.name)

    def bump(self, insights: str) -> None:
        """Inject insights from siblings and prepare to resume."""
        bump_msg = ModelRequest(
            parts=[
                UserPromptPart(
                    content=(
                        "Your previous attempt did not find the flag. Here are insights "
                        "from other agents working on the same challenge:\n\n"
                        f"{insights}\n\n"
                        "Use these insights to try a different approach. "
                        "Do NOT repeat what has already been tried."
                    )
                )
            ]
        )
        self._messages.append(bump_msg)
        self.loop_detector.reset()
        self.tracer.event("bump", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with sibling insights")

    def _commit_run_usage(self, duration_seconds: float = 0.0) -> None:
        """Record this run's usage once. Cumulative snapshots are not summed again."""
        if self._usage_committed:
            return
        self._usage_committed = True
        usage = copy(self._run_usage)
        if not usage.has_values() and usage.requests == 0:
            return
        self.cost_tracker.record(
            self.agent_name, usage, self.model_id,
            provider_spec=provider_from_spec(self.model_spec),
            duration_seconds=duration_seconds,
        )
        agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
        self.tracer.usage(
            usage.input_tokens, usage.output_tokens,
            usage.cache_read_tokens,
            agent_usage.cost_usd if agent_usage else 0.0,
        )

    def _init_memory(self) -> None:
        workspace = getattr(self.sandbox, "workspace_dir", "") if self.sandbox else ""
        if not workspace:
            return
        from backend.scenario_memory import ScenarioMemoryStore

        self._memory = ScenarioMemoryStore(workspace, current_stage=self.meta.name)
        self._memory.ensure()
        self.deps.memory = self._memory

    @property
    def memory_path(self) -> str | None:
        if self._memory is None:
            return None
        return str(self._memory.json_path)

    def bind_scenario(self, scenario_id: str) -> None:
        if self._memory is None:
            self._init_memory()
        if self._memory is not None:
            self._memory.set_scenario_id(scenario_id)

    def _result(self, status: str, run_steps: int | None = None, run_cost: float | None = None) -> SolverResult:
        self._commit_run_usage()
        agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
        cost = agent_usage.cost_usd if agent_usage else 0.0
        usage = self._run_usage
        self.tracer.event(
            "finish",
            status=status,
            flag=self._flag,
            confirmed=self._confirmed,
            cost_usd=round(cost, 4),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            requests=usage.requests,
        )
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps if run_steps is not None else self._step_count[0],
            cost_usd=run_cost if run_cost is not None else cost,
            log_path=self.tracer.path,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count[0])
        self.tracer.close()
        if self._owns_sandbox and self.sandbox:
            await self.sandbox.stop()
