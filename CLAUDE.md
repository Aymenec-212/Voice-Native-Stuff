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
| 1 | Quantized streaming ASR spike | ⏳ **harness done, runtime NOT yet validated** — run it on the Mac |
| 2 | Nebius + Tavily research CLI | ✅ done, offline-tested; needs one live run to confirm |
| 3 | End-to-end local prototype | ☐ not started — **blocked on the M1 gate** |
| 4 | Native macOS UX | ☐ not started — blocked on M1 |
| 5 | Reliability & metrics / eval set | ☐ prompts written (`docs/evaluation-set.md`), not run |
| 6 | Demo readiness | ☐ not started |

### What "M1 harness done" means

`uv run vnr-asr-spike` exists and is proven end-to-end against a **fake** streaming binary
(`tests/fake_stt.py`): process lifetime, PCM over stdin, incremental parsing, finalize
grace, RSS sampling and error surfacing all work. What is unproven is the only thing that
matters — whether the real Q4_K GGUF runtime streams acceptably on Apple Silicon. That
needs `docs/milestone-1-asr.md` run on the Mac.

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

Fill these in — the next session reads this section first.

- [ ] **M1 gate.** Follow `docs/milestone-1-asr.md` and paste the metrics table here.
      Model load · peak RSS · real-time factor · audio→first transcript · 5-minute
      stability · how the proper nouns (Kyutai, Nebius, Tavily, NVIDIA) came out.
- [ ] **Model card.** Does `efficient-nlp/stt-1b-en_fr-quantized` name a runtime or a
      command? Hugging Face is blocked from agent sessions, so paste it here.
      → the resulting `VNR_ASR_COMMAND` / `VNR_ASR_READY_MARKER`:
- [ ] **Nebius model ID.** `uv run vnr-research --list-models` — is
      `nvidia/Nemotron-3_5-Lightning` listed? If the real ID differs, record it here.
- [ ] **First live research run.** `uv run vnr-research "what's new in Kyutai STT?"` —
      did Nemotron call the tool at all, and were the citations valid? If the model emits
      visible reasoning or ignores the tool, `NEBIUS_EXTRA_BODY` is the escape hatch
      (e.g. `{"chat_template_kwargs":{"thinking":false}}`).

## 6. How to work on this

```bash
uv venv && uv pip install -e ".[dev]"    # add ",asr" on macOS
uv run pytest                            # 99 tests, offline, no keys needed
uv run ruff check .
```

Everything except the real ASR runtime and the live APIs is testable on Linux. The fakes
that make that possible: `tests/conftest.py` (FakeNebius/FakeTavily), `tests/fake_stt.py`
(a stand-in streaming STT binary), and `httpx.MockTransport` for the provider layer.

## 7. Next slice (when the M1 gate passes)

Milestone 3 — end-to-end local prototype:

1. `src/vnr/service.py`: FastAPI WebSocket on `127.0.0.1`, speaking the command/event
   protocol already defined in `src/vnr/events.py`.
2. A session controller owning the IDLE → LISTENING → REVIEW → SUBMITTED → … transitions
   and holding `raw_transcript` vs `submitted_query` (`ResearchSession` already does).
3. A throwaway terminal UI proving mic → transcript → edit → GO → streamed answer.

Do **not** start the SwiftUI app until M1 passes — the plan makes that a hard gate, and
the app's whole premise is that local quantized streaming ASR works.

## 8. Session log

- **2026-09-17** — Repo scaffolded; decisions in §2 agreed with the user. Milestone 2
  delivered (research CLI, 73 tests). Milestone 1 harness delivered and proven against a
  fake binary (26 more tests); the real runtime is unvalidated and waiting on the Mac.
  Evaluation set and opt-in integration tests written.
