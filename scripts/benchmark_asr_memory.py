"""Hardware benchmark: warm weights, bounded allocator cache, repeated sessions.

uv run python scripts/benchmark_asr_memory.py --file audio/test.wav
Add --seconds 300 for five minutes of audio, decoded faster than real time.
No microphone or research API is used; transcripts are hashed for comparison.
"""

import argparse
import hashlib
import json
import time
import wave
from dataclasses import replace

from vnr.asr.mlx_engine import MoshiMlxBackend
from vnr.config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--cache-limit-mb", type=int, default=128)
    args = parser.parse_args()
    config = replace(Settings.load().asr, cache_limit_mb=args.cache_limit_mb)
    with wave.open(args.file, "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (24000, 1, 2):
            raise SystemExit("Expected 24 kHz mono int16 WAV")
        pcm = wav.readframes(wav.getnframes())
    if not pcm:
        raise SystemExit("Empty WAV")
    frames = [
        pcm[i : i + config.frame_bytes].ljust(config.frame_bytes, b"\0")
        for i in range(0, len(pcm), config.frame_bytes)
    ]
    count = round(args.seconds * 1000 / config.frame_ms) if args.seconds else len(frames)
    backend = MoshiMlxBackend(config)

    def report(stage, **extra):
        print(
            json.dumps(
                {
                    "stage": stage,
                    "cache_limit_mb": args.cache_limit_mb,
                    **extra,
                    **backend.memory_snapshot(),
                }
            ),
            flush=True,
        )

    try:
        started = time.monotonic()
        backend.load()
        report("loaded", load_s=time.monotonic() - started)
        loaded_active = backend.memory_snapshot()["mlx_active_mb"]
        for cycle in range(2 if not args.seconds else 1):
            backend.reset()
            started = time.monotonic()
            pieces = []
            for index in range(count):
                pieces.append(backend.step(frames[index % len(frames)]))
                if (index + 1) % 750 == 0:
                    report("decoding", audio_seconds=(index + 1) * 0.08)
            for _ in range(10):
                pieces.append(backend.step(bytes(config.frame_bytes)))
            elapsed = time.monotonic() - started
            report(
                "decoded",
                cycle=cycle,
                decode_s=elapsed,
                rtf=elapsed / (count * 0.08),
                transcript_sha256=hashlib.sha256("".join(pieces).strip().encode()).hexdigest(),
            )
            assert backend.memory_snapshot()["mlx_cache_mb"] <= args.cache_limit_mb + 1
            backend.release_session()
            assert backend._model is not None and backend._gen is None
            assert backend.memory_snapshot()["mlx_active_mb"] <= loaded_active + 1
            assert backend.memory_snapshot()["mlx_cache_mb"] == 0
            report("warm_idle", cycle=cycle)
    finally:
        backend.close()
    report("unloaded")
    assert backend.memory_snapshot()["mlx_active_mb"] < 1
    assert backend.memory_snapshot()["mlx_cache_mb"] == 0


if __name__ == "__main__":
    main()
