import Foundation

/// The states a session moves through, mirroring `SessionState` in `src/vnr/events.py`.
///
/// Decoding is total: an unrecognised state becomes ``unknown`` rather than throwing, so
/// a service that gains a state does not break a UI built against an older copy.
public enum SessionState: Equatable, Sendable {
    case idle
    case listening
    case finalizingTranscript
    case review
    case submitted
    case researchStarted
    case searchStarted
    case searchCompleted
    case synthesizing
    case answerStreaming
    case completed
    case failed
    case cancelled
    case unknown(String)

    public init(wireName: String) {
        switch wireName {
        case "IDLE": self = .idle
        case "LISTENING": self = .listening
        case "FINALIZING_TRANSCRIPT": self = .finalizingTranscript
        case "REVIEW": self = .review
        case "SUBMITTED": self = .submitted
        case "RESEARCH_STARTED": self = .researchStarted
        case "SEARCH_STARTED": self = .searchStarted
        case "SEARCH_COMPLETED": self = .searchCompleted
        case "SYNTHESIZING": self = .synthesizing
        case "ANSWER_STREAMING": self = .answerStreaming
        case "COMPLETED": self = .completed
        case "FAILED": self = .failed
        case "CANCELLED": self = .cancelled
        default: self = .unknown(wireName)
        }
    }

    /// True while the user may start a new utterance — the states the controller calls
    /// settled. Recording is refused in any other state.
    public var acceptsNewRecording: Bool {
        switch self {
        case .idle, .review, .completed, .failed, .cancelled: return true
        default: return false
        }
    }

    /// True while research is in flight, so the UI can show progress rather than a prompt.
    public var isResearching: Bool {
        switch self {
        case .submitted, .researchStarted, .searchStarted, .searchCompleted,
             .synthesizing, .answerStreaming:
            return true
        default:
            return false
        }
    }
}
