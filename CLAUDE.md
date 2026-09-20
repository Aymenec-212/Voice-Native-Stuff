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
| ASR model | `kyutai/stt-1b-en_fr-mlx` (bf16 on disk) quantized **after load** via `VNR_ASR_QUANT_BITS=8` | **This is the §5 trade, recorded not hidden.** The 531 MB Q4_K GGUF is unused. The repo is bf16 only (no `.q4`/`.q8`), so the disk footprint is unavoidable here — only the Candle route in `docs/milestone-1-asr.md` §6 would recover it. **Measured:** 8-bit saves **227 MB of 1921 MB (12 %)** and **20 % of decode time** (RTF 0.67x vs 0.84x), with identical transcripts. Say 12 %, not "the resident model is quantized" — that phrasing implies the ~50 % the measurement does not show |
| Quantization ordering | on-disk `*.q4`/`*.q8` → quantize **before** `load_weights`; bf16 + `VNR_ASR_QUANT_BITS` → quantize **after** | opposite orders, identical-looking config. Getting it backwards fails with `Missing 196 parameters:` — all `.scales`/`.biases`. `plan_quantization()` keeps the two apart; load order is asserted against a recording double |
| ASR model files | `config.json` names the Mimi weights, the LM weights and the tokenizer | no filename is ever guessed; `VNR_ASR_MODEL_DIR` overrides the Hub |
| Audio capture | the **UI** captures and streams PCM over loopback | plan §19 lists `audio.frame` as a UI → service command |
| Swift verification | **no XCTest, ever.** Checks live in the `VNRKitCheck` executable | XCTest on Darwin ships inside Xcode, not the SDK, so a Command Line Tools install cannot run `swift test`. The user develops without Xcode. A runnable check also works on Linux, which is what makes CI possible |
| CI | GitHub Actions, both languages on stock free Linux runners | the Python suite is offline by design and `VNRKitCheck` is not XCTest, so neither needs macOS |
| Citations | model cites `[S3]`; the **app** renumbers to `[1]` and builds the Sources list from Tavily URLs | makes invented URLs structurally impossible (plan §13) |
| Final answer streaming | tool/decision rounds are non-streaming; a separate **final synthesis** call is streamed | plan §17 "prioritize robustness"; avoids ambiguous mixed content+tool_call streams |
| Answer vs sources | `answer` is **prose only**; sources travel as the structured `cited_sources` list on `research.completed`, and **each presentation layer renders them** | the agent used to stream a text Sources block *as well*, so any consumer using both drew the list twice — the macOS client did. One consequence worth keeping: `result.answer` is now exactly the concatenation of the deltas, so a client that accumulates them cannot drift from the session object |

## 3. Milestone status

| # | Milestone | Status |
|---|---|---|
| 1 | Local streaming ASR | ✅ **GATE PASSED 2026-09-19** — MLX in-process transcribes real speech on an M-series Air. RTF 0.659, first transcript 767 ms, warm load 4.16 s |
| 2 | Nebius + Tavily research CLI | ✅ done, offline-tested; needs one live run to confirm |
| 3 | End-to-end local prototype | ✅ done — service + controller + terminal prototype, proven over two real processes |
| 4 | Native macOS UX | ⏳ slice 1 ✅ · slice 2 ✅ · slice 3 ✅ (2026-09-20 — full loop verified: mic frames → transcript → GO → cited answer) · slice 4 (menu bar + overlay + editable review) is next |
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
src/vnr/audio_analysis.py  numeric audio forensics (pure functions; numpy)
src/vnr/asr/            engine protocol + adapters (mlx ← default, moshicpp, mock) + audio
src/vnr/research/       nebius, tavily, sources, citations, tools, prompts, agent
src/vnr/controller.py   session state machine (IDLE → LISTENING → REVIEW → …)
src/vnr/service.py      FastAPI WebSocket on 127.0.0.1
src/vnr/cli/            research CLI + ASR spike + terminal prototype + vnr-audio-diff
tests/                  unit + mocked-agent tests (run on Linux, no keys needed)
macos/                  Milestone 4 SwiftPM package — see macos/README.md
  Sources/VNRKit/         events, state, audio framing, endpoint, commands, health,
                          SessionModel (everything drawn), ServiceClient
  Sources/VNRClient/      connects to the service and renders the stream (macOS-only)
