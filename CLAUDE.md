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
| ASR model | `kyutai/stt-1b-en_fr-mlx` (bf16 on disk) quantized **after load** via `VNR_ASR_QUANT_BITS=8` | **This is the §5 trade, recorded not hidden.** The 531 MB Q4_K GGUF is unused. The repo is bf16 only (no `.q4`/`.q8`), so the disk footprint is unavoidable here — only the Candle route in `docs/milestone-1-asr.md` §6 would recover it. ⚠️ I previously claimed "the resident model is still quantized" — **no measurement supports that**; see §5 |
| Quantization ordering | on-disk `*.q4`/`*.q8` → quantize **before** `load_weights`; bf16 + `VNR_ASR_QUANT_BITS` → quantize **after** | opposite orders, identical-looking config. Getting it backwards fails with `Missing 196 parameters:` — all `.scales`/`.biases`. `plan_quantization()` keeps the two apart; load order is asserted against a recording double |
| ASR model files | `config.json` names the Mimi weights, the LM weights and the tokenizer | no filename is ever guessed; `VNR_ASR_MODEL_DIR` overrides the Hub |
| Audio capture | the **UI** captures and streams PCM over loopback | plan §19 lists `audio.frame` as a UI → service command |
| Swift verification | **no XCTest, ever.** Checks live in the `VNRKitCheck` executable | XCTest on Darwin ships inside Xcode, not the SDK, so a Command Line Tools install cannot run `swift test`. The user develops without Xcode. A runnable check also works on Linux, which is what makes CI possible |
| CI | GitHub Actions, both languages on stock free Linux runners | the Python suite is offline by design and `VNRKitCheck` is not XCTest, so neither needs macOS |
| Citations | model cites `[S3]`; the **app** renumbers to `[1]` and builds the Sources list from Tavily URLs | makes invented URLs structurally impossible (plan §13) |
| Final answer streaming | tool/decision rounds are non-streaming; a separate **final synthesis** call is streamed | plan §17 "prioritize robustness"; avoids ambiguous mixed content+tool_call streams |

## 3. Milestone status

| # | Milestone | Status |
|---|---|---|
| 1 | Local streaming ASR | ✅ **GATE PASSED 2026-09-19** — MLX in-process transcribes real speech on an M-series Air. RTF 0.659, first transcript 767 ms, warm load 4.16 s |
| 2 | Nebius + Tavily research CLI | ✅ done, offline-tested; needs one live run to confirm |
| 3 | End-to-end local prototype | ✅ done — service + controller + terminal prototype, proven over two real processes |
| 4 | Native macOS UX | ⏳ slice 1 ✅ (mic prompt passed on the Mac) · slice 2 written, unverified |
| 5 | Reliability & metrics / eval set | ☐ prompts written (`docs/evaluation-set.md`), not run |
| 6 | Demo readiness | ☐ not started |

### Gate result: PASSED on MLX, after rejecting moshi.cpp

Measured 2026-09-19 on a fixed 17.147 s WAV (24 kHz mono s16), 8-bit quantized at load:
RTF **0.659** · warm load **4.16 s** (39.6 s cold) · audio→first transcript **767 ms** ·
74 transcript updates · peak RSS 1034 MB (instrument suspect, see §5). Full table and the
two traps this cost us are in `docs/milestone-1-asr.md`.

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

**Two lessons worth carrying into M4**, both cases of a double that could not see the
defect it was standing in for:

- The **mock engine invents text per frame arriving and never inspects the samples**, so a
  microphone delivering digital silence (missing macOS permission, or capture routed to a
  Bluetooth headset) looked exactly like a successful run. The spike now reports an input
  peak level, and the controller raises rather than returning an empty transcript from a
  silent device.
- The **fake backend cannot see inside the backend**, which is how the quantize/load
  ordering bug survived. `MlxModules` is the second seam that fixed it.

