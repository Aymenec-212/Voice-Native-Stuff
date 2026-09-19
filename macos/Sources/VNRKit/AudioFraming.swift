import Foundation

/// The audio contract with the local service (docs/PLAN.md §6, §19).
///
/// The model steps on fixed frames of 24 kHz mono audio, and the service expects those
/// frames as little-endian 16-bit PCM over the WebSocket. Every rule here mirrors the
/// Python side — `AsrConfig.frame_samples`, `frame_bytes` and `_to_wire` — so the two
/// cannot disagree about what a frame is.
///
/// This is deliberately pure Foundation: no AVFoundation, so it is checkable by
/// `swift run VNRKitCheck` on any machine, including Linux CI. Only the microphone
/// plumbing is macOS-only and has to be verified by running it.
public enum AudioFormat {
    /// Kyutai STT is a 24 kHz model. Resampling anywhere else would be a second opinion
    /// about the sample rate, so the capture side converts once and the service never does.
    public static let sampleRate: Double = 24_000
    public static let channels = 1
    /// 80 ms — 1920 samples, which is exactly one model step.
    public static let frameMilliseconds = 80
    public static let samplesPerFrame = Int(sampleRate) * frameMilliseconds / 1000
    public static let bytesPerFrame = samplesPerFrame * 2
}

/// Converts float samples to the 16-bit PCM the wire carries, clamping out of range.
///
/// Clamping matters: a sample above 1.0 wraps to a large negative value if it is simply
/// multiplied and truncated, which turns a loud syllable into a click.
public func int16Samples(from floats: [Float]) -> [Int16] {
    floats.map { sample in
        let clamped = min(max(sample, -1.0), 1.0)
        return Int16(clamped * 32767.0)
    }
}

/// Little-endian bytes, which is what `s16le` means on the Python side.
public func littleEndianBytes(_ samples: [Int16]) -> Data {
    var data = Data(capacity: samples.count * 2)
    for sample in samples {
        let bits = UInt16(bitPattern: sample)
        data.append(UInt8(bits & 0xFF))
        data.append(UInt8(bits >> 8))
    }
    return data
}

/// Largest absolute sample, 0.0–1.0.
///
/// The check that catches a microphone delivering digital silence — an unpermitted app or
/// capture routed to a headset. macOS reports neither as an error, so amplitude is the
/// only evidence. Mirrors `peak_amplitude` in `src/vnr/asr/audio.py`.
public func peakAmplitude(_ samples: [Int16]) -> Double {
    guard let loudest = samples.map({ abs(Int($0)) }).max() else { return 0 }
    return Double(loudest) / 32768.0
}

/// Below this the input is silence rather than quiet speech.
public let silenceThreshold = 1e-4

/// Below this the input is audible but too quiet to trust.
///
/// A distinct threshold from `silenceThreshold`, because the two describe different
/// faults. Digital silence means the capture never reached the microphone at all —
/// a missing TCC grant, or a device that is not the one recording. A peak above that
/// but under this one means real audio at a level where the model's accuracy is a
/// coin toss: a far-field mic, an input gain near zero, or speech from across a room.
/// The second case must not be reported as a pass, so a run that ends below this is
/// a warning rather than a success. Mirrors `QUIET_PEAK` in `src/vnr/audio_analysis.py`.
public let quietPeak = 0.05

/// Accumulates samples and hands out whole frames.
///
/// An audio tap delivers whatever buffer size the hardware likes, which will not be 1920
/// samples. Partial frames are held until they are complete rather than padded, because
/// padding inserts silence into the middle of speech.
public struct PCMFramer {
    private var pending: [Int16] = []
    public private(set) var framesProduced = 0

    public init() {}

    /// Appends *samples* and returns every whole frame that is now available.
    public mutating func push(_ samples: [Int16]) -> [Data] {
        pending.append(contentsOf: samples)
        var frames: [Data] = []
        while pending.count >= AudioFormat.samplesPerFrame {
            let frame = Array(pending.prefix(AudioFormat.samplesPerFrame))
            pending.removeFirst(AudioFormat.samplesPerFrame)
            frames.append(littleEndianBytes(frame))
            framesProduced += 1
        }
        return frames
    }

    /// Samples held back because they do not fill a frame.
    public var pendingSampleCount: Int { pending.count }

    /// Everything captured so far, whole frames and remainder alike — for writing a file.
    public mutating func drainRemainder() -> [Int16] {
        defer { pending.removeAll() }
        return pending
    }
}

/// Minimal 16-bit PCM WAV writer.
///
/// Exists so a Swift capture can be handed straight to `uv run vnr-asr-spike --file`,
/// which is the cheapest way to prove the capture path produces audio the model actually
/// accepts — rather than audio that merely looks right.
public func wavFile(from samples: [Int16], sampleRate: Int = Int(AudioFormat.sampleRate)) -> Data {
    let payload = littleEndianBytes(samples)
    var data = Data()

    func append(_ text: String) { data.append(contentsOf: Array(text.utf8)) }
    func append32(_ value: UInt32) {
        for shift in stride(from: 0, to: 32, by: 8) {
            data.append(UInt8((value >> UInt32(shift)) & 0xFF))
        }
    }
    func append16(_ value: UInt16) {
        data.append(UInt8(value & 0xFF))
        data.append(UInt8(value >> 8))
    }

    append("RIFF")
    append32(UInt32(36 + payload.count))
    append("WAVE")
    append("fmt ")
    append32(16)                                   // PCM header size
    append16(1)                                    // format: PCM
    append16(UInt16(AudioFormat.channels))
    append32(UInt32(sampleRate))
    append32(UInt32(sampleRate * AudioFormat.channels * 2))  // byte rate
    append16(UInt16(AudioFormat.channels * 2))     // block align
    append16(16)                                   // bits per sample
    append("data")
    append32(UInt32(payload.count))
    data.append(payload)
    return data
}
