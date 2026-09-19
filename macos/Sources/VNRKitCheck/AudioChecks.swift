import Foundation
import VNRKit

/// Everything about the audio contract that does not need a microphone.
///
/// The numbers here are asserted against the Python side's constants on purpose: if the
/// two disagree about what a frame is, the service silently mis-frames every utterance.
func runAudioChecks() {
    Check.section("Audio format")
    Check.equal(AudioFormat.sampleRate, 24_000, "sample rate matches the model")
    Check.equal(AudioFormat.channels, 1, "mono")
    Check.equal(AudioFormat.frameMilliseconds, 80, "80 ms frames")
    Check.equal(AudioFormat.samplesPerFrame, 1920, "1920 samples — one model step")
    Check.equal(AudioFormat.bytesPerFrame, 3840, "3840 bytes on the wire")

    Check.section("Float to int16")
    Check.equal(int16Samples(from: [0.0]), [0], "silence")
    Check.equal(int16Samples(from: [1.0]), [32767], "full scale positive")
    Check.equal(int16Samples(from: [-1.0]), [-32767], "full scale negative")
    // Clamping: without it, an over-range sample wraps and a loud syllable becomes a click.
    Check.equal(int16Samples(from: [1.5]), [32767], "above full scale clamps, not wraps")
    Check.equal(int16Samples(from: [-1.5]), [-32767], "below full scale clamps, not wraps")

    Check.section("Wire encoding")
    let encoded = littleEndianBytes([1, -1, 256])
    Check.equal(Array(encoded), [0x01, 0x00, 0xFF, 0xFF, 0x00, 0x01], "little-endian bytes")
    Check.equal(littleEndianBytes([0, 0]).count, 4, "two bytes per sample")

    Check.section("Peak amplitude")
    Check.equal(peakAmplitude([0, 0, 0]), 0.0, "digital silence reads exactly zero")
    Check.that(peakAmplitude([0, 0, 0]) < silenceThreshold, "silence is below the threshold")
    Check.equal(peakAmplitude([16384]), 0.5, "half scale")
    Check.equal(peakAmplitude([-32768]), 1.0, "negative full scale")
    Check.equal(peakAmplitude([]), 0.0, "an empty buffer is silence, not a crash")

    Check.section("Framing")
    var framer = PCMFramer()
    // A tap delivers whatever the hardware likes; frames must come out at 1920 regardless.
    Check.equal(framer.push(Array(repeating: 1, count: 1000)).count, 0, "a partial frame waits")
    Check.equal(framer.pendingSampleCount, 1000, "the remainder is held, not padded")

    let frames = framer.push(Array(repeating: 1, count: 1000))
    Check.equal(frames.count, 1, "the frame completes once enough samples arrive")
    Check.equal(frames.first?.count, 3840, "a frame is exactly 3840 bytes")
    Check.equal(framer.pendingSampleCount, 80, "the leftover carries forward")

    var bulk = PCMFramer()
    let produced = bulk.push(Array(repeating: 0, count: 1920 * 3 + 5))
    Check.equal(produced.count, 3, "a large buffer yields every whole frame")
    Check.equal(bulk.pendingSampleCount, 5, "and holds the remainder")
    Check.equal(bulk.framesProduced, 3, "frames produced is counted")
    Check.equal(bulk.drainRemainder().count, 5, "the remainder can be drained for a file")
    Check.equal(bulk.pendingSampleCount, 0, "draining empties the buffer")

    Check.section("WAV output")
    let wav = wavFile(from: [0, 1, -1])
    Check.equal(wav.count, 44 + 6, "44-byte header plus the samples")
    Check.equal(String(decoding: wav.prefix(4), as: UTF8.self), "RIFF", "RIFF magic")
    Check.equal(String(decoding: wav[8..<12], as: UTF8.self), "WAVE", "WAVE magic")
    Check.equal(String(decoding: wav[36..<40], as: UTF8.self), "data", "data chunk")
    // 24 kHz mono s16 is what `vnr-asr-spike --file` requires; anything else it rejects.
    Check.equal(Array(wav[24..<28]), [0xC0, 0x5D, 0x00, 0x00], "24000 Hz in the header")
    Check.equal(Array(wav[22..<24]), [0x01, 0x00], "one channel")
    Check.equal(Array(wav[34..<36]), [0x10, 0x00], "16 bits per sample")
}
