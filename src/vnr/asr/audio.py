"""Audio sources for the ASR path (docs/PLAN.md §6).

Two sources, one interface: the live microphone, and a WAV file for repeatable
measurement and for the fixed command set the test plan asks for (PLAN §24).

Both deliver frames in exactly the format the runtime expects — no resampling stage is
introduced. A file at the wrong sample rate is an error you fix with ``ffmpeg``, not
something this layer papers over.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import wave
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import AsrConfig
from ..errors import MicrophoneError, VnrError


class AudioError(VnrError):
    code = "audio"
    user_message = "Audio input unavailable."


#: Below this, the input is silence rather than quiet speech. Digital silence from a
#: muted or unpermitted device is exactly 0.0; a live microphone always has a noise floor.
SILENCE_THRESHOLD = 1e-4


def peak_amplitude(frame: bytes, wire_format: str) -> float:
    """Largest absolute sample in *frame*, normalised to 0.0–1.0.

    macOS hands an app that lacks microphone permission a stream of zeros rather than an
    error, so silence is the only evidence that the capture path is dead.
    """
    if not frame:
        return 0.0
    if wire_format == "f32le":
        samples = array.array("f")
        samples.frombytes(frame[: len(frame) - len(frame) % 4])
        return min(1.0, max((abs(v) for v in samples), default=0.0))
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) - len(frame) % 2])
    return max((abs(v) for v in samples), default=0) / 32768.0


def _to_wire(pcm16: bytes, stdin_format: str) -> bytes:
    if stdin_format == "s16le":
        return pcm16
    samples = array.array("h")
    samples.frombytes(pcm16)
    return array.array("f", (s / 32768.0 for s in samples)).tobytes()


def wire_to_pcm16(frame: bytes, wire_format: str) -> bytes:
    """The inverse of :func:`_to_wire` — back to the 16-bit PCM a WAV file holds.

    Used by ``--dump-wav`` so the file is exactly what the engine was fed, one decode
    step at a time. Anything reconstructed from the *source* instead would be a second
    opinion, and the whole point of the dump is that it is not.
    """
    if wire_format != "f32le":
        return frame
    floats = array.array("f")
    floats.frombytes(frame[: len(frame) - len(frame) % 4])
    return array.array(
        "h", (int(max(-1.0, min(1.0, v)) * 32767.0) for v in floats)
    ).tobytes()


def apply_gain(frame: bytes, wire_format: str, gain: float) -> bytes:
    """Scale *frame* by *gain*, clamping rather than wrapping.

    Clamping is the whole safety story: an Int16 multiplied past full scale wraps to a
    large negative value, so a gain that is slightly too high would not merely distort a
    loud syllable — it would invert it, turning the fix into a worse fault than the one
    it was applied for.

    A gain of exactly 1.0 returns the frame unchanged, so the default path allocates
    nothing and the bytes the model sees are provably the bytes that arrived.
    """
    if gain == 1.0 or not frame:
        return frame
    if wire_format == "f32le":
        samples = array.array("f")
        samples.frombytes(frame[: len(frame) - len(frame) % 4])
        return array.array("f", (max(-1.0, min(1.0, v * gain)) for v in samples)).tobytes()
    pcm = array.array("h")
    pcm.frombytes(frame[: len(frame) - len(frame) % 2])
    return array.array(
        "h", (max(-32768, min(32767, int(v * gain))) for v in pcm)
    ).tobytes()


def write_wav(path: Path | str, pcm16: bytes, *, sample_rate: int, channels: int = 1) -> None:
    """Write mono 16-bit PCM as a WAV file."""
    with contextlib.closing(wave.open(str(path), "wb")) as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16)


def _downmix(pcm16: bytes, channels: int) -> bytes:
    """Average interleaved channels down to mono (stdlib only: audioop is gone in 3.13)."""
    if channels == 1:
        return pcm16
    samples = array.array("h")
    samples.frombytes(pcm16)
    mono = array.array(
        "h",
        (
            sum(samples[i : i + channels]) // channels
            for i in range(0, len(samples) - channels + 1, channels)
        ),
    )
    return mono.tobytes()


class MicrophoneSource:
    """Live capture. Raw frames only — this audio never leaves the machine."""

    def __init__(self, config: AsrConfig, *, device: int | str | None = None) -> None:
        self.config = config
        self.device = device
        self.seconds_captured = 0.0

    async def frames(self) -> AsyncIterator[bytes]:
        try:
            import sounddevice
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise MicrophoneError(
                "sounddevice is not installed. Run: uv pip install -e '.[asr]'"
            ) from exc

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)

        def callback(indata, _frames, _time, status) -> None:  # pragma: no cover - audio thread
            if status:
                pass  # overflows are logged by the caller via the metrics summary
            loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

        try:
            stream = sounddevice.RawInputStream(
                samplerate=self.config.sample_rate,
                blocksize=self.config.frame_samples,
                channels=1,
                dtype="int16",
                device=self.device,
                callback=callback,
            )
        except Exception as exc:  # pragma: no cover - hardware dependent
            raise MicrophoneError(f"Could not open the microphone: {exc}") from exc

        seconds_per_frame = self.config.frame_ms / 1000.0
        with stream:
            while True:
                frame = await queue.get()
                if frame is None:
                    return
                self.seconds_captured += seconds_per_frame
                yield _to_wire(frame, self.config.stdin_format)


class WavFileSource:
    """Replays a WAV file, either as fast as the runtime accepts it or in real time."""

    def __init__(self, path: Path | str, config: AsrConfig, *, realtime: bool = False) -> None:
        self.path = Path(path)
        self.config = config
        self.realtime = realtime
        self.seconds_captured = 0.0

    async def frames(self) -> AsyncIterator[bytes]:
        if not self.path.exists():
            raise AudioError(f"Audio file not found: {self.path}")
        with contextlib.closing(wave.open(str(self.path), "rb")) as wav:
            if wav.getsampwidth() != 2:
                raise AudioError(
                    f"{self.path.name} is {wav.getsampwidth() * 8}-bit; 16-bit PCM is required."
                )
            if wav.getframerate() != self.config.sample_rate:
                raise AudioError(
                    f"{self.path.name} is {wav.getframerate()} Hz but the runtime expects "
                    f"{self.config.sample_rate} Hz. Convert it first:\n"
                    f"  ffmpeg -i {self.path.name} -ac 1 -ar {self.config.sample_rate} out.wav"
                )
            channels = wav.getnchannels()
            seconds_per_frame = self.config.frame_ms / 1000.0
            while True:
                chunk = wav.readframes(self.config.frame_samples)
                if not chunk:
                    return
                frames_read = len(chunk) // (2 * channels)
                self.seconds_captured += frames_read / self.config.sample_rate
                chunk = _downmix(chunk, channels)
                yield _to_wire(chunk, self.config.stdin_format)
                if self.realtime:
                    await asyncio.sleep(seconds_per_frame)


class SilenceSource:
    """Generates silent frames. Only useful for exercising the harness itself."""

    def __init__(self, config: AsrConfig, *, seconds: float = 3.0) -> None:
        self.config = config
        self.seconds = seconds
        self.seconds_captured = 0.0

    async def frames(self) -> AsyncIterator[bytes]:
        frame = bytes(self.config.frame_bytes)
        seconds_per_frame = self.config.frame_ms / 1000.0
        emitted = 0.0
        while emitted < self.seconds:
            emitted += seconds_per_frame
            self.seconds_captured = emitted
            yield frame
            await asyncio.sleep(0)
