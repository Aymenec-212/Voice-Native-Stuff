#if os(macOS)
import AVFoundation
import Foundation
import SwiftUI
import VNRKit

/// Everything the views observe, and the only place that talks to the service.
///
/// The views render `model` and nothing else — the rule slice 3 established. What this
/// adds is the draft: `SessionModel.transcript` is what the ASR said, `draft` is what the
/// user will send, and they are deliberately separate (PLAN §7 keeps both, and their
/// difference is the ASR quality signal).
@MainActor
public final class SessionStore: ObservableObject {
    @Published public private(set) var model = SessionModel()
    @Published public private(set) var gate: ServiceHealth.Gate = .unreachable
    @Published public var draft = TranscriptDraft()
    @Published public private(set) var isRecording = false
    @Published public private(set) var notice: String?
    /// Peak of the microphone, for the level meter. A dead device reads zero here, which
    /// is the failure macOS reports as silence rather than as an error.
    @Published public private(set) var inputPeak: Double = 0

    private let endpoint: ServiceEndpoint
    private let capture = AudioCapture()
    private var client: ServiceClient?
    private var reader: Task<Void, Never>?
    private var healthPoll: Task<Void, Never>?

    public init(endpoint: ServiceEndpoint) {
        self.endpoint = endpoint
    }

    public var canRecord: Bool { model.canRecord(healthGate: gate) && !isRecording }

    // MARK: - Health, which gates the record button

    public func startHealthPolling() {
        healthPoll?.cancel()
        healthPoll = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let next = await fetchHealth(self.endpoint)
                self.gate = next
                // Fast while it is still coming up, slow once it is in: the model takes
                // seconds to load and the answer does not change often afterwards.
                let delay: UInt64 = next.allowsRecording ? 10_000_000_000 : 1_000_000_000
                try? await Task.sleep(nanoseconds: delay)
            }
        }
    }

    // MARK: - Recording

    public func toggleRecording() async {
        if isRecording {
            await stopRecording()
        } else {
            await startRecording()
        }
    }

    public func startRecording() async {
        guard canRecord else {
            notice = gate.allowsRecording ? nil : gate.explanation
            return
        }
        guard await microphoneGranted() else {
            notice = "Microphone access is off. System Settings → Privacy & Security."
            return
        }

        do {
            try await connectIfNeeded()
        } catch {
            notice = "Could not reach the service: \(error)"
            return
        }

        draft.reset()
        notice = nil
        inputPeak = 0

        guard let client else { return }
        do {
            try await client.send(.startRecording)
        } catch {
            notice = "The service refused to start: \(error)"
            return
        }

        do {
            try capture.start { [weak self] frame in
                // The audio thread must not wait on the actor.
                Task { await self?.forward(frame) }
            }
            isRecording = true
        } catch {
            notice = "Microphone unavailable: \(error)"
            try? await client.send(.stopRecording)
        }
    }

    public func stopRecording() async {
        guard isRecording else { return }
        capture.stop()
        isRecording = false
        inputPeak = capture.peak
        if capture.dropped > 0 {
            notice = "\(capture.dropped) audio buffers were dropped — the recording has gaps."
        }
        try? await client?.send(.stopRecording)
    }

    private func forward(_ frame: Data) async {
        guard let client else { return }
        do {
            try await client.send(audioFrame: frame)
        } catch {
            // The socket is gone. Stop capturing rather than filling a queue nobody reads.
            capture.stop()
            isRecording = false
            notice = "The connection dropped while recording."
        }
    }

    // MARK: - Review (PLAN §7)

    public func submit() async {
        switch ReviewDecision.go(draft) {
        case .refused(let reason):
            notice = reason
        case .submit(let command):
            notice = nil
            try? await client?.send(command)
        }
    }

    public func discard() async {
        draft.reset()
        notice = nil
        if case .submit(let command) = ReviewDecision.discard {
            try? await client?.send(command)
        }
    }

    public func cancelResearch() async {
        try? await client?.send(.cancel)
    }

    // MARK: - The socket

    private func connectIfNeeded() async throws {
        if let client, await client.endedBecause == nil { return }
        reader?.cancel()

        let fresh = ServiceClient(channel: URLSessionChannel(endpoint: endpoint))
        client = fresh
        reader = Task { [weak self] in
            do {
                try await fresh.run { [weak self] event, model, changed in
                    Task { @MainActor in
                        self?.absorb(event, model, changed)
                    }
                }
            } catch ServiceClientError.busy {
                await self?.report("Another client is already using the model.")
            } catch {
                await self?.report("Disconnected: \(error.localizedDescription)")
            }
        }
    }

    private func absorb(_ event: ServiceEvent, _ model: SessionModel, _ changed: Bool) {
        // Nothing changed means nothing to redraw — streaming ASR repeats itself about
        // thirty times an utterance, and publishing each one flickers the overlay.
        guard changed else { return }
        self.model = model
        // The draft follows the ASR only until the user types; `follow` enforces that,
        // so a final transcript landing after an edit cannot discard the correction.
        draft.follow(model.transcript)
    }

    @MainActor
    private func report(_ message: String) {
        notice = message
        if isRecording {
            capture.stop()
            isRecording = false
        }
    }

    private func microphoneGranted() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return true
        case .notDetermined:
            // Requesting without NSMicrophoneUsageDescription terminates the process
            // rather than returning an error, so the bundle must carry it — see
            // macos/Resources/Info.plist and scripts/make-app.sh.
            return await AVCaptureDevice.requestAccess(for: .audio)
        default:
            return false
        }
    }
}
#endif
