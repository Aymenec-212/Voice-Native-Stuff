#if os(macOS)
import AVFoundation
import Foundation
import VNRKit

/// Microphone capture, framed exactly as the socket wants it.
///
/// The same path `VNRCapture` proved in slice 2, with the WAV writer replaced by a
/// callback: 24 kHz mono, whole 1920-sample frames through `PCMFramer`, partials held
/// back rather than padded. Voice processing is disabled **before** the input format is
/// read, because toggling it changes that format and a converter built from the earlier
/// one is converting from a description that no longer applies.
public final class AudioCapture {
    public enum Failure: Error {
        case noMicrophone
        case cannotConvert
        case engineFailed(String)
    }

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var framer = PCMFramer()
    private let lock = NSLock()
    private var onFrame: ((Data) -> Void)?

    public private(set) var deviceSampleRate: Double = 0
    /// Largest sample seen, so a dead microphone is visible rather than merely quiet.
    public private(set) var peak: Double = 0
    /// Converter buffers that could not be used. Gaps the ear misses; the model does not.
    public private(set) var dropped = 0

    public init() {}

    public var isRunning: Bool { engine.isRunning }

    /// Starts capture, calling *onFrame* with one 3840-byte frame at a time.
    public func start(onFrame: @escaping (Data) -> Void) throws {
        let input = engine.inputNode

        // Before the format is read — see the note above.
        try? input.setVoiceProcessingEnabled(false)

        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0 else { throw Failure.noMicrophone }
        deviceSampleRate = inputFormat.sampleRate

        guard
            let target = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: AudioFormat.sampleRate,
                channels: AVAudioChannelCount(AudioFormat.channels),
                interleaved: false
            ),
            let converter = AVAudioConverter(from: inputFormat, to: target)
        else { throw Failure.cannotConvert }
        self.converter = converter

        lock.lock()
        framer = PCMFramer()
        peak = 0
        dropped = 0
        self.onFrame = onFrame
        lock.unlock()

        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, _ in
            self?.handle(buffer, converter: converter, target: target, from: inputFormat)
        }

        do {
            engine.prepare()
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw Failure.engineFailed(error.localizedDescription)
        }
    }

    public func stop() {
        guard engine.isRunning else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        lock.lock()
        onFrame = nil
        lock.unlock()
    }

    private func handle(
        _ buffer: AVAudioPCMBuffer,
        converter: AVAudioConverter,
        target: AVAudioFormat,
        from inputFormat: AVAudioFormat
    ) {
        let ratio = AudioFormat.sampleRate / inputFormat.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let converted = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else {
            note(dropped: true)
            return
        }

        var conversionError: NSError?
        var alreadySupplied = false
        // One tap buffer per conversion: supplying it twice would duplicate audio rather
        // than end the conversion.
        let status = converter.convert(to: converted, error: &conversionError) { _, inputStatus in
            if alreadySupplied {
                inputStatus.pointee = .noDataNow
                return nil
            }
            alreadySupplied = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard
            status != .error, conversionError == nil, converted.frameLength > 0,
            let channel = converted.floatChannelData
        else {
            note(dropped: true)
            return
        }

        let floats = Array(
            UnsafeBufferPointer(start: channel[0], count: Int(converted.frameLength))
        )
        let samples = int16Samples(from: floats)

        lock.lock()
        peak = max(peak, peakAmplitude(samples))
        let frames = framer.push(samples)
        let sink = onFrame
        lock.unlock()

        for frame in frames { sink?(frame) }
    }

    private func note(dropped isDropped: Bool) {
        guard isDropped else { return }
        lock.lock()
        dropped += 1
        lock.unlock()
    }
}
#endif
