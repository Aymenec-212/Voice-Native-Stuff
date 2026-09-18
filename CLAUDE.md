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
| ASR runtime | **MLX, in-process** (`MlxEngine` via `moshi_mlx`). moshi.cpp was tried and rejected — see the gate result below | Kyutai's designated Apple path, Metal GPU, no sidecar. moshi.cpp has no macOS support and no stdin path at all |
| ASR model | `kyutai/stt-1b-en_fr-mlx` (bf16 on disk) quantized at load via `VNR_ASR_QUANT_BITS=8` | **This is the §5 trade, recorded not hidden.** The 531 MB Q4_K GGUF is unused by this runtime. A `*.q8.safetensors` in the repo would remove the trade — check for one |
| ASR model files | `config.json` names the Mimi weights, the LM weights and the tokenizer | no filename is ever guessed; `VNR_ASR_MODEL_DIR` overrides the Hub |
| Audio capture | the **UI** captures and streams PCM over loopback | plan §19 lists `audio.frame` as a UI → service command |
| Citations | model cites `[S3]`; the **app** renumbers to `[1]` and builds the Sources list from Tavily URLs | makes invented URLs structurally impossible (plan §13) |
| Final answer streaming | tool/decision rounds are non-streaming; a separate **final synthesis** call is streamed | plan §17 "prioritize robustness"; avoids ambiguous mixed content+tool_call streams |

## 3. Milestone status

| # | Milestone | Status |
|---|---|---|
| 1 | Local streaming ASR | ⏳ **runtime decided: MLX** (moshi.cpp rejected — see gate result). `MlxEngine` written and unit-tested; never yet run against real weights |
| 2 | Nebius + Tavily research CLI | ✅ done, offline-tested; needs one live run to confirm |
| 3 | End-to-end local prototype | ✅ done — service + controller + terminal prototype, proven over two real processes |
| 4 | Native macOS UX | ☐ next, once the runtime actually transcribes |
| 5 | Reliability & metrics / eval set | ☐ prompts written (`docs/evaluation-set.md`), not run |
| 6 | Demo readiness | ☐ not started |

### Gate result: moshi.cpp rejected, MLX adopted

Recorded in full in `docs/milestone-1-asr.md` §1. The short version, from reading
`tools/moshi-stt.cpp` on the Mac:

- `-i -` can never work: the `-i` handler calls `file_exists()` and exits. With no `-i`
  the tool opens the mic itself via SDL. **There is no stdin path**, so the subprocess
  design had no route through it.
- `-r`, `-q` and `-m` all mean something other than what this repo assumed.
- It expects `Codes4Fun/*` GGUF packaging, not `efficient-nlp/stt-1b-en_fr-quantized`.
- **No macOS support at all** — Linux/Windows quick starts, CUDA/Vulkan/CPU backends, no
  Metal. This alone settles it; the `mkfifo` workaround was never worth testing.

The replacement is `MlxEngine`: `moshi_mlx` running **in our own process** on Metal. No
sidecar, no stdin, no stdout scraping. PLAN §19 is unchanged — the UI still captures PCM
and streams it over loopback.

**Still unproven:** the real model has never transcribed anything here. `MlxEngine`'s
orchestration is covered by tests against a fake backend (framing, worker thread, partials,
drain, cancel, failure), but every `moshi_mlx` call runs only on Apple Silicon. Run
`uv run vnr-asr-spike` before Milestone 4.

## 4. Repo map

```
docs/PLAN.md            condensed spec — the normative document
docs/milestone-1-asr.md how to build/run the ASR spike on macOS
src/vnr/config.py       env-backed configuration
src/vnr/events.py       the UI event vocabulary (asr.*, research.*)
src/vnr/session.py      ResearchSession state object
src/vnr/metrics.py      latency/token/credit instrumentation
src/vnr/asr/            engine protocol + adapters (mlx ← default, moshicpp, mock) + audio
src/vnr/research/       nebius, tavily, sources, citations, tools, prompts, agent
src/vnr/controller.py   session state machine (IDLE → LISTENING → REVIEW → …)
src/vnr/service.py      FastAPI WebSocket on 127.0.0.1
src/vnr/cli/            research CLI + ASR spike + terminal prototype
tests/                  unit + mocked-agent tests (run on Linux, no keys needed)
```

## 5. Open questions / things only the user can answer

Fill these in — the next session reads this section first.

- [ ] **First real transcription.** `uv pip install -e ".[dev,asr,service,mlx]"`, then
      `uv run vnr-asr-spike`. Paste the metrics table: model load (time the *second* run,
      the first downloads ~2 GB) · peak RSS · real-time factor · audio→first transcript ·
      5-minute stability · how Kyutai / Nebius / Tavily / NVIDIA came out.
- [ ] **Quantization.** Start at `VNR_ASR_QUANT_BITS=8`. Does `kyutai/stt-1b-en_fr-mlx`
      publish a `*.q8.safetensors` or `*.q4.safetensors`? If so set `VNR_ASR_WEIGHTS_NAME`
      to it — that makes the weights quantized on disk and closes the §5 trade. Then try
      4-bit and compare transcripts (4-bit is documented as corrupting the *TTS* model;
      for STT it is simply untested).
      → result:
- [ ] **First mic → GO → cited answer run.** `vnr-service` + `vnr-prototype`. The GO
      prompt is fixed (prompt_toolkit, prefilled for real), so this should now complete.

**Answered:**
- ✅ **Nebius model ID** — `nvidia/Nemotron-3_5-Lightning` is correct and a live research
  run completed (2026-09-18).
- ✅ **Model card** — `efficient-nlp/stt-1b-en_fr-quantized` names no runtime and no
  command. HF's `python -m moshi.server` snippet is auto-generated from its
  `library_name: moshi` tag and is wrong: the Python moshi stack cannot read GGUF.

## 6. How to work on this

```bash
uv venv && uv pip install -e ".[dev,service]"   # + ",asr,mlx" on Apple Silicon
uv run pytest                                  # 166 tests, offline, no keys needed
uv run ruff check .
```

Everything except the real ASR runtime and the live APIs is testable on Linux. The fakes
that make that possible: `tests/conftest.py` (FakeNebius/FakeTavily), `tests/fake_stt.py`
(a stand-in streaming STT binary), `FakeBackend` in `tests/test_mlx_engine.py` (stands in
for `moshi_mlx`), and `httpx.MockTransport` for the provider layer.

**The `moshi_mlx` boundary is the one place tests cannot reach.** `MlxEngine` is split so
that everything around inference is testable and only `MoshiMlxBackend`'s calls are not.
Keep that split when changing it.

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

- **2026-09-18** — Gate result recorded: **moshi.cpp rejected** (no stdin path, no macOS
  support, wrong flag semantics, wrong weights packaging). Runtime is now **MLX in-process**
  via `moshi_mlx`; the Q4_K GGUF is unused and the bf16-plus-load-quantization trade is
  documented rather than hidden. Fixed the GO prompt: stdlib `readline` silently ignores
  `set_startup_hook` under macOS libedit, so the line was never prefilled and every run
  cancelled — meaning `research.submit` from the prototype had never executed. Now
  prompt_toolkit with `prompt_async`, tested with simulated keystrokes. 35 new tests.
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
