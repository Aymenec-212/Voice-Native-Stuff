# CLAUDE.md — working notes & progress tracker

Read this file first in a new session. It is the handoff document: what exists,
what is proven, what is next, and which decisions are already settled.

- **Spec / source of truth:** `docs/PLAN.md` (condensed from the original project plan).
- **Branch:** `claude/wizardly-ptolemy-j7ma9h`
- **Package manager:** `uv` (never `pip`).
- **Dev machine:** macOS / Apple Silicon (the user's). Agent sessions run on Linux
  containers with **no microphone, no Hugging Face egress, and no API keys** — so
  anything touching the mic, the GGUF weights, or the live Nebius/Tavily APIs must
  be written here and **run by the user on the Mac**.

---

## 1. What this project is

Voice-native web research utility for macOS:

```
mic → local quantized streaming ASR → editable transcript → user presses GO
    → Nemotron 3.5 Lightning (Nebius Token Factory) ⇄ Tavily web_search
    → progress events + streamed cited answer
```

Hard product rules (do not violate — see `docs/PLAN.md` §2, §28):
- Raw audio never leaves the Mac. Only the **user-approved text** goes to the cloud.
- No speculative search or speculative LLM calls from partial transcripts.
- Exactly **one** external tool in V1: `web_search`. No filesystem/shell/browser/MCP/sub-agents.
- The agent loop is one model, one conversation, one tool, one bounded loop.

## 2. Settled decisions

| Decision | Choice | Why |
|---|---|---|
| Local service language | Python 3.11+, managed by `uv` | user's request |
| UI ⇄ service transport | FastAPI WebSocket bound to `127.0.0.1` | plan §19 |
| HTTP client | `httpx` (async) directly — no OpenAI SDK, no LangChain | plan §8: thin provider layer, and `httpx.MockTransport` makes tests dependency-free |
| Agent concurrency | async throughout | cancellation (plan §22) and the WS service need it |
| ASR runtime | pluggable `AsrEngine`; first target is a **moshi.cpp / ggml** style binary that loads `efficient-nlp/stt-1b-en_fr-quantized` Q4_K | only path confirmed to load Q4_K GGUF; MLX path is BF16 and is explicitly *not* a silent substitute (plan §5) |
| Citations | model cites `[S3]`; the **app** renumbers to `[1]` and builds the Sources list from Tavily URLs | makes invented URLs structurally impossible (plan §13) |
| Final answer streaming | tool/decision rounds are non-streaming; a separate **final synthesis** call is streamed | plan §17 "prioritize robustness"; avoids ambiguous mixed content+tool_call streams |

## 3. Milestone status

| # | Milestone | Status |
|---|---|---|
| 1 | Quantized streaming ASR spike | ⏳ **code written, NOT yet validated** — needs a run on the Mac |
| 2 | Nebius + Tavily research CLI | ⏳ in progress |
| 3 | End-to-end local prototype | ☐ not started |
| 4 | Native macOS UX | ☐ not started (blocked on M1 gate) |
| 5 | Reliability & metrics / eval set | ☐ not started |
| 6 | Demo readiness | ☐ not started |

**M1 is a hard gate:** do not start Milestone 4 (SwiftUI app) until the user reports
the spike numbers in §5 below.

## 4. Repo map

```
docs/PLAN.md            condensed spec — the normative document
docs/milestone-1-asr.md how to build/run the ASR spike on macOS
src/vnr/config.py       env-backed configuration
src/vnr/events.py       the UI event vocabulary (asr.*, research.*)
src/vnr/session.py      ResearchSession state object
src/vnr/metrics.py      latency/token/credit instrumentation
src/vnr/asr/            engine protocol + adapters (moshicpp, mock) + mic capture
src/vnr/research/       nebius, tavily, sources, citations, tools, prompts, agent
src/vnr/cli/            research CLI + ASR spike entrypoints
tests/                  unit + mocked-agent tests (run on Linux, no keys needed)
```

## 5. Open questions / things only the user can answer

- [ ] **M1 numbers.** Run `uv run vnr-asr-spike` on the Mac and record: model load time,
      resident memory, real-time factor, audio→transcript delay, stability over 5 min.
- [ ] Does `efficient-nlp/stt-1b-en_fr-quantized`'s model card name a specific runtime or
      command? (Hugging Face is blocked from agent sessions — paste it here if so.)
- [ ] Confirm `nvidia/Nemotron-3_5-Lightning` appears in `GET /v1/models` on the user's
      Nebius account (`uv run vnr-research --list-models`).

## 6. Session log

- **2026-09-17** — Repo scaffolded. Decisions in §2 agreed with the user.