```

## 5. Open questions / things only the user can answer

- [ ] **What actually fixed the slice-2 capture?** The only open question from slice 2,
      and it matters because an accidental fix can be accidentally undone. Two things
      changed in the audio path between the broken and working versions: the tap moved
      from before `engine.prepare()` to a second after `engine.start()`, and a chime with
      a lead-in was added — which also changes when the speaker starts talking. Evidence
      leans hard toward the second (peak went *down* 0.193 → 0.072 while quality went up;
      the failing run gave **zero** transcript updates, the signature of no speech rather
      than bad speech). One run decides it:
      ```
      PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/legacy.wav 8 --legacy-tap-order
      uv run vnr-asr-spike --file /tmp/legacy.wav
      ```
      transcribes → ordering was never the fault, the original take had no speech in it;
      silent → ordering was the fix and must be pinned. Delete both debug flags after.
      → result:
- [ ] **Is the low input level actually costing transcript quality?** Mic paths land at
      0.07–0.10 against 0.80 for a `say` file and transcribe worse — but level and
      source-type are confounded, and the capture that transcribed to *nothing* peaked
      **higher** (0.193) than either working file. One file, one variable:
      `uv run vnr-asr-spike --file /tmp/capture.wav --gain 8` against the same file
      ungained. Better → `VNR_ASR_INPUT_GAIN` belongs in the product path (already wired
      through the controller). No better → the quietness is a symptom of the input
      processing that also stripped the high frequencies, and gain cannot buy it back.
      → result:
- [ ] **Is macOS input processing stripping high frequencies?** The capture path loses
      content the sounddevice path keeps (1.1 % of energy above 6 kHz against 5.8 %), and
      it is already gone in the raw 44.1 kHz tap, before any conversion. `VNRCapture` now
      disables `isVoiceProcessingEnabled` before reading the input format, but
      system-level enhancement is out of reach of the process: check **Control Center →
      Mic Mode → Standard** and re-measure with `vnr-audio-diff`.
      → result:

- [ ] **Five-minute stability.** `uv run vnr-asr-spike --seconds 300` — drift, growing
      memory, output stopping mid-run.
- [ ] **Proper-noun baseline.** Record the §24 phrase set once (`docs/milestone-1-asr.md`
      §5) and keep the WAVs, so 8-bit vs 4-bit is a comparison rather than an impression.
- [ ] **4-bit quality.** Untested for STT; documented as corrupting the *TTS* model.
- [ ] **First mic → GO → cited answer run.** `vnr-service` + `vnr-prototype`.

**Answered:**
- ✅ **M4 slice 3** — PASS (2026-09-20). `swift build` clean, 164 checks, and the full
      loop verified on the Mac: connect → listening → 98 frames → partials → final
      transcript → review → submit → 4 searches → synthesizing (18 sources) → streamed
      answer → completed. Real answer, 10 cited sources, correct `[n]` numbering, 4 Tavily
      credits, 11.9 s. `URLSessionWebSocketTask` behaves. **The review step was visibly
      the gate**: the submitted query differed from the transcript and only the submitted
      text reached research — PLAN §7 observed rather than asserted. Three bugs it found
      are fixed; see §7.
- ✅ **M4 slice 2** — PASS (2026-09-19). `swift build` clean; the captured file transcribes.
- ✅ **The 44.1 → 24 kHz conversion was never the bug.** The pre-conversion dump settled
      it: ffmpeg's resample and ours differ on nothing measurable (duration 7.90 vs 7.88 s,
      peak 0.0722 vs 0.0721, rms identical, HF 1.3 % vs 1.1 %, no periodic jumps in
      either) and both transcribe. `AVAudioConverter` needs no rewrite. The suspicion was
      reasonable and wrong — which is the argument for bisecting rather than rewriting: a
      rewrite would have "fixed" it and taught us nothing.
- ✅ **Does load-time quantization reduce resident memory? Yes, modestly.** 8-bit 1694 MB
      vs bf16 1921 MB — 227 MB, **12 %**, not the ~50 % the bf16→int8 story implies. Most
      of the footprint is not the quantized weights. Identical transcripts, so no accuracy
      cost, and a **20 % better RTF** (0.67x vs 0.84x), which is the bigger win.
- ✅ **`ru_maxrss` is unusable for this model — it errs in both directions.** 417 MB *low*
      at 8-bit, 263 MB *high* at bf16, so it ranked the two configurations backwards. A
      consistent offset could be corrected for; a sign that flips with the setting under
      test cannot. Quote `mx.get_peak_memory()`; treat `process peak RSS` as being about
      the process, not the model.
- ✅ **The finalize drain is a stall detector, not a budget.** Confirmed on the Mac:
      RTF 0.66x with no override needed, and a cut-short drain still invalidates the
      metric rather than reporting the timeout as a measurement.
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
uv run pytest                                  # 242 tests, offline, no keys needed
uv run ruff check .

cd macos && swift build && swift run VNRKitCheck   # the Swift half
```

