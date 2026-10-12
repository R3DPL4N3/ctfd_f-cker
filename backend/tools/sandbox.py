"""Pydantic AI tool wrappers — thin delegation to backend.tools.core."""

from typing import Any

from pydantic_ai import RunContext

from backend.deps import SolverDeps
from backend.tools.core import (
    do_bash,
    do_check_findings,
    do_list_files,
    do_read_file,
    do_web_fetch,
    do_webhook_create,
    do_webhook_get_requests,
    do_write_file,
)


async def bash(ctx: RunContext[SolverDeps], command: str, timeout_seconds: int = 60) -> str:
    """Execute a bash command inside the sandboxed Docker container.

    Distfiles are at /challenge/distfiles/ (read-only).
    Write generated/repaired files to /challenge/workspace/ (writable).
    Challenge services are reachable via host.docker.internal.
    Run `cat /tools.txt` to see all installed tools.
    """
    return await do_bash(ctx.deps.sandbox, command, timeout_seconds)


async def read_file(ctx: RunContext[SolverDeps], path: str) -> str:
    """Read a file from the container. For distfiles use paths like /challenge/distfiles/readme.txt."""
    return await do_read_file(ctx.deps.sandbox, path)


async def write_file(ctx: RunContext[SolverDeps], path: str, content: str) -> str:
    """Write a file into the container."""
    return await do_write_file(ctx.deps.sandbox, path, content)


async def list_files(ctx: RunContext[SolverDeps], path: str = "/challenge/distfiles") -> str:
    """List files in a directory inside the container."""
    return await do_list_files(ctx.deps.sandbox, path)


async def check_findings(ctx: RunContext[SolverDeps]) -> str:
    """Check for new findings from other agents working on the same challenge.

    Call this periodically to see if siblings have discovered useful information.
    """
    return await do_check_findings(ctx.deps.message_bus, ctx.deps.model_spec)


async def notify_coordinator(ctx: RunContext[SolverDeps], message: str) -> str:
    """Send a message to the coordinator about a strategic discovery or request.

    Use this when you find something that affects the overall competition strategy,
    like discovering a flag format pattern, a shared vulnerability across challenges,
    or when you need help from other solvers.
    """
    if ctx.deps.notify_coordinator:
        try:
            await ctx.deps.notify_coordinator(message)
            return "Message sent to coordinator."
        except Exception as e:
            return f"Notification failed: {e}"
    return "No coordinator connected."


async def web_fetch(ctx: RunContext[SolverDeps], url: str, method: str = "GET", body: str = "") -> str:
    """Fetch a URL from the host. Useful for web challenges.

    Prefer bash+curl inside the sandbox for cookies/sessions.
    """
    return await do_web_fetch(url, method, body)


async def memory_get(ctx: RunContext[SolverDeps]) -> str:
    """Read persistent scenario memory (hosts, credentials, sessions, networks, findings)."""
    store = ctx.deps.memory
    if store is None:
        return "Scenario memory is unavailable because the workspace is not mounted."
    return store.structured_text()


async def memory_update(
    ctx: RunContext[SolverDeps],
    targets: list[dict[str, Any]] | None = None,
    credentials: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    networks: list[dict[str, Any]] | None = None,
    findings: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> str:
    """Merge durable discoveries into scenario memory. Do not dump raw command output."""
    store = ctx.deps.memory
    if store is None:
        return "Scenario memory is unavailable because the workspace is not mounted."
    payload = {
        key: value
        for key, value in {
            "targets": targets,
            "credentials": credentials,
            "sessions": sessions,
            "networks": networks,
            "findings": findings,
            "artifacts": artifacts,
        }.items()
        if value
    }
    state = store.update(payload)
    if ctx.deps.campaign is not None:
        await ctx.deps.campaign.publish(payload, ctx.deps.challenge_name)
    from backend.scenario_memory import render_markdown

    return (
        "Memory updated. Secrets are stored in scenario-state.json and are not echoed here.\n\n"
        + render_markdown(state)
    )




async def campaign_search(ctx: RunContext[SolverDeps], query: str = "", limit: int = 30) -> str:
    """Search durable cross-challenge intelligence. Secret values are redacted."""
    if ctx.deps.campaign is None:
        return "Campaign unavailable"
    return ctx.deps.campaign.search(query, limit)


async def campaign_publish(
    ctx: RunContext[SolverDeps],
    targets: list[dict[str, Any]] | None = None,
    credentials: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    networks: list[dict[str, Any]] | None = None,
    findings: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> str:
    """Publish findings into encrypted host-owned campaign state."""
    if ctx.deps.campaign is None:
        return "Campaign unavailable"
    return await ctx.deps.campaign.publish(
        {key: value for key, value in {
            "targets": targets, "credentials": credentials, "sessions": sessions,
            "networks": networks, "findings": findings, "artifacts": artifacts,
        }.items() if value}, ctx.deps.challenge_name
    )


async def campaign_get_credential(ctx: RunContext[SolverDeps], ref: str) -> str:
    """Explicitly retrieve one credential from the encrypted campaign vault."""
    if ctx.deps.campaign is None:
        return "Campaign unavailable"
    return ctx.deps.campaign.credential(ref)


async def campaign_stage_hash(ctx: RunContext[SolverDeps], path: str) -> str:
    """Stage a hash artifact under the durable host job store."""
    if ctx.deps.campaign is None:
        return "Campaign unavailable"
    return await ctx.deps.campaign.stage_hash(
        ctx.deps.sandbox, path, ctx.deps.challenge_name
    )


async def campaign_job_submit(ctx: RunContext[SolverDeps], artifact_id: str,
                              principal: str, hash_type: str, engine: str = "john") -> str:
    """Start a detached offline cracking job; never runs model-provided shell commands."""
    if ctx.deps.campaign is None:
        return "Campaign unavailable"
    return ctx.deps.campaign.job_submit(artifact_id, principal, hash_type, engine)


async def campaign_job_status(ctx: RunContext[SolverDeps], job_id: str) -> str:
    """Read background job progress without returning cracked secrets."""
    if ctx.deps.campaign is None:
        return "Campaign unavailable"
    return ctx.deps.campaign.job_status(job_id)


async def webhook_create(ctx: RunContext[SolverDeps]) -> str:
    """Create a webhook.site token for out-of-band HTTP callbacks (XSS, SSRF, bot challenges)."""
    return await do_webhook_create()


async def webhook_get_requests(ctx: RunContext[SolverDeps], uuid: str) -> str:
    """Retrieve HTTP requests received by a webhook.site token."""
    return await do_webhook_get_requests(uuid)
