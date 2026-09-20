# Milestone 4 — the native macOS client

A SwiftPM package, built and run from VS Code with the Swift extension. No `.pbxproj`:
a project file is painful to edit in an agent session that cannot compile Swift, and
nothing here needs one.

**The split:** the agent writes Swift and its tests on Linux and cannot build them; you
build, run and report back. Same arrangement that got the ASR runtime working.

## Slice 1 — the microphone prompt ✅ passed 2026-09-19

The first runnable artifact is *"it asks for the mic"*, not *"it captures audio"*.

A bare SwiftPM executable has no `Info.plist`, which means no
`NSMicrophoneUsageDescription` and no bundle identifier. macOS then either hands the
process digital silence or terminates it outright when access is requested — and neither
looks like a permission problem from the inside. That is an afternoon lost, and it has
already been lost once on this project. So the bundle is built and verified before any
audio code exists.

```bash
cd macos
./scripts/make-app.sh run
```

That builds `VNRProbe`, wraps it in `dist/VoiceNativeResearch.app` with the usage
description, ad-hoc signs it, launches it through `open`, and prints its log.

Expected on a first run: a macOS permission dialog, then

```
Status before asking: not determined (never asked)
Requesting access — macOS should now show a permission dialog…

Answer: GRANTED
Status after asking: authorized

PASS — the bundle is correct and the app holds a microphone grant.
```

To see the prompt again after answering once:

```bash
./scripts/make-app.sh reset      # tccutil reset Microphone com.aymenec.voicenativeresearch
```

### Why `open` rather than running the binary

Launched from a shell, the terminal becomes the *responsible process* for TCC, so the
grant lands on Terminal (or VS Code) instead of on this app — and you end up debugging a
permission that was never ours. `open` gives the bundle its own identity. Running
`dist/VoiceNativeResearch.app/Contents/MacOS/VNRProbe` directly is still useful when the
app fails to launch at all and you need the error on stderr.

### What the probe checks, in order

1. Is there a bundle, and does it have an identifier? TCC keys grants by bundle id.
2. **Is `NSMicrophoneUsageDescription` present?** Checked *before* requesting access,
   because requesting without it does not return an error — the system kills the process.
3. Authorization status before asking.
4. The request itself, waiting up to 120 s for an answer.
5. Status afterwards.

Each failure prints what to do next rather than a status code.

## Slice 2 — audio capture ✅ passed 2026-09-19

`VNRCapture` records 24 kHz mono, frames it exactly as the WebSocket will, writes a WAV,
and the model transcribes it:

```bash
cd macos
PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/capture.wav 8
uv run vnr-asr-spike --file /tmp/capture.wav      # from the repo root
```

That second command is the verification — no assertion can establish that the sample rate,
channel count, bit depth and framing are right; only the model that consumes the audio can.

`SIGN_IDENTITY` picks the codesigning identity (default `-`, ad-hoc). Pass the one you
signed with before, because TCC keys the grant on the signature and re-signing ad-hoc
throws the grant away:

```bash
SIGN_IDENTITY="VNR Dev" PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/capture.wav 8
```

Nothing consults `security find-identity -v -p codesigning`: it reports 0 valid identities
on a machine where `codesign --sign "VNR Dev"` works, so it is not evidence of anything.

### The bisect result: the resampling was never the problem

The pre-conversion dump settled it. Feeding our raw 44.1 kHz tap through ffmpeg's
resampler and through ours produced files that differ on nothing:

| | ffmpeg's resample | ours |
|---|---|---|
| duration | 7.90 s | 7.88 s |
| peak | 0.0722 | 0.0721 |
| rms | 0.0125 | 0.0125 |
| dBFS | −38.1 | −38.1 |
| energy above 6 kHz | 1.3 % | 1.1 % |
| periodic jumps | none | none |

Both transcribe. **`AVAudioConverter` is exonerated and needs no rewrite.** The suspicion
was reasonable and wrong, which is the case for bisecting rather than rewriting: a rewrite
would have "fixed" it and taught us nothing.

### What actually changed — not yet settled

