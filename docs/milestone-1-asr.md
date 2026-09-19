# Milestone 1 — local streaming ASR

**GATE PASSED (2026-09-19).** MLX in-process transcribes real speech end to end on an
M-series MacBook Air — load, stream, partials, finalize drain, clean exit.

| Fixed WAV, 24 kHz mono s16, 17.147 s | |
|---|---|
| quantization | 8-bit (group 64), quantized at load |
| model load | **4.16 s** warm (39.6 s on the first-ever load: cold cache + kernel compile) |
| real-time factor | **0.659** — 1.5× faster than real time |
| audio → first transcript | **767 ms** |
| peak resident memory | 1034 MB — but see *The memory number is not evidence* below |
| recording end → final | 11.27 s (backlog from a fast replay, not live latency) |
| transcript updates | 74 |

The earlier gate result stands on its own: **moshi.cpp is not viable for this project**
(§1), and the plan says a clear failure is a valid outcome. What follows is what was
tried, why it failed, and what replaced it.

---

## 1. The gate result: moshi.cpp is out

Read from `tools/moshi-stt.cpp` and the project's own README:

| Finding | Consequence |
|---|---|
| `-i` calls `file_exists()` on its argument and exits with *"failed to find input file"* | `-i -` never worked. With no `-i` the tool opens the mic itself through SDL — there is **no stdin path at all** |
| `-r` is `--model-root` (the *parent* of a `kyutai/` folder); `-q` is `--quantize` (quantize-on-load for safetensors, paired with `-g` to cache a GGUF); `-m` is `--model`, a directory containing `config.json` | The `-r {model_dir} -q {quant}` command this repo shipped was wrong on every flag |
| Its own aria2 scripts fetch `Codes4Fun/stt-1b-en_fr-GGUF` + `Codes4Fun/moshi-common` (Mimi as GGUF) | `efficient-nlp/stt-1b-en_fr-quantized` is unrelated packaging it does not expect |
| Quick starts, binary releases, every documented backend and every benchmark row are Linux/Windows on CUDA, Vulkan or CPU. No Metal flag exists | **No macOS support.** Even a working FIFO would leave us CPU-only on Apple Silicon |

A `mkfifo /tmp/vnr.wav` workaround would satisfy `file_exists()`, and the decoder is picked
by extension, so it *might* have fed audio in. It was not tried, because the missing macOS
support defeats the approach before the FIFO question matters — and a CPU-only 1B model on
an Apple laptop is the wrong end state regardless.

### What the model card actually says

`efficient-nlp/stt-1b-en_fr-quantized` names **no runtime and no command**. Four sentences:
a quantized version of Kyutai `stt-1b-en_fr`, Q8_0 and Q4_K GGUF. Hugging Face's
`python -m moshi.server` snippet on that page is auto-generated from the `library_name:
moshi` tag — and the Python moshi stack does not read GGUF. The snippet is noise.

So the Q4_K GGUF in `models/stt-q4k/` is **not used by the chosen runtime**. Keep it; it is
the input to the Candle route below if that is ever revisited.

## 2. The runtime: MLX, in-process

Kyutai's own designated on-device path, and the only one with first-party Apple Silicon
support (they run the 1B model on an iPhone 16 Pro). `moshi_mlx` is a Python package, so
there is no sidecar at all: the model loads inside our service and runs on the Metal GPU.

```
UI captures PCM ─(loopback WS)─▶ service ─▶ MlxEngine ─▶ moshi_mlx on Metal
```

PLAN §19 is unchanged — the UI still captures audio and streams it over loopback. What
disappears is the child process, its stdin, and the text-scraping that went with it.

### The trade, stated plainly

The MLX checkpoint is bf16 on disk (~2 GB), not the 531 MB Q4_K GGUF. PLAN §5's critical
rule is that the BF16 checkpoint must never be a **silent** substitution — so it is
recorded here, in `CLAUDE.md` §2, and in the README.

**The disk footprint cannot be avoided on this path.** `kyutai/stt-1b-en_fr-mlx` contains
only `config.json`, `mimi-pytorch-e351c8d8@125.safetensors` (385 MB),
`model.safetensors` (1.98 GB, bf16) and `tokenizer_en_fr_audio_8000.model`. There is no
`.q4`/`.q8` variant, so `VNR_ASR_WEIGHTS_NAME` cannot rescue it. Only the Candle route in
§6 would.

What is recovered is the **resident** footprint: `VNR_ASR_QUANT_BITS=4|8` runs
`nn.quantize` after loading, so the model in memory is quantized even though the file is
not.

Start at 8-bit. `unmute-mlx-bridge` documents 4-bit as corrupting the *TTS* model
(gibberish, mixed voices) and says nothing about STT, so 4-bit for STT is untested rather
than known-good. Try it second and compare transcripts.

### Why the ordering matters

Quantization has two shapes that look identical in config and are opposites in practice:

| Checkpoint | `nn.quantize` runs | Because |
|---|---|---|
| `*.q4` / `*.q8` (quantized on disk) | **before** `load_weights` | the module tree needs QuantizedLinear/QuantizedEmbedding slots for the `.scales`/`.biases` the file carries |
| bf16 + `VNR_ASR_QUANT_BITS` | **after** `load_weights` | quantizing first makes the tree demand `.scales`/`.biases` a bf16 file does not have |

