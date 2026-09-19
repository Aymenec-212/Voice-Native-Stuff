"""``vnr-audio-diff`` — compare two recordings by measurement rather than by ear.

    uv run vnr-audio-diff good.wav bad.wav

Built for exactly one question: a file sounds correct, is structurally correct, and still
transcribes to nothing. Ears and `wave` headers have already agreed it is fine, so the
answer has to come from numbers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..audio_analysis import AudioStats, analyse, findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vnr-audio-diff",
        description="Measure a WAV, or compare two, for things that stop transcription.",
    )
    parser.add_argument("files", nargs="+", metavar="WAV", help="one file, or two to compare")
    parser.add_argument(
        "--expect-rate", type=int, default=24_000, help="sample rate the model wants"
    )
    return parser


ROWS: list[tuple[str, str]] = [
    ("sample rate", "{s.sample_rate} Hz"),
    ("channels", "{s.channels}"),
    ("duration", "{s.duration_s:.2f}s"),
    ("samples", "{s.sample_count}"),
    ("peak", "{s.peak:.4f}"),
    ("rms", "{s.rms:.4f}"),
    ("dBFS", "{s.dbfs:.1f}"),
    ("DC offset", "{s.dc_offset:+.5f}"),
    ("clipped", "{s.clipped_samples}"),
    ("zero-crossing rate", "{s.zero_crossing_rate:.4f}"),
    ("energy above 6 kHz", "{s.hf_energy_ratio:.1%}"),
]


def periodicity(stats: AudioStats) -> str:
    if stats.discontinuity_period is None:
        return "none detected"
    return (
        f"every {stats.discontinuity_period} samples "
        f"({stats.discontinuity_period_ms:.1f} ms, {stats.discontinuity_share:.0%})"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        measured = [analyse(Path(path)) for path in args.files]
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    names = [Path(s.path).name for s in measured]
    width = max(22, *(len(name) for name in names))

    print(f"{'':22}  " + "  ".join(name.ljust(width) for name in names))
    print("-" * (24 + (width + 2) * len(names)))
    for label, template in [*ROWS, ("periodic jumps", None)]:
        if template is None:
            values = [periodicity(s) for s in measured]
        else:
            values = [template.format(s=s) for s in measured]
        print(f"{label:22}  " + "  ".join(value.ljust(width) for value in values))

    exit_code = 0
    for stats in measured:
        notes = findings(stats, expected_rate=args.expect_rate)
        print(f"\n{Path(stats.path).name}:")
        if not notes:
            print("  nothing here explains a failure to transcribe.")
            continue
        exit_code = 1
        for note in notes:
            print(f"  - {note}")

    if len(measured) == 2:
        first, second = measured
        print("\nDifferences:")
        ratio = (second.duration_s / first.duration_s) if first.duration_s else 0
        print(f"  duration ratio  {ratio:.3f}  (1.000 means the same length of audio)")
        print(f"  rms ratio       {(second.rms / first.rms) if first.rms else 0:.3f}")
        print(
            f"  HF energy       {first.hf_energy_ratio:.1%} → {second.hf_energy_ratio:.1%}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