Between the file that transcribed to nothing and the one that works, exactly two things
changed in the audio path:

1. the tap moved from before `engine.prepare()` to one second after `engine.start()`;
2. a chime with a one-second lead-in was added — which also changes **when the speaker
   starts talking**.

The evidence leans hard toward (2) being the whole story:

- the failing file peaked at **0.193** and the working one at **0.072**. Quality went up
  as level went *down*, which no conversion or gain explanation predicts;
- the failing run produced **zero** transcript updates. Degraded speech yields a bad
  transcript, not an empty one — the live mic at peak 0.099 gave 71 updates. Zero is the
  signature of no speech present;
- the old tool printed "Recording — speak now…" to a log nobody can watch while it runs,
  and recording started the instant the engine did.

That is a hypothesis, not a result, and an accidental fix can be accidentally undone. One
run settles it:

```bash
PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/legacy.wav 8 --legacy-tap-order
uv run vnr-asr-spike --file /tmp/legacy.wav
```

`--legacy-tap-order` restores the old ordering and keeps everything else, chime included,
so you speak through the whole take. Transcribes → the ordering was never the fault and
the original file simply had no speech in it. Silent → the ordering *was* the fix, and it
gets pinned rather than left to where a line happens to sit. `--no-lead-in` isolates the
other half if needed. Both flags exist only to answer this and should be deleted once it
is answered.

### The real signal: high frequencies lost before any conversion

`vnr-audio-diff /tmp/live.wav /tmp/capture.wav` — the same mic, two capture APIs:

```
energy above 6 kHz   5.8%  ->  1.1%
rms ratio            0.790
```

`live.wav` is sounddevice asking CoreAudio for 24 kHz; `capture.wav` is AVAudioEngine's
44.1 kHz tap. ffmpeg's resample of the raw dump also lands at 1.3 %, so **the high
frequencies are gone before any conversion** — the raw tap never had them. That points at
speech enhancement on the input node or a different device mode.

`VNRCapture` now reports `isVoiceProcessingEnabled` and turns it off before reading the
input format — that order matters, because toggling it changes the node's format, and a
converter built from the earlier one would be converting from a description that no longer
applies. System-level enhancement is out of the process's reach, so check **Control Center
→ Mic Mode → Standard** by hand: Voice Isolation removes exactly what the model uses.

### Level

Every microphone path here is about ten times quieter than a `say`-generated file, and the
transcripts degrade alongside:

| source | peak | transcript |
|---|---|---|
| `audio/test.wav` (`say`) | 0.802 | near-perfect |
| live mic | 0.099 | mangled |
| VNRCapture | 0.072 | mangled |

Tempting, but three points with source-type and level confounded — and the failing capture
at **0.193** produced nothing at all, which breaks the monotonic story outright. So level
is a hypothesis, and `--gain` makes it a measurement on one file rather than an argument:

```bash
uv run vnr-asr-spike --file /tmp/capture.wav --gain 8
```

Same audio, same model, one variable. If the transcript improves, level is the lever and
`VNR_ASR_INPUT_GAIN` belongs in the product path (it is already wired through the
controller, so both capture routes get it). If it does not, the quietness is a symptom of
the same enhancement that took the high frequencies, and gain will not buy it back.

## Slice 3 — the WebSocket client ✅ passed 2026-09-20

```bash
uv run vnr-service --engine mock          # terminal 1, from the repo root
cd macos && swift run VNRClient           # terminal 2

# the whole loop, no microphone needed:
swift run VNRClient --file /tmp/capture.wav --submit "compare Kyutai and Nebius"
```

Verified end to end on the Mac: connect → `idle (ready)` → `listening` → 98 frames →
partials → `finalizingTranscript` → final transcript → `review` → submit → `submitted` →
`researchStarted` → 4 searches → `synthesizing` (18 sources) → `answerStreaming` →
`completed`. A real answer, 10 cited sources, `[n]` numbering matching the markers,
4 Tavily credits, 11.9 s.

**The review step was visibly the gate.** The submitted query differed from the
transcript, and only the submitted text reached research — which is PLAN §7's guarantee
observed rather than asserted.

