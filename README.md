# CTF Agent

Multi-model CTF solver with a long-lived coordinator, isolated Docker sandboxes, CTFd integration, and persistent multi-stage scenario support.

## Quick Start

```bash
# Install
uv sync

# Build sandbox image
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .

# Log in to Codex CLI (used by the default solver/coordinator)
codex login

# Configure CTFd
cp .env.example .env
# Edit .env with your CTFd URL/token

# Run against a CTFd instance
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 10 \
  -v
```

Useful shorthand:

```bash
uv run ctf-solve --challenges-dir challenges -v
uv run ctf-solve --challenges-dir challenges --max-challenges 20 -v
```

## Category Selection

By default the coordinator can see and attack every visible CTFd category.

Use a whitelist when you only want selected categories such as Web, Linux, or Windows.

Repeat `--category`:

```bash
uv run ctf-solve \
  --category Windows \
  --category Web \
  --category Linux \
  -v
```

Or use the comma-separated shorthand:

```bash
uv run ctf-solve \
  --categories Windows,Web,Linux \
  -v
```

Category matching is case-insensitive.

When a whitelist is configured, disallowed categories are filtered at the CTFd discovery layer. This means they are excluded from:

- coordinator challenge listings
- startup auto-spawn
- poller discovery
- newly unlocked challenge routing
- manual `spawn_swarm` requests

For example:

```bash
uv run ctf-solve --categories Windows,Web,Linux -v
```

will ignore categories such as Forensics, Crypto, Reverse, Pwn, and Misc unless they are explicitly added.

An empty whitelist preserves the original behavior and allows all categories.

## How It Works

A coordinator LLM manages the competition while solver swarms attack individual challenges.

```text
                        +-----------------+
                        |  CTFd Platform  |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Category Filter |
                        +--------+--------+
                                 |
                        +--------v--------+
                        |  Poller (5s)    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Coordinator LLM |
                        |  Luna / Claude  |
                        +--------+--------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v--------+ +------v---------+ +------v---------+
     | Swarm:          | | Swarm:         | | Swarm:         |
     | challenge-1     | | challenge-2    | | challenge-N    |
     |                 | |                | |                |
     | GPT-5.6 Sol     | | GPT-5.6 Sol    | |      ...       |
     +--------+--------+ +--------+-------+ +----------------+
              |                    |
     +--------v--------+  +-------v--------+
     | Docker Sandbox  |  | Docker Sandbox |
     | (isolated)      |  | (isolated)     |
     +-----------------+  +----------------+
```

Each solver runs in an isolated Docker container with CTF tooling pre-installed.

The coordinator handles orchestration and strategic guidance. Exploitation, reconnaissance, shell interaction, lateral movement, and flag discovery stay inside solver sandboxes.

## Multi-Stage Scenario Continuity

CTFd prerequisite chains can reuse the same solver session instead of creating a fresh sandbox for every stage.

Example:

```text
AD-01 Initial Access
        |
        | flag accepted
        v
AD-02 Lateral Movement
        |
        | flag accepted
        v
AD-03 Domain Compromise
```

When AD-02 directly requires AD-01, the deterministic control plane can continue the existing scenario.

For supported solvers such as Codex, continuation preserves:

- the same solver object
- the same Codex thread
- the same Docker sandbox
- `/challenge/workspace`
- downloaded files and scripts
- credentials and findings
- Kerberos tickets and related state
- previous reconnaissance
- stage history and scenario cost accounting

New challenge files are staged into the existing workspace without recreating the container.

Conceptually:

```text
AD-01
  |
  v
GPT-5.6 Sol
thread: THREAD_A
sandbox: SANDBOX_A
  |
  | FLAG accepted
  v
waiting_for_unlock
  |
  | AD-02 prerequisite match
  v
same Sol
same THREAD_A
same SANDBOX_A
  |
  v
AD-02
```

If the winning solver does not support `continue_with_challenge()`, the system falls back to a new swarm for the next stage.

Direct CTFd prerequisite relationships are deterministic and do not require the coordinator LLM to guess whether two challenges belong to the same scenario.

## Flag Submission Control Plane

Solvers do not independently own CTFd submission state.

The normal flow is:

```text
solver
  |
  | candidate flag
  v
ChallengeSwarm.try_submit_flag()
  |
  v
control-plane submission
  |
  v
CTFd
  |
  +--> ACCEPTED
  |      -> mark scenario solved
  |      -> refresh CTFd immediately
  |      -> route newly unlocked challenge
  |
  +--> REJECTED
  |      -> cache exact rejected candidate
  |
  +--> RETRYABLE_ERROR
         -> do not poison candidate cache
```

