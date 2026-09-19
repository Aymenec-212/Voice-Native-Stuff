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

## Slice 2 — audio capture (this slice) ⚠️ blocked

Captures 24 kHz mono audio in exactly the shape the service wants and writes a WAV, so the
Python spike can transcribe it:

```bash
cd macos
PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/capture.wav 6
uv run vnr-asr-spike --file /tmp/capture.wav      # from the repo root
```

**That second command is the verification.** If the model transcribes the file, the sample
rate, channel count, bit depth and framing are all correct. If it rejects the file or
returns silence, they are not. No assertion can establish that; only the model that will
consume the audio in production can.

The capture tool reports frames produced, samples held back, dropped buffers and the peak
input level. It fails loudly if every sample is zero — the silent-device trap again — and
now **warns instead of reporting PASS** when the device runs below 24 kHz or the peak is
too low to trust. A run that ends `CAPTURED WITH n WARNING(S)` exits non-zero.

The bundle identifier does not change with `PRODUCT`, so all these tools share one
microphone grant: the probe asks for it, the others inherit it. `SIGN_IDENTITY` picks the
codesigning identity (default `-`, ad-hoc) — pass the one you signed with before, because
TCC keys the grant on the signature and re-signing ad-hoc silently throws the grant away:

```bash
SIGN_IDENTITY="VNR Dev" PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/capture.wav 6
```

Nothing consults `security find-identity -v -p codesigning`: it reports 0 valid identities
on a machine where `codesign --sign "VNR Dev"` works, so it is not evidence of anything.

### The blocker, and how to decide it

The file is structurally correct, audible, at a workable level (peak 0.193) — and the model
transcribes **nothing** from it, while the same words spoken into `vnr-asr-spike` live
transcribe fine. Structure, audibility, silence, level, framing arithmetic and Bluetooth
are all ruled out. The one stage the live path does not have is the 44.1 kHz → 24 kHz
conversion, which makes it the obvious suspect — but reading the converter found no
definite bug, and a rewrite on a hunch would prove nothing either way.

So the tool is instrumented to decide it rather than argue about it. It now writes **two**
files:

```
/tmp/capture.wav                 24 kHz mono, after our conversion — what the service gets
/tmp/capture-raw-44100.wav       the device's own rate, before any conversion
```

The second is the control. Resample it with a known-good tool and compare:

```bash
ffmpeg -i /tmp/capture-raw-44100.wav -ar 24000 -ac 1 -sample_fmt s16 /tmp/reference.wav
uv run vnr-asr-spike --file /tmp/reference.wav     # ffmpeg's resampling
uv run vnr-asr-spike --file /tmp/capture.wav       # ours
```

| Reference | Ours | Conclusion |
|---|---|---|
| transcribes | silent | our `AVAudioConverter` use is the bug — fix it here |
| silent | silent | the fault is upstream of conversion, in the capture itself |
| silent | transcribes | the raw dump or ffmpeg invocation is wrong, not the capture |

Then get the numbers, rather than another opinion:

```bash
uv run vnr-audio-diff /tmp/reference.wav /tmp/capture.wav
```

`vnr-audio-diff` prints both files side by side — rate, duration, peak, RMS, dBFS, DC
offset, clipped samples, zero-crossing rate, high-frequency energy share — and flags what
would stop a file transcribing. The row that matters most is **periodic jumps**: a
resampler whose state resets once per tap callback leaves a transient at every buffer
boundary, which at a 4096-frame callback and 44100 → 24000 lands every 2229 samples
(92.9 ms). That is texture to the ear and a wall to a model. The detector is checked
against exactly that injected fault in `tests/test_audio_analysis.py`.

### Getting a like-for-like reference

The live path can now write out exactly the frames it fed the model:

```bash
uv run vnr-asr-spike --seconds 8 --dump-wav /tmp/live.wav
uv run vnr-audio-diff /tmp/live.wav /tmp/capture.wav
```

Say the same sentence into both, and the comparison is two files rather than a file and a
memory of how one sounded. The tap sits between the source and the engine, so the dump is
the bytes the model stepped on — not a reconstruction of them.

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
Sources/VNRKit/        event vocabulary, session state, audio framing (pure Foundation)
Sources/VNRKitCheck/   the checks, as a runnable program — runs on macOS and Linux
Sources/VNRProbe/      the microphone permission probe — no audio capture
Sources/VNRCapture/    24 kHz mono capture → WAV, for the spike to transcribe
Resources/Info.plist   bundle template; NSMicrophoneUsageDescription lives here
scripts/make-app.sh    build → bundle → sign → run [args…]; also `reset`
Fixtures/events.json   generated by tests/test_event_contract.py
```

`VNRProbe` deliberately does **not** depend on `VNRKit`: if anything in the kit stops
compiling, the permission check still builds and runs.

`VNRProbe` and `VNRCapture` are `#if os(macOS)` stubs elsewhere, so `swift build` succeeds
on Linux — but CI compiles those *stubs*, not the AVFoundation and AppKit bodies behind
them. **A green CI does not mean the capture tool compiles**; only `swift build` on the Mac
establishes that. `VNRKit` and `VNRKitCheck` are pure Foundation and are fully covered,
which is the reason the audio rules live in `VNRKit` rather than in the capture tool: the
part that can be checked off-device is the part worth putting there.

## Next slices

3. WebSocket client against `ws://127.0.0.1:8765/ws`, rendering purely from the event
   stream; `/health` gates the record button.
4. Menu-bar item, global shortcut, the overlay, and the editable review field. The edit
   step is the product — see `docs/PLAN.md` §7.
5. Clickable citations from `cited_sources`, already numbered to match the `[n]` markers.