### Rendering from the stream, and why it is structural

`SessionModel` is the only thing that decides what is on screen, and the only way to
change it is `apply(_ event:)`. The UI holds no opinion about what happens next: it does
not decide that pressing GO starts research, or that a search finished — it applies what
the service said.

That is not tidiness. The controller is the single authority on session state — PLAN §7's
guarantee is that research never starts without an explicit GO — and a UI that *predicts*
state can disagree with it. A UI that only renders cannot. It also means the whole render
layer is a pure function of recorded events, so `VNRKitCheck` drives it on Linux.

Two behaviours worth naming, because appending would have been the obvious wrong choice:

- **search rows are keyed by index, not appended.** `search_started` and
  `search_completed` carry the same index, and a completion can arrive for a start that
  was missed. Appending shows the same search twice; keying shows it once, in order.
- **`research.started` clears the previous answer, sources and failure.** Otherwise a
  failed run's error sits under a fresh question.

### Three bugs the run found, and where each was fixed

**1. Sources were drawn twice.** The answer text ended with a Sources block *and* the
client rendered its own from `cited_sources`. The fix was not in the client: the agent was
streaming the block as a trailing `answer_delta` *in addition to* the structured list
(`agent.py` even carried a comment anticipating the tension). Now **the answer is prose
and sources travel structured**, and each presentation layer renders them — the overlay as
clickable rows, the CLIs through `render_cited_sources`.

That also buys an invariant worth having: `result.answer` is now exactly the concatenation
of the deltas, so a client that accumulates them cannot drift from the session object.
`tests/test_agent.py` asserts it directly.

**2. A rejected connection did not stop the sender.** With another client holding the
model, the reader failed with `.busy`, printed it — and the scripted sender carried on
issuing commands into a dead socket, so the run looked like it had done something.
`ServiceClient` now ends the session on any reader failure *and* on a clean close; every
`send` after that throws the reason the session ended. `VNRClient` stopped swallowing send
errors with `try?`, which is what made the symptom invisible.

**3. Identical partials re-rendered.** Streaming ASR re-emits the same text many times —
around thirty in one short utterance — and every one drew a line. `apply(_ event:)` now
returns whether anything actually changed, and `run` passes that to the callback.

The flag is reported rather than used to suppress the callback, because an event can
matter without changing the model: `synthesizing` stores nothing but means "show the
spinner". The view layer decides. It is computed per case rather than by comparing whole
models, since an answer delta would otherwise re-compare a string that grows with every
token.

### What is checked where

| Piece | Where | Checked on Linux |
|---|---|---|
| `ServiceEndpoint` — loopback-only URL building | `VNRKit` | ✅ |
| `ClientCommand` — the five command strings and their JSON | `VNRKit` | ✅ |
| `ServiceHealth` — the record-button gate | `VNRKit` | ✅ |
| `SessionModel` — everything drawn, and the change flag | `VNRKit` | ✅ |
| `ServiceClient` — folding frames in, and ending the session | `VNRKit` | ✅ via `ScriptedChannel` |
| `URLSessionChannel` — the actual socket | `VNRKit`, `#if os(macOS)` | ❌ needs the Mac |
| `VNRClient` — the runnable client | `VNRClient`, `#if os(macOS)` | ❌ needs the Mac |

The split is the same one that made `MlxEngine` testable: everything around the transport
is checkable off-device, and only the transport needs a Mac. `ScriptedChannel` is a fake
*transport*, not a fake client — it cannot hide a bug in how events are folded into the
model, because it does no folding.

### The endpoint refuses a non-loopback host

PLAN §2: raw audio never leaves the Mac, and this client streams raw audio. So
`ServiceEndpoint` rejects anything that is not `127.0.0.1`, `localhost` or `::1` — a host
in a config file is exactly how "local only" quietly stops being true. A name that merely
*resolves* to loopback today is not accepted either; DNS is not a security boundary.

### The contract is guarded from the Python side, in both directions

