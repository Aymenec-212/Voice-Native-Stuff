# Voice-Native Web Research Agent

A small macOS utility that turns a spoken request into cited web research.

```
activate → speak → inspect/edit transcript → GO → watch progress → cited answer
```

Speech recognition runs **locally** on a quantized streaming model. Nothing leaves the Mac
until you read the transcript and press GO — and then only the approved *text* does.

```
┌─────────────────────────┐
│          Mac            │
│  Mic                    │
│   ↓                     │
│  Quantized Kyutai STT   │   local only — raw audio never leaves
│   ↓                     │
│  Live transcript        │
│   ↓                     │
│  Edit + GO              │
└──────────┬──────────────┘
           │ approved text only
           ▼
┌─────────────────────────┐
│ Lightweight agent loop  │
│                         │
│ Nemotron 3.5 Lightning  │  ← Nebius Token Factory
│         ↕               │
│     Tavily Search       │
│         ↕               │
│         Web             │
└──────────┬──────────────┘
           │
           ▼
  progress events + cited answer
```

One model, one conversation, one tool, one bounded loop. No agent framework, no sub-agents,
no filesystem or shell access. See [`docs/PLAN.md`](docs/PLAN.md) for the full spec and
[`CLAUDE.md`](CLAUDE.md) for current status.

## Architecture

| Layer | What it does | Where |
|---|---|---|
| ASR runtime | streams mic audio into a resident quantized Kyutai STT model | `src/vnr/asr/` |
| Session controller | holds the raw transcript, the approved query and the session state | `src/vnr/session.py` |
| Research agent | bounded Nemotron ⇄ `web_search` loop, then a streamed synthesis | `src/vnr/research/` |
| Event vocabulary | everything the UI needs, and nothing about the models | `src/vnr/events.py` |
| Local service | loopback WebSocket the native app talks to (Milestone 3) | *not built yet* |

The UI never sees an API key: the local service owns every external call.

### Why citations cannot be invented

The model only ever writes source IDs it was given — `[S1]`, `[S3]`. The application
renumbers them to `[1]`, `[2]` in order of appearance and builds the `Sources` list from
the URLs Tavily actually returned this session. IDs that do not exist are dropped and
reported. The model never writes a URL at all.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv venv
uv pip install -e ".[dev]"      # add ",asr" on macOS for microphone capture
cp .env.example .env            # then fill in NEBIUS_API_KEY and TAVILY_API_KEY
```

`.env` is gitignored. Never commit keys.

## Milestone 2 — research CLI (no UI, no speech)

```bash
uv run vnr-research --list-models        # verify NEBIUS_MODEL exists on your account
uv run vnr-research "what did NVIDIA recently release around agentic models?"
uv run vnr-research --json --save runs/ "compare recent streaming ASR approaches"
```

```
Researching… what did NVIDIA recently release around agentic models?
✓ Preparing research
✓ Searching: "NVIDIA agentic model releases 2026"
  5 results, 5 new sources
✓ Comparing 5 sources
● Writing answer…

NVIDIA released … [1] …

Sources
[1] … — https://…

3 turns · 2 searches · 8 sources · 4 cited · 2 Tavily credits · 5.1k in/612 out tokens
first search 0.94s · first answer token 4.30s · total 9.72s
```

Useful flags: `--events` (raw event JSON on stderr), `--max-searches`, `--max-turns`,
`--depth advanced`, `--verbose`.

## Milestone 1 — quantized ASR spike

A hard gate: the quantized streaming model must be proven on Apple Silicon before any app
work. See [`docs/milestone-1-asr.md`](docs/milestone-1-asr.md).

```bash
uv run vnr-asr-spike --engine mock          # verifies the harness anywhere
uv run vnr-asr-spike                        # the real thing, on the Mac
```

## Tests

```bash
uv run pytest        # offline: no keys, no network, no microphone
uv run ruff check .
```

Integration tests that spend real API credits are opt-in:

```bash
VNR_RUN_INTEGRATION=1 uv run pytest tests/integration -v
```

## Configuration

Everything is environment-driven; see `.env.example`. The budgets that keep the agent
bounded — `RESEARCH_MAX_TURNS`, `RESEARCH_MAX_SEARCHES`, `RESEARCH_MAX_RESULTS_PER_SEARCH`,
`RESEARCH_MAX_ADVANCED_SEARCHES` — are configuration, not constants, and default to
6 / 4 / 5 / 1.