**CI runs both** on stock free Linux runners (`.github/workflows/ci.yml`): pytest + ruff,
and `swift build` + `swift run VNRKitCheck` in the official `swift:5.9-jammy` container.
Green as of 2026-09-20: 242 Python tests, 179 Swift checks.

**Know what that green covers.** `VNRProbe` and `VNRCapture` are `#if os(macOS)` stubs on
Linux, so CI compiles their *stubs*, not their real bodies. Every line of AVFoundation and
AppKit in them is unbuilt until you run `swift build` on the Mac. `VNRKit` and
`VNRKitCheck` are pure Foundation and are fully covered — which is why the audio rules
live in `VNRKit` rather than in the capture tool. A green CI means the contract is intact;
it never means the capture tool compiles.

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

**Slice 2 passed on 2026-09-19.** `VNRCapture` records 24 kHz mono via `AVAudioEngine`,
frames it exactly as the WebSocket will, writes a WAV, and the model transcribes it. The
round trip is the verification — no assertion can establish the format is right; only the
model that consumes it can.

**The bisect exonerated the converter.** ffmpeg's resample of our raw tap and our own
differ on nothing measurable, and both transcribe. The 44.1 → 24 kHz conversion was the
obvious suspect and was not the bug — which is exactly why it was bisected rather than
rewritten. Numbers and the decision table: `macos/README.md` §"Slice 2".

**One thing is still open: which of the two changes actually fixed it.** The tap moved
from before `engine.prepare()` to a second after `engine.start()`, *and* a chime with a
lead-in was added — and the chime changes when the speaker starts talking. The evidence
leans hard toward the chime (peak fell 0.193 → 0.072 while quality rose; the failing run
gave zero transcript updates, which is the signature of no speech rather than bad speech).
`--legacy-tap-order` settles it in one run — see §5. **Delete both debug flags once it is
answered**; they exist only for that.

**Two findings worth carrying forward**, both in §5:

- the capture path loses high frequencies the sounddevice path keeps (1.1 % of energy
  above 6 kHz against 5.8 %), and the loss is already in the raw 44.1 kHz tap. `VNRCapture`
  now disables voice processing *before* reading the input format — toggling it changes
  the node's format, so the old order built the converter from a stale description;
- every mic path is ~10x quieter than a `say` file. `VNR_ASR_INPUT_GAIN` / `--gain` makes
  that testable on one file; it is applied after the peak is recorded, so gain can never
  disguise a failing microphone, and it clamps rather than wraps.

`make-app.sh` takes `SIGN_IDENTITY` — it used to re-sign ad-hoc on every build, silently
discarding a real signature and with it the TCC grant.

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

