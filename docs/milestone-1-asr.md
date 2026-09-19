# Milestone 1 — local streaming ASR

**GATE PASSED (2026-09-19).** MLX in-process transcribes real speech end to end on an
M-series MacBook Air — load, stream, partials, finalize drain, clean exit.

| Fixed WAV, 24 kHz mono s16, 17.147 s | |
|---|---|
| quantization | 8-bit (group 64), quantized at load |
| model load | **4.16 s** warm (39.6 s on the first-ever load: cold cache + kernel compile) |
| real-time factor | **0.659** — 1.5× faster than real time |
| audio → first transcript | **767 ms** |
| model peak memory | **1694 MB** at 8-bit, 1921 MB at bf16 — quote this row, not `ru_maxrss` (§2) |
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

What is recovered is a **12 % slice** of the resident footprint, and a fifth of the
decode time. Measured, not assumed:

| `VNR_ASR_QUANT_BITS` | model peak memory | `ru_maxrss` | RTF |
|---|---|---|---|
| 8 | **1694 MB** | 1277 MB | **0.67x** |
| 0 (bf16) | **1921 MB** | 2184 MB | 0.84x |

Transcripts were identical at both settings, so 8-bit costs no accuracy here.

**Say 12 %, not "the model in memory is quantized."** The second phrasing invites the
reader to assume the ~50 % a bf16→int8 story implies, and the measurement does not support
it: 227 MB of 1921 MB. Most of the resident footprint is not the quantized weights — the
Mimi codec, activations and Metal scratch do not shrink with the LM's bit width.

The unadvertised win is speed: **20 % better real-time factor at 8-bit**, which matters
more to this product than the memory does.

Start at 8-bit. `unmute-mlx-bridge` documents 4-bit as corrupting the *TTS* model
(gibberish, mixed voices) and says nothing about STT, so 4-bit for STT is untested rather
than known-good. Try it second and compare transcripts.

### The memory number: quote `mx.get_peak_memory()`, distrust `ru_maxrss`

**`ru_maxrss` is the wrong instrument for an MLX model, and it is wrong in both
directions** — which is what makes it worse than merely imprecise:

| | model peak memory | `ru_maxrss` | `ru_maxrss` error |
|---|---|---|---|
| 8-bit | 1694 MB | 1277 MB | 417 MB **low** |
| bf16 | 1921 MB | 2184 MB | 263 MB **high** |

A consistent offset could be corrected for. A sign that flips with the setting under test
cannot: `ru_maxrss` ranked the two configurations *backwards*, reporting the quantized run
as using more than half a gigabyte less than the runtime figure and the unquantized run as
using more. `getrusage` counts resident pages; MLX allocates through Metal, which is
neither wholly inside nor wholly outside that accounting.

That is the whole explanation for the earlier non-result (bf16 1018/1016 MB, 8-bit
863/1034 MB — no separation, the 8-bit worst case above both bf16 runs). The instrument
could not see the thing being compared.

**Quote the `model peak memory` row. Treat `process peak RSS` as being about the process,
not the model.** The spike prints both, each naming its instrument, because a single
"memory" number would have hidden a disagreement this large.

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
| model peak memory | spike table | the only memory row that sees Metal allocations. 8-bit measured at 1694 MB against bf16's 1921 MB; far above that means quantization is not taking |
| process peak RSS | spike table | `ru_maxrss`, which errs in both directions (−417 MB at 8-bit, +263 MB at bf16) — about the process, not the model |
| input peak level | spike table | mic paths here land at 0.07–0.10 against 0.80 for a `say` file; see *Input level* below |
| real-time factor | `--file` run | ≥ 1.0 means it cannot keep up with speech |
| audio → first transcript | spike table | above ~1 s feels unresponsive while speaking |
| recording end → final | spike table | the model's ~0.5 s delay plus `VNR_ASR_FINALIZE_GRACE_MS` |
| stability | `--seconds 300` | drift, growing memory, output that stops mid-run |
| accuracy at 8 vs 4 bits | your judgement | see the phrase set below |

### Input level

Every microphone path measured on this machine is roughly ten times quieter than a
`say`-generated file, and the transcripts degrade alongside it:

| source | peak | transcript |
|---|---|---|
| `audio/test.wav` (`say`) | 0.802 | near-perfect |
| live mic (sounddevice) | 0.099 | mangled |
| `VNRCapture` | 0.072 | mangled |

Suggestive, and not yet a finding: three points with level and source-type confounded, and
a fourth that contradicts the trend outright — the capture that transcribed to **nothing**
peaked at 0.193, higher than either working file. So level is not monotonic with quality
and cannot be the whole story.

`--gain` turns that argument into a measurement on one file, holding everything else fixed:

```bash
uv run vnr-asr-spike --file /tmp/capture.wav              # as recorded
uv run vnr-asr-spike --file /tmp/capture.wav --gain 8     # same audio, louder
```

The spike then reports the device's peak and the model's separately, so a boosted quiet
microphone is never mistaken for a loud one. `VNR_ASR_INPUT_GAIN` sets the same thing for
the service path; it defaults to 1.0, and the gain is applied *after* the peak is recorded
so it can never disguise a failing device. Clamping, not wrapping — an Int16 pushed past
full scale would otherwise invert rather than merely distort.

If a gained file transcribes better, level is a real lever and the gain belongs in the
product path. If it does not, the quietness is a symptom of the same input processing that
strips the high frequencies (`macos/README.md` §"Slice 2"), and no amount of gain
recovers detail that was filtered out before capture.

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