Getting this backwards fails at load with `Missing 196 parameters:` listing nothing but
`.scales` and `.biases`. Kyutai's own scripts quantize-then-load because their
`--quantized` flag also selects a pre-quantized filename — the two decisions are fused
there and must not be here. `plan_quantization()` keeps them separate, and the load order
is asserted by tests against a recording double.

## 3. Setup

```bash
uv pip install -e ".[dev,asr,service,mlx]"     # mlx is Apple Silicon only
```

`.env`:

```bash
VNR_ASR_ENGINE=mlx
VNR_ASR_HF_REPO=kyutai/stt-1b-en_fr-mlx    # fetched on first run and cached
VNR_ASR_QUANT_BITS=8                       # 4 or 8; unset infers from the filename
# VNR_ASR_WEIGHTS_NAME=model.q8.safetensors   # if the repo publishes one
# VNR_ASR_MODEL_DIR=/path/to/local/dir        # skips the Hub entirely
```

`config.json` in the repo names the Mimi weights, the LM weights and the tokenizer, so no
filename is ever guessed. `moshi_mlx` targets Python 3.12+; if your venv is 3.11 and the
install resists, rebuild it with `uv venv --python 3.12`.

The frame geometry already matches: Kyutai steps on 1920 samples of 24 kHz mono, which is
this project's existing 80 ms frame. The wire may carry `s16le` (default, half the
bandwidth) or `f32le`; the engine converts.

## 4. Run it

```bash
uv run vnr-asr-spike                        # speak, press Enter
uv run vnr-asr-spike --seconds 300          # stability over five minutes
uv run vnr-asr-spike --file audio/test.wav --json   # repeatable, gives a real-time factor
```

A real-time factor needs a file, because a live microphone runs at 1.0× by definition.
Record one at the rate the model wants:

```bash
mkdir -p audio
# list devices first: ffmpeg -f avfoundation -list_devices true -i ""
ffmpeg -f avfoundation -i ":0" -ac 1 -ar 24000 -sample_fmt s16 -t 20 audio/test.wav
```

The reported factor is wall time to the final transcript over the audio the model stepped,
including the silence the finalize drain pushes through it.

Then the whole path:

```bash
uv run vnr-service        # terminal 1
uv run vnr-prototype      # terminal 2
```

macOS will ask for microphone permission the first time. If it does not, check System
Settings → Privacy & Security → Microphone.

## 5. Record these numbers in `CLAUDE.md` §5

| Measurement | Where it comes from | What would worry us |
|---|---|---|
| model load time | spike header | first run also downloads ~2 GB; time the *second* run |
| peak resident memory | spike table | at 8-bit expect well under the bf16 footprint; near 2 GB means quantization is not taking |
| real-time factor | `--file` run | ≥ 1.0 means it cannot keep up with speech |
| audio → first transcript | spike table | above ~1 s feels unresponsive while speaking |
| recording end → final | spike table | the model's ~0.5 s delay plus `VNR_ASR_FINALIZE_GRACE_MS` |
| stability | `--seconds 300` | drift, growing memory, output that stops mid-run |
| accuracy at 8 vs 4 bits | your judgement | see the phrase set below |

### The fixed phrase set (PLAN §24)

Record once, replay with `--file`, so runs are comparable.

- clean English: *"Find recent work on streaming ASR for low-resource languages."*
- clean French: *"Trouve les travaux récents sur la reconnaissance vocale en streaming."*
- proper nouns: *"Compare Kyutai, Nebius, Tavily and NVIDIA Nemotron."*
- natural pauses: a sentence with two deliberate two-second gaps
- a long request: three clauses, ~20 seconds
- moderate background noise: any of the above with music or a fan running

Proper nouns matter most: they are what you will be fixing in the review step, and they are
the words that make or break a search query.

## 6. Options that were weighed and not taken

**Candle sidecar loading the real Q4_K GGUF.** The strongest fit for PLAN §5 — it uses the
531 MB file already downloaded, and Candle has a Metal backend. The proof it can work is
the Space `efficient-nlp/wasm-streaming-speech`, which loads exactly these four files and
streams 24 kHz mono float32 in 1024-sample chunks via Rust/Candle compiled to WASM.

It was not chosen because Candle ships no Kyutai STT example — `candle-examples` has
`mimi`, `encodec` and `silero-vad`, but no moshi or STT — so this means writing a Rust
binary against kyutai's crates and porting that Space's bespoke GGUF loading. Unknown
effort, real chance of a dead end, and it buys disk footprint rather than capability.

Revisit it only if MLX's memory or load time turns out to be unacceptable in practice.
The first step would be reading the Space's source, which decides whether this is a port or
a rewrite.

**`unmute-mlx-bridge` as a sidecar.** A published, maintained Apple Silicon STT server
speaking `moshi-server`'s MessagePack-over-WebSocket protocol
(`ws://127.0.0.1:8090/api/asr-streaming`), quantizable via `STT_QUANTIZE_BITS`. Faster to a
demo than writing `MlxEngine`, but it adds a third-party process to supervise and gives up
direct control of session boundaries, for the same bf16 disk footprint. It is the obvious
fallback if `moshi_mlx` proves awkward to drive in-process — and a useful sanity check that
the MLX stack works on your machine at all.

**Kyutai's Rust `moshi-server`.** The production path, but it serves the `-candle` bf16
weights and its documented deployments are CUDA. No advantage over MLX here.
