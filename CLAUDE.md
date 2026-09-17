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
| ASR runtime | **moshi.cpp** (chosen by the user), driven via `VNR_ASR_COMMAND` behind the pluggable `AsrEngine` | only path that loads Q4_K GGUF; MLX is BF16 and is explicitly *not* a silent substitute (plan §5) |
| ASR model inputs | `VNR_ASR_MODEL_DIR` (a directory) + `VNR_ASR_QUANT` | the runtime needs four files — the GGUF, the Mimi codec weights, the tokenizer and config.json — and moshi.cpp takes `-r <dir> -q <quant>` |
| Audio capture | the **UI** captures and streams PCM over loopback | plan §19 lists `audio.frame` as a UI → service command |
| Citations | model cites `[S3]`; the **app** renumbers to `[1]` and builds the Sources list from Tavily URLs | makes invented URLs structurally impossible (plan §13) |
| Final answer streaming | tool/decision rounds are non-streaming; a separate **final synthesis** call is streamed | plan §17 "prioritize robustness"; avoids ambiguous mixed content+tool_call streams |

## 3. Milestone status

| # | Milestone | Status |
|---|---|---|
| 1 | Quantized streaming ASR spike | ⏳ harness done; runtime unvalidated — **gate waived by the user, moshi.cpp chosen** |
| 2 | Nebius + Tavily research CLI | ✅ done, offline-tested; needs one live run to confirm |
| 3 | End-to-end local prototype | ✅ done — service + controller + terminal prototype, proven over two real processes |
| 4 | Native macOS UX | ☐ next, once the runtime actually transcribes |
| 5 | Reliability & metrics / eval set | ☐ prompts written (`docs/evaluation-set.md`), not run |
| 6 | Demo readiness | ☐ not started |

### What "M1 harness done" means

`uv run vnr-asr-spike` exists and is proven end-to-end against a **fake** streaming binary
(`tests/fake_stt.py`): process lifetime, PCM over stdin, incremental parsing, finalize
grace, RSS sampling and error surfacing all work. What is unproven is the only thing that
matters — whether the real Q4_K GGUF runtime streams acceptably on Apple Silicon. That
needs `docs/milestone-1-asr.md` run on the Mac.

**On the gate:** the user waived it on 2026-09-17 ("just pick the moshi.cpp, no need to
run any spike") and Milestone 3 was built against the mock engine. That is a legitimate
call — M3 depends on the `AsrEngine` *interface*, not on the runtime — but it means the
first real transcription is still unproven. Run `uv run vnr-asr-spike` once before
Milestone 4, so the SwiftUI work is not built on an assumption.

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
src/vnr/controller.py   session state machine (IDLE → LISTENING → REVIEW → …)
src/vnr/service.py      FastAPI WebSocket on 127.0.0.1
src/vnr/cli/            research CLI + ASR spike + terminal prototype
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
uv run pytest                            # 131 tests, offline, no keys needed
uv run ruff check .
```

Everything except the real ASR runtime and the live APIs is testable on Linux. The fakes
that make that possible: `tests/conftest.py` (FakeNebius/FakeTavily), `tests/fake_stt.py`
(a stand-in streaming STT binary), and `httpx.MockTransport` for the provider layer.

## 7. Next slice — Milestone 4 (native macOS UX)

The protocol is settled and tested, so the Swift side is a client, not a redesign:

1. Connect to `ws://127.0.0.1:8765/ws`; render purely from the event stream.
2. Menu-bar item + global shortcut → `recording.start`; `AVAudioEngine` at 24 kHz mono
   int16 → binary frames; Enter/click → `recording.stop`.
3. An overlay with the live transcript, then an **editable** field. GO sends
   `research.submit` carrying the edited text. That step is the product — do not skip it.
4. Progress rows from `research.search_started` / `search_completed`, the answer from
   `research.answer_delta`, and clickable citations from `cited_sources` on
   `research.completed` (already numbered to match the `[n]` markers in the text).
5. `/health` gates the record button, so recording stays disabled until the model is in.

Before starting: run the spike once (see the gate note in §3).

## 8. Session log

- **2026-09-17 (2)** — User chose moshi.cpp and waived the spike gate. Reworked the ASR
  config for the real download layout (`-r <dir> -q q4_k`; four model files, not one).
  Milestone 3 delivered: session controller, loopback WebSocket service, terminal
  prototype; 32 new tests. Two bugs the tests caught: research events sat in an outbox
  until the client spoke again (so the answer never streamed), and you could neither
  record again after an answer nor retry after a provider failure.
- **2026-09-17** — Repo scaffolded; decisions in §2 agreed with the user. Milestone 2
  delivered (research CLI, 73 tests). Milestone 1 harness delivered and proven against a
  fake binary (26 more tests); the real runtime is unvalidated and waiting on the Mac.
  Evaluation set and opt-in integration tests written.
