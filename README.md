<p align="center">
  <img src="docs/assets/banner.svg" alt="vnr" width="820">
</p>

<p align="center">
  <em>Voice-native web research for macOS.</em>
</p>

---

Run `vnr voice`, speak, then press Enter. You get back a transcript you can fix
before anything is sent, and then an answer with real sources under it.

Speech recognition runs on your Mac. The audio never leaves it. When you press GO the
only thing that goes out is the sentence you just read and approved.

```
record → speak → check the text → GO → searches run → cited answer
```

That third step matters more than it looks. Speech models get proper nouns wrong, and a
mangled name buys you a wasted search and a confidently irrelevant answer. You read the
text before anything is spent on it, and you can fix it first.

## What you need

- Apple Silicon Mac, macOS 13 or later
- [uv](https://docs.astral.sh/uv/)
- A [Nebius](https://studio.nebius.com/) key and a [Tavily](https://tavily.com/) key

The speech half is free and local. The keys are only for the research half.

## Start from the terminal

Voice requires **Apple Silicon macOS**, Python 3.11+, [uv](https://docs.astral.sh/uv/),
a microphone, and internet for the initial model download and research. Research uses
paid Nebius and Tavily accounts. Text-only research also runs on Linux.

```bash
git clone https://github.com/Aymenec-212/Voice-Native-Stuff.git
cd Voice-Native-Stuff
uv venv --python 3.11
uv pip install -e ".[asr,service,mlx]"
cp .env.example .env
```

Edit `.env` with your `NEBIUS_API_KEY` and `TAVILY_API_KEY`. Keep the other defaults,
including `VNR_ASR_QUANT_BITS=8`. Do not overwrite an existing `.env`; it is gitignored.
Run commands from the repository directory:

```bash
uv run vnr doctor                  # local setup check; never prints keys
uv run vnr serve                   # terminal 1: keep this running
uv run vnr voice                   # terminal 2: record → review → research → repeat
```

Wait for **Listening** before speaking. First use downloads the BF16 checkpoint and
loads/quantizes it; subsequent questions reuse resident weights. Press Enter to stop,
edit the prefilled transcript, then Enter to approve it. Clear the line to cancel.
After the answer, Enter starts another recording, `t` shows the optional model trace,
and `q` quits. Ctrl-C exits and closes the session. Stop the service separately with Ctrl-C.
Allow microphone access for your terminal in macOS System Settings when prompted.

The service listens only on loopback. The terminal sends audio only to that local service.
External research starts only after transcript approval. Missing research keys do not
prevent the ASR service from starting; configure them before using GO.

### Text research and evidence inspection

For text-only use, install with `uv pip install -e .` and run `uv run vnr doctor --text`.
No service or speech model is needed:

```bash
uv run vnr ask --list-models
uv run vnr ask --save runs "When is FC Barcelona men's next match? Verify the date using official sources."
uv run vnr inspect runs/SESSION.json       # use the filename printed by --save
uv run vnr inspect runs/SESSION.json --reasoning
uv run vnr ask --json "Compare two streaming ASR approaches" > answer.json
```

The terminal shows actual search queries, result counts, a streamed Markdown answer,
source links, token/search costs and latency. Saved text sessions include retrieved snippets,
searches and the mapping between answer citations and source IDs. `inspect` shows cited
evidence; add `--all-sources` to include uncited retrievals. Inspecting a saved session
makes no network calls. Reasoning is hidden by default and is **not evidence**.
`--json` writes one JSON document to stdout; progress stays on stderr. Redirected ordinary
answers remain plain Markdown. Session files contain your query, retrieved text and model
trace; saving is opt-in. Voice sessions currently stay in memory and do not produce these
saved evidence files.

Useful options: `vnr voice --once`, `vnr ask --max-searches 2`, `vnr ask --events`, and
`vnr COMMAND --help`. Earlier `vnr-service`, `vnr-prototype`, and `vnr-research` commands
remain available. Full recording script and verification checklist: [demo guide](docs/demo.md).

### How the ASR model is served

`vnr serve` runs Kyutai's MLX speech stack in-process on the Metal GPU. It loads on demand,
then quantizes the language-model weights at load time. The checkpoint on disk is BF16;
8-bit is not a promise that every runtime allocation or the audio codec uses 8 bits.
Metal scratch cache is capped at 128 MiB by default. Per-utterance attention state is freed
between recordings while weights remain resident; after five idle minutes weights and
cache are unloaded. No cold start per recording. See [measured memory and throughput](docs/reliability-2026-09-21.md).

### Optional native menu bar

Keep `uv run vnr serve` running, then in another terminal:

```bash
cd macos && PRODUCT=VNRApp ./scripts/make-app.sh run
```

Use the packaged `.app` so macOS receives its microphone permission declaration.
Do not run native and terminal voice clients at the same time.

## Settings worth knowing

Everything lives in `.env.example`, commented. The four that actually change behaviour:

| | |
|---|---|
| `VNR_ASR_QUANT_BITS=8` | Quantizes the speech model in memory after loading. The disk checkpoint remains BF16. Accuracy and speed depend on the workload; 4-bit quality remains unmeasured. |
| `VNR_ASR_IDLE_TIMEOUT_S=300` | How long the weights stay warm once you stop. They are released after this even if the app is still connected. |
| `RESEARCH_MAX_SEARCHES=4` | Hard ceiling on Tavily calls per question. |
| `RESEARCH_MAX_TURNS=6` | Hard ceiling on the agent loop, so one bad question cannot run up a bill. |

## What citation validation proves

The prompt asks the model to cite retrieved source IDs such as `[S1]`. The application
renumbers recognized IDs to `[1]`, `[2]` and builds the source list from URLs returned by
Tavily in this session. Unknown IDs are dropped and reported. This validates citation
membership, **not whether a claim is true or supported**. Open the cited page, compare the
claim to its evidence, and check publication dates, event dates and timezone. Search
snippets can be incomplete or stale; the agent does not independently fetch full pages.

## Under the hood

Kyutai STT via MLX, resident in the service process and stepping 80 ms frames on the Metal
GPU. No sidecar, no subprocess. The research side is one model, one conversation, one tool
(`web_search`), one bounded loop — no framework, no sub-agents, no shell or filesystem
access. The app and the service talk over a WebSocket on `127.0.0.1`, and the UI renders
purely from the event stream rather than deciding anything for itself.

## Tests

```bash
uv pip install -e ".[dev,service]"
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
