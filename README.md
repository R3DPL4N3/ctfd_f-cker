# CTF Agent

uv run ctf-solve --challenges-dir challenges -v
uv run ctf-solve --challenges-dir challenges --max-challenges 20 -v

Autonomous CTF (Capture The Flag) solver that races multiple AI models against challenges in parallel. Built in a weekend, we used it to solve all 52/52 challenges and win **1st place at BSidesSF 2026 CTF**.

Built by [Veria Labs](https://verialabs.com), founded by members of [.;,;.](https://ctftime.org/team/222911) (smiley), the [#1 US CTF team on CTFTime in 2024 and 2025](https://ctftime.org/stats/2024/US). We build AI agents that find and exploit real security vulnerabilities for large enterprises.

## Results

| Competition | Challenges Solved | Result |
|-------------|:-:|--------|
| **BSidesSF 2026** | 52/52 (100%) | **1st place ($1,500)** |

The agent solves challenges across all categories — pwn, rev, crypto, forensics, web, and misc.

## How It Works

A **coordinator** LLM manages the competition while **solver swarms** attack individual challenges. Each swarm runs multiple models simultaneously — the first to find the flag wins.

```
                        +-----------------+
                        |  CTFd Platform  |
                        +--------+--------+
                                 |
                        +--------v--------+
                        |  Poller (5s)    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Coordinator LLM |
                        | (Claude/Codex)  |
                        +--------+--------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v--------+ +------v---------+ +------v---------+
     | Swarm:          | | Swarm:         | | Swarm:         |
     | challenge-1     | | challenge-2    | | challenge-N    |
     |                 | |                | |                |
     |  GPT-5.5 xhigh  | |  GPT-5.5 xhigh | |                |
     |  GPT-5.4 xhigh  | |  GPT-5.4 xhigh | |     ...        |
     +--------+--------+ +--------+-------+ +----------------+
              |                    |
     +--------v--------+  +-------v--------+
     | Docker Sandbox  |  | Docker Sandbox |
     | (isolated)      |  | (isolated)     |
     |                 |  |                |
     | pwntools, r2,   |  | pwntools, r2,  |
     | gdb, python...  |  | gdb, python... |
     +-----------------+  +----------------+
```

Each solver runs in an isolated Docker container with CTF tools pre-installed. Solvers never give up — they keep trying different approaches until the flag is found.

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

When a solver confirms a flag, the agent immediately writes `writeup.md` into that
challenge directory using the solver trace and final findings.

## Coordinator Backends

```bash
# Codex coordinator (default, uses your local Codex CLI login)
uv run ctf-solve --coordinator codex ...

# Claude SDK coordinator
uv run ctf-solve --coordinator claude ...
```

## Solver Models

Default model lineup (configurable in `backend/models.py`):

| Model | Provider | Notes |
|-------|----------|-------|
| GPT-5.5 | Codex CLI | xhigh reasoning |
| GPT-5.4 | Codex CLI | xhigh reasoning |

## Sandbox Tooling

Each solver gets an isolated Docker container pre-loaded with CTF tools:

| Category | Tools |
|----------|-------|
| **Binary** | radare2, GDB, objdump, binwalk, strings, readelf |
| **Pwn** | pwntools, ROPgadget, angr, unicorn, capstone |
| **Crypto** | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs |
| **Forensics** | volatility3, Sleuthkit (mmls/fls/icat), foremost, exiftool |
| **Stego** | steghide, stegseek, zsteg, ImageMagick, tesseract OCR |
| **Web** | curl, nmap, Python requests, flask |
| **Misc** | ffmpeg, sox, Pillow, numpy, scipy, PyTorch, podman |

## Features

- **Multi-model racing** — multiple AI models attack each challenge simultaneously
- **Auto-spawn** — new challenges detected and attacked automatically
- **Coordinator LLM** — reads solver traces, crafts targeted technical guidance
- **Cross-solver insights** — findings shared between models via message bus
- **Docker sandboxes** — isolated containers with full CTF tooling
- **Operator messaging** — send hints to running solvers mid-competition

## Configuration

Run `codex login`, then copy `.env.example` to `.env` and fill in your CTFd
settings:

```bash
cp .env.example .env
```

```env
CTFD_URL=https://ctf.example.com
CTFD_TOKEN=ctfd_your_token
```

All CTFd settings can also be passed as environment variables or CLI flags. API
provider settings are only needed if you explicitly select an API-backed model
spec such as `azure/...`, `bedrock/...`, `zen/...`, or `google/...`.

## Requirements

- Python 3.14+
- Docker
- `codex` CLI logged in with `codex login`
- `claude` CLI (optional, only for the Claude coordinator/solver)

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — CTFd interaction and HTML helpers in `pull_challenges.py`
