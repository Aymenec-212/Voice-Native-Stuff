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
| ASR runtime | steps mic audio through a resident Kyutai STT model on the Metal GPU, in-process | `src/vnr/asr/` |
| Session controller | holds the raw transcript, the approved query and the session state | `src/vnr/session.py` |
| Research agent | bounded Nemotron ⇄ `web_search` loop, then a streamed synthesis | `src/vnr/research/` |
| Event vocabulary | everything the UI needs, and nothing about the models | `src/vnr/events.py` |
| Session controller | the IDLE → LISTENING → REVIEW → SUBMITTED state machine | `src/vnr/controller.py` |
| Local service | loopback WebSocket the native app talks to | `src/vnr/service.py` |

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

## Milestone 3 — end-to-end prototype

```bash
uv pip install -e ".[dev,asr,service]"
uv run vnr-service       # terminal 1: loads ASR on demand, binds 127.0.0.1:8765
uv run vnr-prototype     # terminal 2: speak → Enter → edit → GO
```

The prototype talks to the service over the same loopback WebSocket the SwiftUI app will
use, and captures audio the same way the app will, so this is the real data path with a
terminal where the overlay goes.

```
UI → service   text JSON   recording.start · recording.stop · research.submit
                           research.cancel · session.reset
               binary      one frame of PCM
service → UI   text JSON   asr.* and research.* events
```

`recording.stop` lands in `REVIEW` and stops there. Only `research.submit` — carrying the
text the user actually approved — starts anything.

## Milestone 1 — local streaming ASR

```bash
uv pip install -e ".[dev,asr,service,mlx]"   # mlx is Apple Silicon only
uv run vnr-asr-spike --engine mock           # verifies the harness anywhere
uv run vnr-asr-spike                         # the real thing, on the Mac
```

The runtime is Kyutai's MLX stack, run **in-process** on the Metal GPU — no sidecar. An
earlier attempt at a ggml/moshi.cpp subprocess was rejected: it has no stdin path and no
macOS support. That gate result, and the full setup, are in
[`docs/milestone-1-asr.md`](docs/milestone-1-asr.md).

One honest caveat, stated because the plan forbids hiding it: the MLX checkpoint is bf16
on disk and is quantized at load, so the *resident* model is quantized but the file is not.
The 531 MB Q4_K GGUF is not used by this runtime.

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

### Idle memory and answer rendering

The speech model loads on first recording and stays warm between uses. After
`VNR_ASR_IDLE_TIMEOUT_S=300` seconds without activity it releases its weights and Metal
cache, even if the app's WebSocket remains connected. Recording, transcript finalization,
and research keep it warm. Health checks never load or retain the model. A cold wake
shows a loading indicator; wait for **Listening** before speaking.

Clients call `POST /asr/prepare` before recording and check `ready` in the response.
The app and both service CLI clients do this automatically. Readiness changes also
arrive over the existing WebSocket; connected clients do not poll `/health`.

The answer window renders headings, inline emphasis and links, lists, quotes, fenced
code and simple tables, with collapsible search activity and separate cited sources.

Run the real Apple Silicon unload/reload check with:

```bash
uv run python scripts/check_asr_memory.py
```

It reports current RSS and MLX active/cache/peak memory and fails if active allocations
or RSS do not drop. Peak memory is historical and is expected to remain unchanged.