Timeouts, transport failures, temporary server failures, and authentication errors are not treated as incorrect flags.

The coordinator LLM itself does not expose a direct `submit_flag` tool.

## Scenario Unlock Window

After a scenario stage is accepted, the winning solver waits for a newly unlocked continuation instead of immediately destroying its sandbox.

Default:

```text
120 seconds
```

Configure with:

```env
SCENARIO_UNLOCK_WAIT_SECONDS=120
```

If no related challenge appears before the timeout, the scenario is stopped and its sandbox is cleaned up normally.

For the first prerequisite-chain milestone, continuation assumes the next CTFd challenge is hidden until the prerequisite is solved, then becomes visible.

## Coordinator Backends

```bash
# Codex coordinator (default)
uv run ctf-solve --coordinator codex ...

# Claude SDK coordinator
uv run ctf-solve --coordinator claude ...
```

The default Codex coordinator model is:

```text
gpt-5.6-luna
```

The default solver lineup currently contains:

| Model | Provider | Notes |
|---|---|---|
| GPT-5.6 Sol | Codex CLI | high reasoning, persistent thread continuation |

## GOAD / Active Directory Benchmark Example

A useful benchmark layout is a hidden prerequisite chain:

```text
AD-01 Initial Access
    |
    | prerequisite
    v
AD-02 Lateral Movement
    |
    | prerequisite
    v
AD-03 Domain Compromise
```

Run only Windows challenges:

```bash
uv run ctf-solve \
  --models codex/gpt-5.6-sol \
  --coordinator codex \
  --coordinator-model gpt-5.6-luna \
  --categories Windows \
  --max-challenges 1 \
  -v
```

Expected behavior:

```text
AD-01
 -> Sol + THREAD_A + SANDBOX_A
 -> flag accepted
 -> AD-02 unlocks
 -> SAME Sol + SAME THREAD_A + SAME SANDBOX_A
 -> flag accepted
 -> AD-03 unlocks
 -> SAME Sol + SAME THREAD_A + SAME SANDBOX_A
 -> final flag accepted
 -> scenario stops
 -> sandbox destroyed
```

## Writeups and Metrics

When a solver confirms a flag, the agent writes a `writeup.md` into that challenge directory using the solver trace and final findings.

Scenario stages also preserve per-stage metrics such as:

```text
AD-01
steps: 3
$0.22

AD-02
steps: 5
$0.71

AD-03
steps: 8
$1.06

Scenario total:
$1.99
```

## Sandbox Tooling

Each solver gets an isolated Docker container pre-loaded with CTF tools:

| Category | Tools |
|---|---|
| Binary | radare2, GDB, objdump, binwalk, strings, readelf |
| Pwn | pwntools, ROPgadget, angr, unicorn, capstone |
| Crypto | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs |
| Forensics | volatility3, Sleuthkit, foremost, exiftool |
| Stego | steghide, stegseek, zsteg, ImageMagick, tesseract OCR |
| Web | curl, nmap, Python requests, flask |
| Misc | ffmpeg, sox, Pillow, numpy, scipy, PyTorch, podman |

## Features

- multi-model solver swarms
- CTFd polling and auto-spawn
- category whitelist filtering
- persistent prerequisite-chain scenarios
- same-thread Codex continuation
- same-sandbox workspace persistence
- deterministic control-plane flag submission
- retry-safe submission classification
- coordinator trace monitoring and targeted bumps
- cross-solver findings via message bus
- isolated Docker sandboxes
- per-stage writeups, costs, and step counts
- operator messaging
- GitHub Actions test checks

## Configuration

Run `codex login`, then copy `.env.example` to `.env` and fill in your CTFd settings:

```bash
cp .env.example .env
```

```env
CTFD_URL=https://ctf.example.com
CTFD_TOKEN=ctfd_your_token
```

All CTFd settings can also be passed as environment variables or CLI flags.

API provider settings are only needed if you explicitly select an API-backed model spec such as `azure/...`, `bedrock/...`, `zen/...`, or `google/...`.

## Testing

Run the full suite locally:

```bash
uv run pytest -q
```

Scenario-specific tests:

```bash
uv run pytest -q tests/test_scenario_continuity.py tests/test_scenario_e2e.py
```

GitHub Actions runs both the scenario tests and the full test suite on every push to `main` and on pull requests targeting `main`.

## Requirements

- Python 3.14+
- Docker
- `codex` CLI authenticated with `codex login`
- `claude` CLI only if using the Claude coordinator/solver
