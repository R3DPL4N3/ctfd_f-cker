# Claude CLI support

The CTF agent can use Claude Code as a solver, a coordinator, or both. The integration uses the **Claude Agent SDK**, which runs the Claude Code runtime. `claude-cli/…` is an explicit model prefix for this backend; `claude-sdk/…` remains supported. The coordinator names `claude` and `claude-cli` select the same backend. This integration does not spawn a fresh `claude -p` command for every turn.

## Setup

1. Install Claude Code and sign in with `claude auth login`. Verify with `claude auth status`.
2. Install project dependencies with `uv sync` and build the Docker image:

   ```bash
   docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
   ```

3. Copy `.env.example` to `.env` and configure CTFd, or pass `--ctfd-url` and `--ctfd-token` on the command line.

The SDK includes its own Claude Code runtime. The system `claude` command is used for sign-in and diagnostics. If `ANTHROPIC_API_KEY` is set, Claude Code may use API billing instead of a subscription; check your environment before launching many solvers. Keep credentials out of the repository.

## Examples

Run Claude as the only solver against one challenge directory:

```bash
uv run ctf-solve \
  --challenge challenges/example \
  --models claude-cli/claude-opus-4-6 \
  --no-submit -v
```

Run Claude as coordinator and solver for a live CTFd instance:

```bash
uv run ctf-solve \
  --coordinator claude-cli \
  --coordinator-model claude-opus-4-6 \
  --models claude-cli/claude-opus-4-6 \
  --challenges-dir challenges \
  --max-challenges 10 -v
```

Mix Claude and Codex solvers while keeping the Claude coordinator:

```bash
uv run ctf-solve \
  --coordinator claude \
  --models claude-cli/claude-opus-4-6 \
  --models codex/gpt-5.6-sol \
  --categories Windows,Web -v
```

The model spec is `claude-cli/<model>[/<effort>]`. Supported effort suffixes are `low`, `medium`, `high`, and `max`; for example, `claude-cli/claude-opus-4-6/high`. Model availability depends on the Claude account and runtime. `--coordinator-model` takes the model ID without a provider prefix. The default solver is still Codex, so pass `--models` to opt into Claude solving.

## How the solver works

- Each Claude solver owns one Docker sandbox. Challenge distfiles are mounted read-only at `/challenge/distfiles`; writable work is in `/challenge/workspace`.
- Claude sees `Bash`, `WebFetch`, `WebSearch`, `memory_get`, and `memory_update`. A pre-tool hook rewrites Bash commands to `docker exec` and rejects host file tools. The hook denies execution if routing fails.
- `submit_flag '<flag>'` is intercepted and sent to the CTFd control plane. `--no-submit` returns a dry-run result. `notify_coordinator '<message>'` sends a message to the coordinator.
- The control plane handles duplicate flags, incorrect-submission cooldowns, accepted flags, and newly unlocked challenges. Solvers do not submit to CTFd directly.
- Claude SDK usage and trace events feed the existing cost tracker and solver trace. A live run requires Claude authentication, a working Docker daemon, CTFd access, and an available model.

## Multi-stage scenarios

When a solved stage unlocks a related challenge, the Claude solver can continue with the **same SDK client and Docker sandbox**. New challenge files are copied under `/challenge/workspace/stages/<challenge-name>/`; previous workspace files and container state remain available. The next turn receives a continuation prompt with the new objective.

Scenario memory is stored in `scenario-state.json` and `scenario-memory.md` in the sandbox workspace. `memory_update` records durable targets, credentials, sessions, networks, findings, and artifacts. `memory_get` reads structured details after context compaction or when a new stage starts. Trace output does not include memory tool arguments or results containing credentials.

The session persists while this process and sandbox are alive. Restarting the CTF agent creates new solver instances; this integration does not restore Claude sessions after a process restart.

## Troubleshooting

| Symptom | Check |
|---|---|
| Authentication failure | Run `claude auth status`; sign in again with `claude auth login`. Check whether `ANTHROPIC_API_KEY` overrides the intended login. |
| Docker startup failure | Run `docker info` and confirm the `ctf-sandbox` image exists. |
| Unknown or unavailable model | Try an available Claude model ID in both `--models` and `--coordinator-model`. |
| Host file tool denied | This is intentional for solvers. Read and write challenge files with Bash paths under `/challenge/`. |
| Flag rejected or cooldown | Inspect the solver trace and CTFd response; a retryable CTFd error is not cached as a wrong flag. |
| No scenario continuation | Confirm the next challenge has a supported prerequisite or causal unlock relationship and appears within `SCENARIO_UNLOCK_WAIT_SECONDS`. |

Claude Code command and authentication details: [CLI reference](https://code.claude.com/docs/en/cli-reference). Agent SDK custom tools and permission behavior: [custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools), [permissions](https://code.claude.com/docs/en/agent-sdk/permissions).
