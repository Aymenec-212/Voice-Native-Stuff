import Foundation

/// `GET /health` — what gates the record button.
///
/// The model loads on demand after startup or an idle unload and takes seconds. A record button that is
/// live before then produces an utterance the service cannot transcribe, which surfaces
/// as an error the user cannot act on. So readiness is asked for, not assumed.
public struct ServiceHealth: Equatable, Sendable, Decodable {
    public let ready: Bool
    public let engine: String
    public let model: String
    /// Set when the model failed to load. The service deliberately stays up in that case,
    /// so "reachable" and "usable" are different questions and this is the second one.
    public let error: String?

    public init(ready: Bool, engine: String, model: String, error: String? = nil) {
        self.ready = ready
        self.engine = engine
        self.model = model
        self.error = error
    }

    /// What the UI should do about it, rather than a bare Bool the caller must interpret.
    public enum Gate: Equatable, Sendable {
        case recordingEnabled
        /// Still loading. Transient — poll again.
        case loading
        case sleeping
        /// The model failed to load. Not transient; says so.
        case unusable(String)
        /// Nothing answered on the port.
        case unreachable
    }

    public var gate: Gate {
        if let error, !error.isEmpty { return .unusable(error) }
        return ready ? .recordingEnabled : .sleeping
    }
}

extension ServiceHealth.Gate {
    /// The one question the record button asks.
    public var allowsRecording: Bool { self == .recordingEnabled }

    /// What to show next to a disabled button. Never empty, so there is no state in
    /// which the button is dead and the UI says nothing about why.
    public var explanation: String {
        switch self {
        case .recordingEnabled: return "Ready."
        case .sleeping: return "Speech model resting · press ⌃⌥Space to wake"
        case .loading: return "Loading the speech model…"
        case .unusable(let reason): return "Speech model unavailable: \(reason)"
        case .unreachable:
            return "The local service is not running. Start it with: uv run vnr-service"
        }
    }
}