When adding a double, ask what class of bug it makes invisible.

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
macos/                  Milestone 4 SwiftPM package — see macos/README.md
```

## 5. Open questions / things only the user can answer

- [ ] **Does load-time quantization actually reduce resident memory?** Measured peak RSS
      does not separate bf16 from 8-bit: bf16 1018/1016 MB, 8-bit 863/1034 MB across two
      runs each — 8-bit's worst run exceeds both bf16 runs. **The instrument was the
      prime suspect, so the spike now reports a second figure**: `model peak memory`,
      from `mx.get_peak_memory()`, which sees the Metal buffers `ru_maxrss` cannot.
      Re-run at bf16 and 8-bit and compare that row. If it separates, only the instrument
      was wrong; if it does not, the §2 mitigation is not real. Either answer is worth
      having — record it.
      → result:
- [ ] **Does the captured audio transcribe?** M4 slice 2:
      `PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/capture.wav 6`, then
      `uv run vnr-asr-spike --file /tmp/capture.wav`. The spike transcribing it is the
      proof that sample rate, channels, bit depth and framing are right — no assertion
      can establish that.
      → result:

- [ ] **Five-minute stability.** `uv run vnr-asr-spike --seconds 300` — drift, growing
      memory, output stopping mid-run.
- [ ] **Proper-noun baseline.** Record the §24 phrase set once (`docs/milestone-1-asr.md`
      §5) and keep the WAVs, so 8-bit vs 4-bit is a comparison rather than an impression.
- [ ] **4-bit quality.** Untested for STT; documented as corrupting the *TTS* model.
- [ ] **First mic → GO → cited answer run.** `vnr-service` + `vnr-prototype`.

**Answered:**
- ✅ **CI is live and the Swift actually runs.** First run on 2026-09-19: `swift build` and
  **92 VNRKitCheck assertions** passed in the `swift:5.9-jammy` container, alongside
  pytest + ruff — the first time any Swift in this project had been compiled by CI. Both
  jobs on free Linux runners.
- ✅ **M4 slice 1** — PASS (2026-09-19). `swift build` compiled clean on the first attempt;
  the dialog appeared, the grant landed on the bundle rather than on Terminal, so the
  `open` decision was right.
- ✅ **`swift test` is not available on this machine and never will be.** Command Line
  Tools has no XCTest (it lives in Xcode's Platform directory), and installing 15 GB of
  Xcode is not happening. The `could not determine XCTest paths` warning during
  `swift build` has the same cause and is cosmetic. **Everything to be verified must be
  reachable from `swift build`, `swift run` or a script.**
- ✅ **M1 gate** — passed on MLX. Numbers in §3.
- ✅ **The empty transcripts were not a code bug** — macOS microphone permission (TCC
  delivers silence, not an error) plus a Bluetooth earbud selected as input. Every frame
  stepped was zeros, so pad tokens on every step was correct behaviour.
- ✅ **Pre-quantized MLX weights** — none exist. `kyutai/stt-1b-en_fr-mlx` ships only
  `config.json`, the Mimi safetensors (385 MB), `model.safetensors` (1.98 GB bf16) and the
  tokenizer. `VNR_ASR_WEIGHTS_NAME` cannot close the §5 trade.
- ✅ **Nebius model ID** — `nvidia/Nemotron-3_5-Lightning` is correct and a live research
  run completed.
- ✅ **Model card** — `efficient-nlp/stt-1b-en_fr-quantized` names no runtime and no
  command; HF's `python -m moshi.server` snippet is auto-generated and wrong.

## 6. How to work on this

```bash
uv venv && uv pip install -e ".[dev,service]"   # + ",asr,mlx" on Apple Silicon
uv run pytest                                  # 191 tests, offline, no keys needed
uv run ruff check .

cd macos && swift build && swift run VNRKitCheck   # the Swift half
```

**CI runs both** on stock free Linux runners (`.github/workflows/ci.yml`): pytest + ruff,
and `swift build` + `swift run VNRKitCheck` in the official `swift:5.9-jammy` container.
`VNRProbe` and `VNRCapture` are `#if os(macOS)` stubs elsewhere, so the whole package
type-checks on Linux rather than just part of it. Green as of 2026-09-19: 191 Python
tests, 92 Swift checks.

`push` is scoped to `main`; PR branches are covered by `pull_request`. Matching both
(`branches: ["**"]`) ran the whole workflow twice per push.

Known warning, not worth a guess: `actions/checkout@v4` targets Node 20, which GitHub
now forces onto Node 24 with a deprecation notice. Bumping the action version would
silence it, but the version to bump *to* has not been verified from here, and a wrong
one turns green CI red.

Everything except the real ASR runtime and the live APIs is testable on Linux. The fakes
that make that possible: `tests/conftest.py` (FakeNebius/FakeTavily), `tests/fake_stt.py`
(a stand-in streaming STT binary), `FakeBackend` in `tests/test_mlx_engine.py` (stands in
for `moshi_mlx`), and `httpx.MockTransport` for the provider layer.

**The `moshi_mlx` boundary is the one place tests cannot reach.** `MlxEngine` is split so
that everything around inference is testable and only `MoshiMlxBackend`'s calls are not.
Keep that split when changing it. Note the second seam: `MlxModules` makes the imported
third-party modules injectable, so `load()` — including the quantization ordering — is
tested against a recording double even though the real modules only exist on Apple
Silicon. A fake *backend* cannot catch an ordering bug inside the backend.

## 7. Milestone 4 — native macOS UX (in progress)

**Slice 1 passed on 2026-09-19.** The bundle is correct, the dialog appears, and the grant
lands on the bundle rather than on the terminal that launched it.

**Slice 2 (audio capture) is written and unverified.** `VNRCapture` records 24 kHz mono
via `AVAudioEngine`, frames it exactly as the WebSocket will, and writes a WAV — which
`uv run vnr-asr-spike --file` then transcribes. That round trip is the verification:
only the model that will consume the audio can confirm the format is right.

