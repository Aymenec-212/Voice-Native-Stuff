# Milestone 1 — quantized streaming ASR spike

The hard gate. Nothing in Milestone 3 or 4 starts until this passes.

**Goal:** microphone → Q4_K Kyutai STT → continuously updating transcript, running locally
on Apple Silicon, with weights loaded once and kept resident. Nothing else.

> **Honesty note.** The agent session that wrote this code has no microphone and no
> Hugging Face access, so the runtime below is *unvalidated*. The adapter was verified
> end-to-end against a fake streaming binary (`tests/fake_stt.py`), which proves the
> plumbing — process lifetime, PCM on stdin, incremental parsing, finalize grace, error
> surfacing — but not the model. Everything model-specific is configuration you point at
> whatever you actually build.

---

## The critical rule

Do **not** silently substitute `kyutai/stt-1b-en_fr-mlx`. It is Apple-Silicon friendly,
but it is still a ~1.98 GB BF16 checkpoint, and avoiding exactly that is the point of this
milestone. If the quantized path turns out to be unworkable, that is a finding to record
in `CLAUDE.md` and decide on deliberately — not something to paper over.

For the same reason the mock engine must be asked for by name (`--engine mock`) and
announces itself in yellow. It proves nothing about this milestone.

## 1. Get the weights

```bash
# Q4_K is the target. Q8_0 is the fallback if Q4_K accuracy disappoints.
uv tool install huggingface-hub
hf download efficient-nlp/stt-1b-en_fr-quantized --local-dir models/stt-q4k
ls -lh models/stt-q4k        # models/ is gitignored
```

The download is a **directory of four things**, not one file, and the runtime needs all of
them:

```
model-q4k.gguf                        531M   the quantized language model  ← the point
model-q80.gguf                        1.0G   the fallback quantization
mimi-pytorch-e351c8d8@125.safetensors 367M   the Mimi audio codec
tokenizer_en_fr_audio_8000.{json,model}      the tokenizer
config.json                                  how they fit together
```

That is why `VNR_ASR_MODEL_DIR` points at the directory and `VNR_ASR_QUANT` picks the
quantization, rather than a single `VNR_ASR_MODEL` path.

Read that repo's model card before going further: **if it names a specific runtime or
command, that is authoritative over anything below.** Paste it into `CLAUDE.md` §5 so the
next session has it.

## 2. Build a runtime that loads GGUF

The Kyutai stacks (`moshi`, `moshi-mlx`, the Rust `moshi-server`) do not read GGUF. The
path that does is a ggml port — `Codes4Fun/moshi.cpp` is the one to try first:

```bash
git clone https://github.com/Codes4Fun/moshi.cpp
cd moshi.cpp && mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release       # add the Metal flag its README documents
cmake --build . -j
./moshi-stt --help                        # ← read this carefully, it drives step 3
```

Check three things in that help output:

1. **Can it read audio from stdin?** The README only shows `-i seashells.mp3`, so `-i -`
   is the assumption in the default command and the one thing most likely to be wrong.
   If it cannot, see *If stdin is not supported* below — that path is already supported
   and needs no code change.
2. **Does it print anything when the weights finish loading?** That string becomes
   `VNR_ASR_READY_MARKER`, and without it the reported load time is only a lower bound.
3. **What PCM format does it expect?** 24 kHz mono is what Kyutai wants; `s16le` and
   `f32le` are both supported by the adapter.

## 3. Configure the adapter

In `.env`:

```bash
VNR_ASR_ENGINE=moshicpp
VNR_ASR_BINARY=/absolute/path/to/moshi.cpp/build/moshi-stt
VNR_ASR_MODEL_DIR=/absolute/path/to/models/stt-q4k
VNR_ASR_QUANT=q4_k

# Placeholders: {binary} {model_dir} {model} {quant} {sample_rate}
VNR_ASR_COMMAND={binary} -r {model_dir} -q {quant} -i -

VNR_ASR_STDIN_FORMAT=s16le        # or f32le
VNR_ASR_OUTPUT_FORMAT=text        # or json, if you wrap it in something that emits JSON lines
VNR_ASR_READY_MARKER=             # e.g. "model loaded" — strongly recommended
```

Check the argv before running anything:

```bash
uv run vnr-asr-spike --print-command
```

If the binary rejects a flag, it exits immediately and the spike shows you its stderr
rather than hanging.

## 4. Run the spike

```bash
uv pip install -e ".[dev,asr]"     # sounddevice needs the asr extra

uv run vnr-asr-spike                       # speak, then press Enter
uv run vnr-asr-spike --seconds 300         # stability over five minutes
uv run vnr-asr-spike --file audio/test.wav --json   # repeatable, gives a real-time factor
```

macOS will ask for microphone permission the first time. If it does not, check System
Settings → Privacy & Security → Microphone for your terminal.

Real-time factor is only meaningful with `--file` (audio is pushed as fast as the runtime
accepts it). A live microphone runs at 1.0× by definition.

## 5. Record these numbers in `CLAUDE.md` §5

| Measurement | Where it comes from | What would worry us |
|---|---|---|
| model load time | spike header (set the ready marker first) | more than a few seconds on every app start |
| peak resident memory | spike table (samples the child process) | anything near the BF16 footprint — that suggests the quantization isn't being used |
| real-time factor | `--file` run | ≥ 1.0× means it cannot keep up with speech |
| audio → first transcript | spike table | above ~1s feels unresponsive while speaking |
| recording end → final | spike table | includes the model's ~0.5s decoding delay plus `VNR_ASR_FINALIZE_GRACE_MS` |
| stability | `--seconds 300` | drift, growing memory, output that stops mid-run |
| accuracy | your judgement | see the fixed phrase set below |

### The fixed phrase set (PLAN §24)

Record these once and replay them with `--file` so runs are comparable. The goal is not a
WER benchmark — it is whether the transcript-review step makes the errors tolerable.

- clean English: *"Find recent work on streaming ASR for low-resource languages."*
- clean French: *"Trouve les travaux récents sur la reconnaissance vocale en streaming."*
- proper nouns: *"Compare Kyutai, Nebius, Tavily and NVIDIA Nemotron."*
- natural pauses: a sentence with two deliberate two-second gaps
- a long request: three clauses, ~20 seconds
- moderate background noise: any of the above with music or a fan running

Proper nouns are the ones that matter most here: they are exactly what the user will have
to fix in the review step, and they are the words that make or break a search query.

## If stdin is not supported

Don't fork the C++. Write a ~40-line shim that owns the binary's preferred input and emits
JSON lines on stdout, then point `VNR_ASR_COMMAND` at the shim and set
`VNR_ASR_OUTPUT_FORMAT=json`. The adapter accepts:

```json
{"text": "the transcript so far"}
{"text": " more words", "delta": true}
{"text": "the final transcript", "final": true}
```

`tests/fake_stt.py` is a working example of the process contract.

## Gate criteria

Milestone 1 passes when all of these hold:

- [ ] the Q4_K (or Q8_0) GGUF runs locally on Apple Silicon — no BF16 checkpoint loaded
- [ ] transcript updates appear *while speaking*, not only at the end
- [ ] the process is spawned once and reused across several utterances
- [ ] real-time factor comfortably below 1.0
- [ ] memory stays flat over a five-minute run
- [ ] proper nouns are close enough that the review step is a correction, not a retype

Record the outcome in `CLAUDE.md` — including a failure. A clear "Q4_K is too inaccurate,
Q8_0 costs N MB more and is fine" is a perfectly good gate result.