**Slice 3 passed on 2026-09-20.** `VNRClient` connects to `ws://127.0.0.1:8765/ws` and
the full loop works end to end on the Mac. `SessionModel` renders **purely from the event
stream** — `apply(_ event:)` is the only mutation. That is structural, not tidy: the
controller is the single authority on session state (PLAN §7 — research never starts
without an explicit GO), and a UI that *predicts* state can disagree with it. A UI that
only renders cannot. `/health` gates the record button through `ServiceHealth.Gate`, which
distinguishes loading from a failed load from nothing answering, and every case explains
itself so the button is never dead and silent.

`ServiceEndpoint` refuses a non-loopback host. PLAN §2 says raw audio never leaves the
Mac and this client streams raw audio, so a host in a config file is not allowed to undo
that. A name that merely resolves to loopback is refused too.

**Three bugs the run found, all fixed** (detail in `macos/README.md` §"Slice 3"):

1. **Sources drawn twice.** Not a client bug: the agent streamed a text Sources block as
   a trailing `answer_delta` *and* sent the structured list. Fixed at the source — see
   the §2 decision. The invariant `result.answer == "".join(deltas)` is now tested.
2. **A rejected connection did not stop the sender.** The reader failed with `.busy` and
   the sender kept issuing commands into a dead socket. `ServiceClient` now ends the
   session on any reader failure *and* on a clean close, and every later `send` throws the
   reason it ended. `VNRClient` stopped swallowing send errors with `try?`, which is what
   made it invisible.
3. **Identical partials re-rendered.** `apply(_ event:)` now returns whether anything
   changed, and `run` passes that through. Reported rather than used to suppress the
   callback, because an event can matter without changing the model (`synthesizing`
   stores nothing but means "show the spinner") — the view layer decides.

**The contract is guarded in both directions.** `macos/Fixtures/events.json` covered
service → UI; nothing covered UI → service, which is the half this slice depends on, and
the service *rejects* an unknown command rather than ignoring it. `test_event_contract.py`
now reads the command strings out of `_command()`'s own `match` with `ast` and requires
each in `ClientCommand.swift`; `test_service.py` drives a whole session with the literal
strings `jsonText()` emits. Both were verified by breaking each side and watching the
tests fail.

Remaining slices, in order — **slice 4 is next and unblocked**:

1. Menu-bar item + global shortcut → `recording.start`; the captured frames → binary
   frames on the socket; Enter/click → `recording.stop`.
2. An overlay with the live transcript, then an **editable** field. GO sends
   `research.submit` carrying the edited text. That step is the product — do not skip it.
   `SessionModel` already carries what it needs: `transcript`, `transcriptIsFinal`,
   `awaitingApproval`.
3. Progress rows from `research.search_started` / `search_completed`, the answer from
   `research.answer_delta`, and clickable citations from `cited_sources` on
   `research.completed` (already numbered to match the `[n]` markers in the text).
   `SessionModel.searches` keys rows by index rather than appending, because a completion
   can arrive for a start that was missed — appending would show the same search twice.

**Constraint to plan around:** agent sessions run on Linux and cannot compile or run
Swift. The split is: the agent writes the Swift package and its tests; the user builds and
runs them from VS Code with the Swift extension and reports back, exactly as the ASR
runtime was handled. SwiftPM, no `.pbxproj` — a project file is not editable blind.

## 8. Session log

- **2026-09-20** — **Slice 3 passed**: the full loop runs on the Mac — mic frames →
  transcript → review → GO → 4 searches → streamed answer with 10 cited sources. The
  review step was visibly the gate, which is PLAN §7 observed rather than asserted. Fixed
  the three bugs the run exposed. The first was not where it looked: sources rendered
  twice because the *agent* streamed a text Sources block in addition to the structured
  list, so the fix moved rendering to every presentation layer and left `answer` as prose
  — which also makes `result.answer` exactly the concatenation of the deltas, an invariant
  a delta-accumulating client depends on and which is now tested. The second was a session
  that did not end: a refused connection failed the reader while the sender kept writing
  into a dead socket, hidden by `try?` at every call site. The third was the flicker
  source: `apply` now reports whether anything changed, passed through rather than used to
  drop callbacks, since an event can matter without changing the model. 4 new Python
  tests, 242 total.
