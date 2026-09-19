"""`--dump-wav`: the reference file the live path was never able to produce.

Slice 2's capture transcribes to nothing while the live microphone path transcribes
fine, and comparing them by ear settled nothing. This flag makes the comparison a
diff: the live path writes out exactly the frames it handed the model, so
`vnr-audio-diff live.wav captured.wav` is comparing like with like rather than a
capture against an impression of one.
"""

from __future__ import annotations

import asyncio
import struct
import wave
from pathlib import Path

import pytest

from vnr.cli.asr_spike import SpikeRecorder, _stream, main


class RecordingEngine:
    """Collects the frames the spike pushes, so the dump can be checked against them."""

    def __init__(self) -> None:
        self.pushed: list[bytes] = []

    async def push_audio(self, frame: bytes) -> None:
        self.pushed.append(frame)


class ListSource:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames

    async def frames(self):
        for frame in self._frames:
            yield frame


def write_tone(path: Path, *, rate: int = 24_000, seconds: float = 0.4) -> Path:
    """A ramp, not silence: silence would round-trip even through a broken conversion."""
    samples = [((i * 37) % 20_000) - 10_000 for i in range(int(rate * seconds))]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


async def test_the_dump_holds_exactly_what_the_engine_was_pushed():
    frames = [struct.pack("<2h", i, -i) for i in range(5)]
    engine, dump = RecordingEngine(), bytearray()

    await _stream(engine, ListSource(frames), SpikeRecorder(), asyncio.Event(), "s16le", dump)

    assert engine.pushed == frames
    assert bytes(dump) == b"".join(frames)


async def test_stopping_truncates_the_dump_with_the_stream():
    stop = asyncio.Event()
    stop.set()
    engine, dump = RecordingEngine(), bytearray()

    await _stream(engine, ListSource([b"\x00\x00"]), SpikeRecorder(), stop, "s16le", dump)

    assert engine.pushed == []
    assert bytes(dump) == b""


async def test_no_dump_is_collected_unless_asked():
    engine = RecordingEngine()
    await _stream(engine, ListSource([b"\x01\x00"]), SpikeRecorder(), asyncio.Event(), "s16le")
    assert engine.pushed == [b"\x01\x00"]


def test_replaying_a_wav_dumps_it_back_byte_for_byte(tmp_path, capsys):
    """The property that makes the dump usable as a reference.

    If the file that comes out is not the file that went in, the dump is an artifact of
    the spike rather than evidence about the audio — and the whole comparison is void.
    """
    source = write_tone(tmp_path / "in.wav")
    dumped = tmp_path / "out.wav"

    code = main(["--engine", "mock", "--file", str(source), "--dump-wav", str(dumped)])

    assert code == 0
    assert dumped.read_bytes() == source.read_bytes()


def test_the_dump_states_its_own_rate_and_length(tmp_path, capsys):
    source = write_tone(tmp_path / "in.wav", seconds=0.8)
    dumped = tmp_path / "out.wav"

    main(["--engine", "mock", "--file", str(source), "--dump-wav", str(dumped)])

    with wave.open(str(dumped), "rb") as wav:
        assert wav.getframerate() == 24_000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == pytest.approx(0.8 * 24_000, abs=1920)
