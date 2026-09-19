#if os(macOS)
import AVFoundation
import AppKit
import Foundation
import VNRKit

// Milestone 4, slice 2: capture microphone audio in the shape the service wants.
//
// Writes TWO files, deliberately:
//
//   <output>                  24 kHz mono, after our conversion — what the service gets
//   <output>-raw-<rate>.wav   the device's own rate, before any conversion
//
// The second one exists because a capture that sounds perfect and is structurally
// perfect can still transcribe to nothing, and from outside the process there is no way
// to tell whether the fault is in the capture or in the resampling. With both files the
// question is decidable in one step:
//
//     ffmpeg -i capture-raw-44100.wav -ar 24000 -ac 1 -sample_fmt s16 reference.wav
//     uv run vnr-asr-spike --file reference.wav     # known-good resampler
//     uv run vnr-asr-spike --file capture.wav       # ours
//     uv run vnr-audio-diff reference.wav capture.wav
//
// If the reference transcribes and ours does not, the resampling here is the bug. If
// neither does, the bug is upstream of it, in the capture itself.

let arguments = CommandLine.arguments.dropFirst()
let outputPath = arguments.first
    ?? FileManager.default.temporaryDirectory.appendingPathComponent("vnr-capture.wav").path
let seconds = Double(arguments.dropFirst().first ?? "") ?? 5.0

let logURL = FileManager.default.temporaryDirectory.appendingPathComponent("vnr-capture.log")
var transcript: [String] = []
var warnings: [String] = []

func say(_ line: String) {
    print(line)
    transcript.append(line)
    try? transcript.joined(separator: "\n").appending("\n")
        .write(to: logURL, atomically: true, encoding: .utf8)
}

func warn(_ line: String) {
    warnings.append(line)
    say("WARNING: \(line)")
}

/// An audible cue. Launched with `open`, stdout goes to a log nobody can watch, so a
/// printed "speak now" is invisible until the recording is already over.
func cue(_ name: String) {
    NSSound(named: NSSound.Name(name))?.play()
}

NSApplication.shared.setActivationPolicy(.accessory)

say("Voice-Native Research — microphone capture")
say("  output: \(outputPath)")
say("  log:    \(logURL.path)")
say("  length: \(seconds)s")

// --- permission ---------------------------------------------------------------------
// Request rather than refuse. Refusing meant that after `tccutil reset` nothing in the
// capture path ever asked again, so the grant could not come back.
switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized:
    break
case .notDetermined:
    let usage = Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription")
    guard let usage = usage as? String, !usage.isEmpty else {
        say("FAIL: NSMicrophoneUsageDescription is missing; requesting access would kill")
        say("  the process. Run through the bundle: ./scripts/make-app.sh run")
        exit(2)
    }
    say("")
    say("Requesting microphone access — answer the dialog…")
    let answered = DispatchSemaphore(value: 0)
    var granted = false
    AVCaptureDevice.requestAccess(for: .audio) { allowed in
        granted = allowed
        answered.signal()
    }
    if answered.wait(timeout: .now() + 120) == .timedOut {
        say("FAIL: no answer within 120s.")
        exit(2)
    }
    guard granted else {
        say("FAIL: access denied. Allow it in System Settings → Privacy & Security →")
        say("  Microphone, or reset with ./scripts/make-app.sh reset")
        exit(2)
    }
    say("Granted.")
case .denied, .restricted:
    say("")
    say("FAIL: microphone access is denied or restricted. Allow it in System Settings →")
    say("  Privacy & Security → Microphone, or reset with ./scripts/make-app.sh reset")
    exit(2)
@unknown default:
    say("FAIL: unknown authorization status.")
    exit(2)
}

// --- capture ------------------------------------------------------------------------
final class Capture {
    private let lock = NSLock()
    private var framer = PCMFramer()
    private var converted: [Int16] = []
    private var raw: [Int16] = []
    private var peak: Double = 0
    private var rawPeak: Double = 0
    private(set) var callbacks = 0
    private(set) var dropped = 0

    func append(converted samples: [Int16], raw rawSamples: [Int16]) {
        lock.lock()
        defer { lock.unlock() }
        callbacks += 1
        _ = framer.push(samples)          // frame accounting, as the WebSocket will do it
        converted.append(contentsOf: samples)
        raw.append(contentsOf: rawSamples)
        peak = max(peak, peakAmplitude(samples))
        rawPeak = max(rawPeak, peakAmplitude(rawSamples))
    }

