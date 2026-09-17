"""A stand-in for a streaming STT binary: reads raw PCM on stdin, emits text on stdout.

Used to exercise the subprocess adapter without model weights. It deliberately behaves
like a real CLI transcriber: it writes partial words with no trailing newline, redraws
the line with \\r, and can print a readiness banner first.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

WORDS = ["find", "recent", "work", "on", "streaming", "ASR", "for", "Darija"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready-marker")
    parser.add_argument("--bytes-per-word", type=int, default=1920)
    parser.add_argument("--mode", choices=["append", "redraw", "json"], default="append")
    parser.add_argument("--exit-immediately", action="store_true")
    parser.add_argument("--fail", action="store_true")
    parser.add_argument("--load-delay", type=float, default=0.0)
    args = parser.parse_args()

    if args.fail:
        print("error: unrecognised option '-i'", file=sys.stderr, flush=True)
        return 2
    if args.exit_immediately:
        return 0
    if args.load_delay:
        time.sleep(args.load_delay)
    if args.ready_marker:
        sys.stdout.write(args.ready_marker + "\n")
        sys.stdout.flush()

    consumed, emitted = 0, 0
    line: list[str] = []
    while True:
        chunk = sys.stdin.buffer.read(1024)
        if not chunk:
            break
        consumed += len(chunk)
        # One word per `bytes_per_word` of audio, cycling forever so a long-lived process
        # keeps producing across several utterances.
        while consumed >= (emitted + 1) * args.bytes_per_word:
            word = WORDS[emitted % len(WORDS)]
            emitted += 1
            line = [word] if len(line) >= len(WORDS) else [*line, word]
            if args.mode == "json":
                sys.stdout.write(json.dumps({"text": " ".join(line)}) + "\n")
            elif args.mode == "redraw":
                sys.stdout.write("\r" + " ".join(line))
            else:
                sys.stdout.write(word + " ")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