`macos/Fixtures/events.json` covered service → UI. Nothing covered UI → service, which is
the half this slice depends on — and the service *rejects* an unknown command rather than
ignoring it, so a mis-spelled string in Swift fails only on a Mac.
`tests/test_event_contract.py` now reads the command strings out of `_command()`'s own
`match` statement with `ast` and requires each to appear in `ClientCommand.swift`;
`tests/test_service.py` drives a whole session using the literal strings `jsonText()`
emits, sorted keys and all — `research.submit` goes out as `{"query":…,"type":…}`, with
the type *second*. The busy close code (4409) is pinned the same way, and a test asserts
the streamed answer carries no Sources block.

## Checks — not `swift test`

```bash
cd macos && swift run VNRKitCheck
```

**There is no test target, deliberately.** XCTest on Darwin lives in Xcode's Platform
directory rather than in the SDK, so a Command Line Tools install cannot run `swift test`
at all — and this project is developed without Xcode. Rather than require a 15 GB download
for a few hundred assertions, the checks are an ordinary executable that exits non-zero on
failure.

That choice pays twice: `VNRKit` is pure Foundation, so the same checks run on **Linux**,
which is what puts both languages under one CI workflow on a stock free runner.

**Anything you want verified must be reachable from `swift build`, `swift run` or a
script.** If a check needs XCTest, it will never run.

The fixtures in `Fixtures/events.json` are **generated by the Python suite**, not written
by hand:

```bash
VNR_UPDATE_FIXTURES=1 uv run pytest tests/test_event_contract.py
```

`tests/test_event_contract.py` fails if the checked-in fixtures no longer match what the
service emits, and separately fails if any `EventType` has no fixture at all. Since agent
sessions cannot compile Swift, that Python test is the other half of the guard — keep it
that way.

## Layout

```
Package.swift
Sources/VNRKit/        event vocabulary, session state, audio framing, service client
  AudioFraming.swift     24 kHz mono framing rules, shared with the Python side
  ServiceEvent.swift     service → UI events; unknown types decode, never throw
  SessionState.swift     the controller's states, decoded totally
  ServiceEndpoint.swift  loopback-only URL building
  ClientCommand.swift    UI → service commands and their JSON
  ServiceHealth.swift    /health, and the record-button gate
  SessionModel.swift     everything drawn, derived from events alone
  ServiceClient.swift    the transport protocol + a URLSession channel (macOS-only)
Sources/VNRKitCheck/   the checks, as a runnable program — runs on macOS and Linux
Sources/VNRProbe/      the microphone permission probe — no audio capture
Sources/VNRCapture/    24 kHz mono capture → WAV, for the spike to transcribe
Sources/VNRClient/     connects to the service and renders the event stream
Resources/Info.plist   bundle template; NSMicrophoneUsageDescription lives here
scripts/make-app.sh    build → bundle → sign → run [args…]; also `reset`
Fixtures/events.json   generated by tests/test_event_contract.py
```

`VNRProbe` deliberately does **not** depend on `VNRKit`: if anything in the kit stops
compiling, the permission check still builds and runs.

`VNRProbe`, `VNRCapture` and `VNRClient` are `#if os(macOS)` stubs elsewhere, so
`swift build` succeeds on Linux — but CI compiles those *stubs*, not the AVFoundation,
AppKit and URLSession bodies behind them. **A green CI does not mean those tools compile**;
only `swift build` on the Mac establishes that. `VNRKit` and `VNRKitCheck` are pure
Foundation and are fully covered, which is the reason the audio rules, the event
vocabulary, the command vocabulary and the whole render model live in `VNRKit` rather than
in the tools: the part that can be checked off-device is the part worth putting there.
`URLSessionChannel` is the only piece of the client that is not.

## Next slices

4. Menu-bar item, global shortcut, the overlay, and the editable review field. The edit
   step is the product — see `docs/PLAN.md` §7. `SessionModel` already carries everything
   it needs to draw: transcript, `awaitingApproval`, search rows, answer and citations.
5. Clickable citations from `cited_sources`, already numbered to match the `[n]` markers.