    func drop() {
        lock.lock()
        defer { lock.unlock() }
        callbacks += 1
        dropped += 1
    }

    var snapshot: (
        converted: [Int16], raw: [Int16], frames: Int, pending: Int, peak: Double, rawPeak: Double
    ) {
        lock.lock()
        defer { lock.unlock() }
        return (converted, raw, framer.framesProduced, framer.pendingSampleCount, peak, rawPeak)
    }
}

let capture = Capture()
let engine = AVAudioEngine()
let input = engine.inputNode
let inputFormat = input.outputFormat(forBus: 0)

guard inputFormat.sampleRate > 0 else {
    say("")
    say("FAIL: the input device reports a sample rate of 0 — there is no usable microphone.")
    say("  Check System Settings → Sound → Input.")
    exit(3)
}
say("  device: \(inputFormat.sampleRate) Hz, \(inputFormat.channelCount) ch")

if inputFormat.sampleRate < AudioFormat.sampleRate {
    warn(
        "the device runs at \(Int(inputFormat.sampleRate)) Hz, below the model's "
        + "\(Int(AudioFormat.sampleRate)) Hz. Upsampling invents no detail, so recognition "
        + "will be worse than it should be. A Bluetooth headset in call mode does this; "
        + "pick the built-in microphone in System Settings → Sound → Input."
    )
}

guard
    let targetFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: AudioFormat.sampleRate,
        channels: AVAudioChannelCount(AudioFormat.channels),
        interleaved: false
    ),
    let converter = AVAudioConverter(from: inputFormat, to: targetFormat)
else {
    say("FAIL: cannot convert the input format to 24 kHz mono.")
    exit(3)
}

/// Read a tap buffer as mono floats, whatever layout the device uses.
func monoFloats(_ buffer: AVAudioPCMBuffer) -> [Float] {
    let frames = Int(buffer.frameLength)
    guard frames > 0, let channels = buffer.floatChannelData else { return [] }
    let count = Int(buffer.format.channelCount)
    if count == 1 {
        return Array(UnsafeBufferPointer(start: channels[0], count: frames))
    }
    var mixed = [Float](repeating: 0, count: frames)
    for channel in 0..<count {
        let data = UnsafeBufferPointer(start: channels[channel], count: frames)
        for index in 0..<frames { mixed[index] += data[index] }
    }
    return mixed.map { $0 / Float(count) }
}

func startRecording() {
    input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { buffer, _ in
        let ratio = AudioFormat.sampleRate / inputFormat.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let converted = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity)
        else {
            capture.drop()
            return
        }

        var conversionError: NSError?
        var alreadySupplied = false
        // One tap buffer per conversion: supply it once, then report the input as dry.
        // Saying `.haveData` a second time would hand the converter the same buffer
        // again, which duplicates audio rather than ending the conversion.
        let status = converter.convert(to: converted, error: &conversionError) { _, inputStatus in
            if alreadySupplied {
                inputStatus.pointee = .noDataNow
                return nil
            }
            alreadySupplied = true
            inputStatus.pointee = .haveData
            return buffer
        }

        // A dropped buffer sounds like nothing much and destroys a model's input, so it
        // is counted rather than silently returned on.
        guard status != .error, conversionError == nil, converted.frameLength > 0 else {
            capture.drop()
            return
        }

        guard let channel = converted.floatChannelData else {
            capture.drop()
            return
        }
        let floats = Array(UnsafeBufferPointer(start: channel[0], count: Int(converted.frameLength)))
        capture.append(converted: int16Samples(from: floats), raw: int16Samples(from: monoFloats(buffer)))
    }
}

do {
    engine.prepare()
    try engine.start()
} catch {
    say("FAIL: could not start the audio engine: \(error.localizedDescription)")
    exit(3)
}

say("")
say("Starting in 1s — a chime means speak.")
cue("Tink")
RunLoop.current.run(until: Date().addingTimeInterval(1.0))   // let the chime finish first

startRecording()
say("Recording — speak now…")
RunLoop.current.run(until: Date().addingTimeInterval(seconds))

input.removeTap(onBus: 0)
engine.stop()
cue("Pop")

// --- report ---------------------------------------------------------------------------
let result = capture.snapshot
let captured = Double(result.converted.count) / AudioFormat.sampleRate

