<p align="center">
  <img src="docs/assets/banner.svg" alt="vnr" width="820">
</p>

<p align="center">
  <em>Voice-native web research for macOS.</em>
</p>

---

Hold ⌃⌥Space, ask a question out loud, let go. You get back a transcript you can fix
before anything is sent, and then an answer with real sources under it.

Speech recognition runs on your Mac. The audio never leaves it. When you press GO the
only thing that goes out is the sentence you just read and approved.

```
⌃⌥Space → speak → check the text → GO → searches run → cited answer
```

That third step matters more than it looks. Speech models get proper nouns wrong, and a
mangled name buys you a wasted search and a confidently irrelevant answer. You read the
text before anything is spent on it, and you can fix it first.

## What you need

- Apple Silicon Mac, macOS 13 or later
- [uv](https://docs.astral.sh/uv/)
- A [Nebius](https://studio.nebius.com/) key and a [Tavily](https://tavily.com/) key

The speech half is free and local. The keys are only for the research half.

## Running it

```bash
uv venv
uv pip install -e ".[dev,asr,service,mlx]"
cp .env.example .env          # fill in NEBIUS_API_KEY and TAVILY_API_KEY
```

Two processes. The service owns the speech model and makes every outbound call; the app
is just the menu bar and the overlay, and never sees a key.

```bash
uv run vnr-service                                     # terminal 1

cd macos && PRODUCT=VNRApp ./scripts/make-app.sh run    # terminal 2
```

That script wraps the binary in a proper `.app` before launching it, which is not
optional. A bare SwiftPM executable has no `NSMicrophoneUsageDescription`, and macOS
answers that by handing the process digital silence instead of an error. Took an
afternoon to work out the first time.

Say yes to the microphone prompt. The speech model is about 2 GB and downloads on your
first recording, so that one is slow; afterwards it stays warm between questions and
releases itself after five idle minutes.

### Without the menu bar

The same loop in a terminal, and a research-only CLI that skips speech entirely:

```bash
uv run vnr-prototype                                   # speak, edit, GO
uv run vnr-research "what changed in MLX quantization this year?"
```

## Settings worth knowing

Everything lives in `.env.example`, commented. The four that actually change behaviour:

| | |
|---|---|
| `VNR_ASR_QUANT_BITS=8` | Quantizes the speech model in memory after loading. Costs nothing measurable in accuracy, saves ~12% of resident memory and about 20% of decode time. 4-bit is untested for speech. |
| `VNR_ASR_IDLE_TIMEOUT_S=300` | How long the weights stay warm once you stop. They are released after this even if the app is still connected. |
| `RESEARCH_MAX_SEARCHES=4` | Hard ceiling on Tavily calls per question. |
| `RESEARCH_MAX_TURNS=6` | Hard ceiling on the agent loop, so one bad question cannot run up a bill. |

## Citations can't be faked

The model never writes a URL. It cites by ID — `[S1]`, `[S3]` — and the app renumbers
those to `[1]`, `[2]` and builds the source list from what Tavily actually returned during
that session. An ID that doesn't exist gets dropped and logged. There is no path by which
a made-up link reaches you, because the model was never holding one.

## Under the hood

Kyutai STT via MLX, resident in the service process and stepping 80 ms frames on the Metal
GPU. No sidecar, no subprocess. The research side is one model, one conversation, one tool
(`web_search`), one bounded loop — no framework, no sub-agents, no shell or filesystem
access. The app and the service talk over a WebSocket on `127.0.0.1`, and the UI renders
purely from the event stream rather than deciding anything for itself.

## Tests

```bash
uv run pytest         # offline: no keys, no network, no microphone
uv run ruff check .

cd macos && swift build && swift run VNRKitCheck
```

The ones that spend real credits are opt-in:

```bash
VNR_RUN_INTEGRATION=1 uv run pytest tests/integration -v
```

## Digging further

[`docs/PLAN.md`](docs/PLAN.md) is the spec everything here is held to.
[`macos/README.md`](macos/README.md) covers the Swift side, including why there is no
Xcode project. [`docs/milestone-1-asr.md`](docs/milestone-1-asr.md) has the speech runtime
and the measurements behind it, and
[`docs/reliability-2026-09-21.md`](docs/reliability-2026-09-21.md) covers memory, repeat
sessions and how reasoning is kept out of the answer.
[`CLAUDE.md`](CLAUDE.md) is where things stand and what is still open.
