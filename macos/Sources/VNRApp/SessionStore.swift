#if os(macOS)
import AVFoundation
import Combine
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
    /// True while a live socket exists. While it does, the socket is authoritative about
    /// readiness and `/health` is not polled at all.
    @Published public private(set) var isConnected = false

    @Published public private(set) var isPreparing = false

    private var isStarting = false
    private let endpoint: ServiceEndpoint
    private let capture = AudioCapture()
    private var client: ServiceClient?
    private var reader: Task<Void, Never>?
    private var healthPoll: Task<Void, Never>?

    public init(endpoint: ServiceEndpoint) {
        self.endpoint = endpoint
    }

    public var canRecord: Bool { model.canRecord(healthGate: gate) && !isRecording }

    /// For the menu-bar label, which observes the app delegate rather than this object.
    ///
    /// An explicit publisher because `$isRecording` inherits the *setter's* access level
    /// from `private(set)`, so it cannot be reached from outside — and keeping the setter
    /// private is worth more than the one line this costs.
    public var recordingChanged: AnyPublisher<Bool, Never> {
        $isRecording.eraseToAnyPublisher()
    }

    // MARK: - Health, which gates the record button

    /// Polls `/health` **only while there is no socket**.
    ///
    /// The service pushes readiness on `session.state_changed`, so once the socket is up
    /// polling is asking a question already being answered — and it is not a free
    /// question: the process on the other end is holding a 1.7 GB resident model. The
    /// first version polled every 10 s regardless and put dozens of requests into a
    /// single session's log.
    ///
    /// It still runs while disconnected, which is the case that needs it: before the
    /// first connection the record button has no other way to know the model is in, and
    /// after a drop it is how the button goes dead again.
    public func startHealthPolling() {
        healthPoll?.cancel()
        healthPoll = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if self.isConnected {
                    // Idle-but-connected: check back rarely, only to notice a socket
                    // that went away without the reader hearing about it.
                    try? await Task.sleep(nanoseconds: 30_000_000_000)
                    continue
                }
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
        guard model.state.acceptsNewRecording, !isStarting, !isRecording else { return }
        isStarting = true
        isPreparing = !gate.allowsRecording
        defer { isPreparing = false; isStarting = false }
        notice = nil
        do {
            var request = URLRequest(url: endpoint.healthURL.deletingLastPathComponent()
                .appendingPathComponent("asr/prepare"))
            request.httpMethod = "POST"
            request.timeoutInterval = 180
            let (data, _) = try await URLSession.shared.data(for: request)
            let health = try JSONDecoder().decode(ServiceHealth.self, from: data)
            gate = health.gate
            guard health.ready else { notice = gate.explanation; return }
        } catch {
            notice = "Could not prepare speech recognition: \(error.localizedDescription)"
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
            let deadline = Date().addingTimeInterval(5)
            while model.state != .listening && isConnected && Date() < deadline {
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            guard isConnected, model.state == .listening else {
                notice = "Recording did not start. Try again when the service is ready."
                return
            }
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
            disconnected(notice: "The connection dropped while recording.")
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
        isConnected = false

        let fresh = ServiceClient(channel: URLSessionChannel(endpoint: endpoint))
        client = fresh
        isConnected = true
        // One capture list, on the Task. Inside it `self` is already the weak optional,
        // so a second `[weak self]` on a nested closure has nothing left to weaken.
        //
        // This Task inherits the enclosing @MainActor isolation, which is why the calls
        // to `report` below need no `await`: it was never on another actor. The inner
        // Task *does* need `@MainActor`, because `run`'s callback arrives on the
        // ServiceClient actor rather than this one.
        reader = Task { [weak self] in
            do {
                try await fresh.run { event, model, changed in
                    Task { @MainActor in
                        self?.absorb(event, model, changed)
                    }
                }
                self?.disconnected(notice: nil)
            } catch ServiceClientError.busy {
                self?.disconnected(notice: "Another client is already using the model.")
            } catch {
                self?.disconnected(notice: "Disconnected: \(error.localizedDescription)")
            }
        }
    }

    /// The socket is gone: say so, stop capturing, and let health polling take over the
    /// record-button gate again.
    private func disconnected(notice message: String?) {
        isConnected = false
        if let message { report(message) }
        if isRecording {
            capture.stop()
            isRecording = false
        }
    }

    private func absorb(_ event: ServiceEvent, _ model: SessionModel, _ changed: Bool) {
        // Nothing changed means nothing to redraw — streaming ASR repeats itself about
        // thirty times an utterance, and publishing each one flickers the overlay.
        guard changed else { return }
        self.model = model
        // The socket is authoritative while it is open: `session.state_changed` carries
        // readiness, so the gate follows it and `/health` stays quiet.
        if let ready = model.ready {
            gate = ready ? .recordingEnabled : .sleeping
        }
        // The draft follows the ASR only until the user types; `follow` enforces that,
        // so a final transcript landing after an edit cannot discard the correction.
        draft.follow(model.transcript)
    }

    /// No `@MainActor` here: the whole class carries it, and repeating it invited the
    /// call sites to `await` something that never crosses an actor.
    private func report(_ message: String) {
        notice = message
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