- **2026-09-19 (6)** — **Slice 3 written**: the WebSocket client. `VNRKit` gains
  `ServiceEndpoint` (loopback-only by construction), `ClientCommand`, `ServiceHealth` with
  a gate that explains itself in every state, `SessionModel` — everything drawn, derived
  from events alone — and `ServiceClient` over an injectable `WebSocketChannel`; only
  `URLSessionChannel` and `VNRClient` need a Mac. Same split that made `MlxEngine`
  testable, and `ScriptedChannel` is a fake *transport* rather than a fake client, so it
  cannot hide a bug in how events fold into the model. Closed the other half of the
  cross-language contract: the command vocabulary is now read out of the service's own
  `match` statement with `ast` and required to appear in the Swift, and a whole session is
  driven with the literal JSON `jsonText()` emits (sorted keys put `query` before `type`).
  Both guards were verified by breaking each side. Also drove the real `vnr-service`
  process over a real socket with exactly those strings — opening state, binary audio
  frames, transcript, submit — so the contract is proven live from Linux even though the
  Swift is unbuilt. Fixed `vnr-service --engine`, which offered only `moshicpp` and
  `mock`. 6 new Python tests, 238 total.
- **2026-09-19 (5)** — **Slice 2 passed.** The bisect did its job and cleared the prime
  suspect: ffmpeg's resample of the raw tap and ours differ on nothing, and both
  transcribe — `AVAudioConverter` never needed rewriting. What remains open is *which* of
  the two changes fixed it, so `--legacy-tap-order` exists to answer that in one run
  rather than leaving an accidental fix unpinned. Fixed the detector's false positive: it
  flagged the known-good file at "every 3 samples" (an 8 kHz tone, not a callback) while
  passing the file under suspicion, so periods below 100 samples are no longer candidates
  — the share is still measured against every gap, so a bright file scores low rather than
  scoring 100 % on whatever survived a filter. Added an input-gain stage
  (`VNR_ASR_INPUT_GAIN`, `--gain`) through both the controller and the spike, applied
  after the peak is recorded so it cannot mask a dead microphone, and clamping rather than
  wrapping. `VNRCapture` now disables voice processing before reading the input format,
  since toggling it changes that format. Recorded the settled §5 memory question: 8-bit
  saves 227 MB of 1921 MB (12 %, not ~50 %) and 20 % of decode time, with identical
  transcripts; `ru_maxrss` errs in *both* directions and ranked the two backwards, so the
  runtime figure is the one to quote. 13 new Python tests, 232 total.
- **2026-09-19 (4)** — Slice 2 came back **blocked**: a capture that is structurally
  correct, audible and at a workable level transcribes to nothing. Rather than rewrite the
  suspect stage on a hunch, built the instruments to decide it — `VNRCapture` now also
  dumps the pre-conversion audio at the device's rate, `vnr-asr-spike --dump-wav` writes
  the exact frames the live path fed the model (byte-identical to its input, which is what
  makes it usable as a reference), and `vnr-audio-diff` compares two files numerically.
  Its periodic-discontinuity detector is aimed at the specific fault suspected here and is
  tested against it by injection. Also from the Mac report: `requestAccess` instead of
  refusing, warnings instead of PASS below 24 kHz or at a low peak, dropped-buffer
  counting, an audible cue with a lead-in, and `SIGN_IDENTITY` in `make-app.sh` (which had
  been discarding the user's signature — and the microphone grant with it — on every
  build). Recorded the answered half of the §5 memory question: `ru_maxrss` misses ~600 MB
  of MLX's Metal allocations (1694 vs 1070 MB on one run), so the earlier bf16-vs-8-bit
  comparison was noise; the comparison itself still needs a run on the right instrument.
  28 new Python tests, 219 total; 7 new Swift checks.
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