// Both durations, because a conversion that loses or duplicates audio shows up here
// before it shows up anywhere else. They should agree to within a frame; a ratio that is
// not ~1.000 means the audio plays at the wrong speed, which no model can read.
let rawSeconds = Double(result.raw.count) / inputFormat.sampleRate
let durationRatio: Double = rawSeconds > 0 ? captured / rawSeconds : 0

let convertedPeakText = String(format: "%.4f", result.peak)
let rawPeakText = String(format: "%.4f", result.rawPeak)
let rawSecondsText = String(format: "%.3f", rawSeconds)
let capturedText = String(format: "%.3f", captured)
let ratioText = String(format: "%.4f", durationRatio)

say("")
say("Captured \(result.converted.count) samples (\(capturedText)s)")
say("  tap callbacks: \(capture.callbacks), dropped: \(capture.dropped)")
say("  whole frames:  \(result.frames) of \(AudioFormat.samplesPerFrame) samples")
say("  held back:     \(result.pending) samples (less than one frame)")
say("  peak level:    \(convertedPeakText) converted, \(rawPeakText) raw")
say("  duration:      \(rawSecondsText)s raw -> \(capturedText)s converted (ratio \(ratioText))")

if rawSeconds > 0 && abs(durationRatio - 1.0) > 0.01 {
    let driftText = String(format: "%.1f", (durationRatio - 1) * 100)
    warn(
        "the converted audio differs in length from the device's by \(driftText)%. "
        + "The conversion is losing or duplicating audio, so everything plays at the "
        + "wrong speed — which no model can read, however clean each sample is."
    )
}

if capture.dropped > 0 {
    warn("\(capture.dropped) buffers were dropped — the recording has gaps the ear may miss")
}

guard !result.converted.isEmpty else {
    say("")
    say("FAIL: nothing was captured. The tap never fired.")
    exit(4)
}

if result.peak < silenceThreshold {
    say("")
    say("FAIL: every sample was zero — the device delivered digital silence.")
    say("  The grant exists, so this is the input device rather than permission: check")
    say("  System Settings → Sound → Input.")
    exit(5)
}
if result.peak < quietPeak {
    warn(
        "peak \(String(format: "%.3f", result.peak)) is very quiet — speech usually reaches "
        + "0.1–0.5. Check the input device and move closer before trusting a poor transcript."
    )
}

// The raw file is the control in the experiment: same audio, none of our conversion.
// Named `<output>-raw-<rate>.wav` so the rate is in the filename — a pre-conversion dump
// whose rate has to be remembered is a dump that gets resampled wrong later.
let outputURL = URL(fileURLWithPath: outputPath)
let rawPath = outputURL
    .deletingLastPathComponent()
    .appendingPathComponent(
        outputURL.deletingPathExtension().lastPathComponent
            + "-raw-\(Int(inputFormat.sampleRate)).wav"
    )
    .path

do {
    try wavFile(from: result.converted).write(to: outputURL)
    say("  wrote: \(outputPath)")
    try wavFile(from: result.raw, sampleRate: Int(inputFormat.sampleRate))
        .write(to: URL(fileURLWithPath: rawPath))
    say("  wrote: \(rawPath)  (pre-conversion, for comparison)")
} catch {
    say("FAIL: could not write output: \(error.localizedDescription)")
    exit(4)
}

say("")
if warnings.isEmpty {
    say("PASS — captured audio at 24 kHz mono.")
} else {
    say("CAPTURED WITH \(warnings.count) WARNING(S) — see above. Not a clean run.")
}

say("")
say("Prove the model accepts it:")
say("  uv run vnr-asr-spike --file \(outputPath)")
say("")
say("If that transcribes nothing, bisect rather than guess. Resample the raw file with a")
say("known-good tool and try that — if the reference works and ours does not, the fault is")
say("in this conversion; if neither works, it is upstream of it:")
say("  ffmpeg -i \(rawPath) -ar 24000 -ac 1 -sample_fmt s16 /tmp/reference.wav")
say("  uv run vnr-asr-spike --file /tmp/reference.wav")
say("  uv run vnr-audio-diff /tmp/reference.wav \(outputPath)")
say("")
say("And compare against the path that is known to work, saying the same words:")
say("  uv run vnr-asr-spike --seconds 8 --dump-wav /tmp/live.wav")
say("  uv run vnr-audio-diff /tmp/live.wav \(outputPath)")

exit(warnings.isEmpty ? 0 : 6)

#else
import Foundation

// Linux CI builds every target; this one needs AVFoundation, so it is a stub there.
print("VNRCapture requires macOS — microphone capture is AVFoundation-only.")
exit(0)
#endif
