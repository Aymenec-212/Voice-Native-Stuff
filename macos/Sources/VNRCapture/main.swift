#if os(macOS)
import AVFoundation
import AppKit
import Foundation
import VNRKit

// Milestone 4, slice 2: capture microphone audio in exactly the shape the service wants,
// and write it to a WAV so the Python spike can transcribe it.
//
// That last part is the verification. `swift test` cannot run on a Command Line Tools
// install, so the proof that this capture path is correct is not an assertion — it is
// feeding the output to the model that will consume it in production:
//
//     PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/capture.wav 6
//     uv run vnr-asr-spike --file /tmp/capture.wav
//
// If the spike transcribes it, the format, sample rate, channel count and framing are all
// right. If it rejects the file or returns silence, they are not.

let arguments = CommandLine.arguments.dropFirst()
let outputPath = arguments.first
    ?? FileManager.default.temporaryDirectory.appendingPathComponent("vnr-capture.wav").path
let seconds = Double(arguments.dropFirst().first ?? "") ?? 5.0

let logURL = FileManager.default.temporaryDirectory.appendingPathComponent("vnr-capture.log")
var transcript: [String] = []

func say(_ line: String) {
    print(line)
    transcript.append(line)
    try? transcript.joined(separator: "\n").appending("\n")
        .write(to: logURL, atomically: true, encoding: .utf8)
}

// Same activation policy the menu-bar app will use, so TCC sees this bundle rather than
// whatever launched it.
NSApplication.shared.setActivationPolicy(.accessory)

say("Voice-Native Research — microphone capture")
say("  output: \(outputPath)")
say("  log:    \(logURL.path)")
say("  length: \(seconds)s")

guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
    say("")
    say("FAIL: no microphone grant. Run the probe first:")
    say("  PRODUCT=VNRProbe ./scripts/make-app.sh run")
    exit(2)
}

// Written on the audio render thread, read from the main thread once recording stops.
final class Capture {
    private let lock = NSLock()
    private var framer = PCMFramer()
    private var all: [Int16] = []
    private var peak: Double = 0

    func append(_ samples: [Int16]) {
        lock.lock()
        defer { lock.unlock() }
        _ = framer.push(samples)          // frame accounting, as the WebSocket will do it
        all.append(contentsOf: samples)
        peak = max(peak, peakAmplitude(samples))
    }

    var snapshot: (samples: [Int16], frames: Int, pending: Int, peak: Double) {
        lock.lock()
        defer { lock.unlock() }
        return (all, framer.framesProduced, framer.pendingSampleCount, peak)
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

// One conversion, here. The service never resamples — a second opinion about the sample
// rate is how a pipeline ends up quietly feeding the model the wrong thing.
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

input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { buffer, _ in
    let ratio = AudioFormat.sampleRate / inputFormat.sampleRate
    let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
    guard let converted = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity)
    else { return }

    var conversionError: NSError?
    var alreadySupplied = false
    converter.convert(to: converted, error: &conversionError) { _, status in
        if alreadySupplied {
            status.pointee = .noDataNow
            return nil
        }
        alreadySupplied = true
        status.pointee = .haveData
        return buffer
    }
    guard conversionError == nil, let channel = converted.floatChannelData else { return }

    let floats = Array(UnsafeBufferPointer(start: channel[0], count: Int(converted.frameLength)))
    capture.append(int16Samples(from: floats))
}

do {
    engine.prepare()
    try engine.start()
} catch {
    say("FAIL: could not start the audio engine: \(error.localizedDescription)")
    exit(3)
}

say("")
say("Recording — speak now…")
// RunLoop rather than sleep: it keeps servicing the loop while the tap fills.
RunLoop.current.run(until: Date().addingTimeInterval(seconds))

input.removeTap(onBus: 0)
engine.stop()

let result = capture.snapshot
let captured = Double(result.samples.count) / AudioFormat.sampleRate

say("")
say("Captured \(result.samples.count) samples (\(String(format: "%.2f", captured))s)")
say("  whole frames: \(result.frames) of \(AudioFormat.samplesPerFrame) samples")
say("  held back:    \(result.pending) samples (less than one frame)")
say("  peak level:   \(String(format: "%.4f", result.peak))")

guard !result.samples.isEmpty else {
    say("")
    say("FAIL: nothing was captured. The tap never fired.")
    exit(4)
}

do {
    try wavFile(from: result.samples).write(to: URL(fileURLWithPath: outputPath))
    say("  wrote:        \(outputPath)")
} catch {
    say("FAIL: could not write \(outputPath): \(error.localizedDescription)")
    exit(4)
}

if result.peak < silenceThreshold {
    say("")
    say("FAIL: every sample was zero — the device delivered digital silence.")
    say("  The grant exists, so this is the input device rather than permission:")
    say("  check System Settings → Sound → Input, and that a connected headset has not")
    say("  been selected automatically.")
    exit(5)
}

say("")
say("PASS — captured audio at 24 kHz mono. Now prove the model accepts it:")
say("  uv run vnr-asr-spike --file \(outputPath)")
exit(0)

#else
import Foundation

// Linux CI builds every target; this one needs AVFoundation, so it is a stub there.
print("VNRCapture requires macOS — microphone capture is AVFoundation-only.")
exit(0)
#endif