The bundle is not an afterthought. A bare SwiftPM executable has no
`NSMicrophoneUsageDescription`, so TCC hands it digital silence or kills it outright —
the same failure that cost an afternoon during M1. So the first runnable artifact is
*"it asks for the mic"*, and no audio-capture code is written until that prompt has been
seen. The probe also checks for the usage string *before* requesting access, because
requesting without it does not return an error: the system terminates the process.

**The Swift↔Python contract is guarded from the Python side.** `VNRKit`'s decoding tests
run against `macos/Tests/VNRKitTests/Fixtures/events.json`, which
`tests/test_event_contract.py` generates and verifies. That test fails if the fixtures go
stale *or* if any `EventType` has no fixture. Since agent sessions cannot run
`swift test`, it is the only automatic guard — keep it that way.

Remaining slices, in order:

1. Connect to `ws://127.0.0.1:8765/ws`; render purely from the event stream.
2. Menu-bar item + global shortcut → `recording.start`; the captured frames → binary
   frames on the socket; Enter/click → `recording.stop`.
3. An overlay with the live transcript, then an **editable** field. GO sends
   `research.submit` carrying the edited text. That step is the product — do not skip it.
4. Progress rows from `research.search_started` / `search_completed`, the answer from
   `research.answer_delta`, and clickable citations from `cited_sources` on
   `research.completed` (already numbered to match the `[n]` markers in the text).
5. `/health` gates the record button, so recording stays disabled until the model is in.

**Constraint to plan around:** agent sessions run on Linux and cannot compile or run
Swift. The split is: the agent writes the Swift package and its tests; the user builds and
runs them from VS Code with the Swift extension and reports back, exactly as the ASR
runtime was handled. SwiftPM, no `.pbxproj` — a project file is not editable blind.

## 8. Session log

- **2026-09-19 (3)** — M4 slice 1 **passed** (clean first build, dialog appeared, grant on
  the bundle). Dropped the XCTest target: Command Line Tools has no XCTest, so
  `swift test` could never have run on the dev machine. Assertions moved to a
  `VNRKitCheck` executable, which also runs on Linux — and that unlocked **CI**
  (`.github/workflows/ci.yml`): pytest + ruff, and `swift build` + `VNRKitCheck` in the
  official Swift container, both on free Linux runners. Slice 2 written: `VNRCapture`
  records 24 kHz mono and writes a WAV for `vnr-asr-spike --file` to transcribe, which is
  the only real proof the format is right. Audio framing rules live in `VNRKit` so they
  are checkable off-device; only the AVAudioEngine plumbing needs the Mac.
- **2026-09-19 (2)** — Started **Milestone 4**. `macos/` SwiftPM package: event decoder,
  microphone-permission probe, and an app-bundling script — the bundle planned in from
  the start, because a bare SwiftPM binary has no `NSMicrophoneUsageDescription` and TCC
  answers that with silence or a kill. First runnable artifact is the permission prompt,
  not audio capture. Added `tests/test_event_contract.py`, which generates the fixtures
  the Swift tests decode and fails when they drift — the only automatic guard on the
  cross-language contract, since Swift cannot be compiled here. Also added
  `mx.get_peak_memory()` reporting so the RSS question has an instrument that can
  actually see Metal allocations. 8 new Python tests, 191 total; the Swift is unbuilt.
- **2026-09-19** — **M1 gate PASSED**: MLX in-process transcribes real speech, RTF 0.659.
  Fixed three things the run exposed. (1) `finalize_timeout_s` was a total budget, so a
  fast `--file` replay hit the timeout and reported it *as* a real-time factor — 0.587,
  entirely plausible, entirely a function of the constant. It is now a stall detector, and
  a cut-short drain forces `real_time_factor` to `None`. (2) Silent-input detection: macOS
  hands an unpermitted app zeros, which the mock engine cannot expose; the spike now
  reports an input peak level and the controller refuses to return an empty transcript
  from a silent device. (3) Cosmetics: `--engine mlx` was unselectable, the readiness-probe
  caveat fired for MLX where the load figure is exact, and the RSS number now names its
  instrument. Recorded that the bf16-vs-8-bit RSS comparison shows **no separation**, so
  the "resident model is quantized" claim is withdrawn pending a tool that can see Metal
  allocations. 7 new tests, 183 total.
- **2026-09-18 (2)** — Fixed the MLX quantization **ordering** bug found on the Mac:
  `nn.quantize` ran unconditionally before `load_weights`, so `VNR_ASR_QUANT_BITS=8`
  against a bf16 checkpoint failed with `Missing 196 parameters` (all `.scales`/`.biases`).
  `quantization_for()` conflated "quantized on disk" with "quantize after loading";
  `plan_quantization()` now returns both the bit width and *when*. A test asserted the
  broken combination, so it encoded the defect — replaced. The MLX modules are injectable
  now, so `MoshiMlxBackend.load()` is driven by a recording double and the call order is
  asserted directly; verified by reintroducing the bug and watching the test fail. Also
  made the spike's real-time factor honest (the finalize drain's silence is audio the
  model stepped, so it belongs in the denominator). 12 new tests, 176 total.
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
