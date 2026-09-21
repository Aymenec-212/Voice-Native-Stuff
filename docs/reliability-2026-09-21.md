# Repeat sessions, reasoning, time, and loaded memory

Hardware verification on 2026-09-21, Apple Silicon, configured 8-bit Kyutai MLX model.

## Repeat questions

`SessionModel` previously cleared the previous result only on `research.started`,
which cannot happen until the user presses GO. The overlay selected its answer page
whenever `completion` was present, so a second recording's transcript and GO button
were hidden behind that first result. Reset (`IDLE`) and a new `LISTENING` transition
now clear transcript, answer, reasoning, completion, citations, searches and failures.
Repeated readiness updates during listening do not erase an active transcript.

Checks cover both reset and direct re-recording, plus two full questions on one service
WebSocket. No service or app restart is required.

## Separate reasoning from the answer

Nebius responses can carry `reasoning_content`/`reasoning`, or `<think>` content tags.
The provider boundary separates both forms, including tags split across SSE chunks.
Reasoning travels as `research.reasoning_delta`, is retained separately in session JSON,
and is shown under a collapsed **Model reasoning** disclosure. Citation rewriting and
answer-first-token metrics consume only answer text. Small reasoning chunks are batched
before sending UI events. Untagged prose is not heuristically classified as reasoning.

Synthesis has a bounded additional 4096-token allowance
(`RESEARCH_REASONING_MAX_TOKENS`) on top of the 2048 answer allowance. These sum to the
provider's total output limit; they do not independently constrain the model's thinking.
No final answer, or a length-truncated answer, now produces an actionable retry error
while keeping already received reasoning available. It is not reported as a completed
answer. Thinking remains enabled.

A live regression run of “When is FC Barcelona's next match?” returned a separate final
answer and 19,844 characters of reasoning; none of that reasoning entered the answer.
This verifies separation, not the correctness of every claim made by the provider.

## Time-sensitive questions

There was already a date in the system prompt. Both request startup and final synthesis
now read a fresh local timestamp with its UTC offset, with explicit instructions to
compare event dates against that clock, distinguish publication dates from event dates,
and verify upcoming matches against current official schedules. The clock is supplied
automatically, avoiding a model decision about whether to call a time tool.

Offline checks cover midnight rollover and UTC offsets. The live Barcelona regression
used September 21 as its reference and did not report September 20 as an upcoming event.
Current clock context cannot by itself guarantee that retrieved schedules are complete
or that the model interprets them correctly; unsupported future dates must be qualified.

## Why loaded memory exceeded the quantized weight size

Quantization does not cover all process memory. The measured language-model parameter
arrays alone occupy **1002.4 MiB** at 8-bit. Attention tensors, temporary Metal buffers,
the separate Mimi audio codec and the Python/native runtimes add to that. MLX previously
retained roughly **920–950 MiB** of reusable buffers after loading/decoding.

The app now:

- Limits reusable Metal allocator cache to **128 MiB**, configurable with
  `VNR_ASR_CACHE_LIMIT_MB`. It does not cap active weights or shorten attention context.
- Clears load/quantization temporaries before reporting ready.
- Drops generator and model-owned attention state after finalization/cancellation,
  clears allocator cache, and retains the model weights for the next recording.
- Resets both model attention caches at the next utterance. Merely replacing `LmGen`
  did not reset these caches, which are owned by `Lm`.
- Serializes reset, decoding and session cleanup on the inference worker and rejects
  late transcript callbacks from a cancelled utterance.
- Preserves the five-minute idle weight unload and existing quantization setting.

### Short recording comparison

Same local `audio/test.wav` (about 16.35 seconds), two sessions per configuration:

| Measurement | Previous allocator policy | 128 MiB cache limit |
| --- | ---: | ---: |
| Cache after first decode | 919.9 MiB | 92.1 MiB |
| Active MLX after first decode | 1036.4 MiB | 1036.4 MiB |
| First decode real-time factor | 0.870 | 0.724 |
| Second decode real-time factor | 0.718 | 0.729 |

Corresponding transcripts were identical across cache configurations. This is a small
performance comparison, not a broad accuracy evaluation or evidence of a speedup;
first-run warmup and system load affect timing. Neither weights nor quantization changed.
The finished-utterance cleanup returns active MLX memory to **1003.9 MiB** and cache to
zero while keeping weights ready.

### Five minutes of audio

`uv run python scripts/benchmark_asr_memory.py --file audio/test.wav --seconds 300`
replayed 300 seconds of local WAV audio in **217.57 seconds (RTF 0.725)**. This tests
continuous inference, not a live microphone capture.

| Stage | Active MLX | Cached MLX |
| --- | ---: | ---: |
| Loaded and ready | 1003.9 MiB | 0 MiB |
| 60 seconds of audio | 1100.4 MiB | 127.1 MiB |
| 120 seconds | 1196.4 MiB | 126.1 MiB |
| 180 seconds | 1292.4 MiB | 126.1 MiB |
| 240 seconds | 1388.4 MiB | 119.0 MiB |
| 300 seconds | 1484.4 MiB | 127.6 MiB |
| Finished, weights still warm | 1003.9 MiB | 0 MiB |
| Fully unloaded | <0.001 MiB | 0 MiB |

Attention memory grows during a long utterance and is released at its end; the allocator
cache remains capped. Historical peak was 1813.4 MiB and does not decrease on release.
The script also reports current RSS, but RSS and Activity Monitor's Memory column are
not interchangeable with MLX allocations on unified memory; do not sum them or claim
that the whole app now occupies only the parameter-array size.

References: [MLX cache limit API](https://ml-explore.github.io/mlx/build/html/python/_autosummary/mlx.core.set_cache_limit.html),
[NVIDIA's Nemotron reasoning format](https://github.com/NVIDIA-NeMo/Megatron-Bridge/blob/main/examples/models/nemotron/nemotron_3/lightning/README.md).
The installed `moshi_mlx` implementation was also inspected for model-owned KV caches.

## Validation

318 Python tests passed (5 live-integration tests skipped), Ruff clean, macOS build
passed, and 230 Swift executable checks passed. Live research, local WAV comparison,
five minutes of inference, and full unload/reload were checked separately on hardware.
The existing Command Line Tools XCTest-path warning and existing Swift 6 migration
warnings are unrelated to these changes.
