import Foundation

/// Everything the UI draws, derived from the event stream and nothing else.
///
/// PLAN §19's rule, made structural: the UI holds no opinion about what happens next. It
/// does not decide that pressing GO starts research, or that a search finished — it
/// applies what the service said. The value of that is not tidiness. The controller is
/// the single authority on session state (PLAN §7: research never starts without an
/// explicit GO), and a UI that predicts state can disagree with it. A UI that only
/// renders cannot.
///
/// Pure Foundation and free of I/O, so `VNRKitCheck` drives it with recorded events on
/// Linux. Only the socket underneath it needs a Mac.
public struct SessionModel: Equatable, Sendable {
    /// The live transcript: partials overwrite, the final replaces.
    public private(set) var transcript = ""
    /// True once `asr.final` has landed, so the UI knows the text is editable.
    public private(set) var transcriptIsFinal = false
    public private(set) var state: SessionState = .idle
    /// From `session.state_changed`; nil until the service has said.
    public private(set) var ready: Bool?
    public private(set) var searches: [SearchRow] = []
    public private(set) var answer = ""
    public private(set) var citations: [ServiceEvent.Citation] = []
    public private(set) var completion: ServiceEvent.Completion?
    public private(set) var failure: Failure?
    /// Event types this build does not understand. Kept so they can be logged rather
    /// than vanishing — the contract test catches drift, this catches it at runtime too.
    public private(set) var unknownEventTypes: [String] = []

    public init() {}

    public struct SearchRow: Equatable, Sendable, Identifiable {
        public let index: Int
        public var query: String
        public var depth: String?
        public var resultCount: Int?
        public var error: String?

        public var id: Int { index }
        /// A row with no outcome yet — the spinner case.
        public var isRunning: Bool { resultCount == nil && error == nil }
    }

    public struct Failure: Equatable, Sendable {
        public let code: String
        public let message: String
        /// ASR failures and research failures read the same to a user but not to the
        /// code that recovers from them.
        public let duringResearch: Bool
    }

    // MARK: - The only mutation

    public mutating func apply(_ event: ServiceEvent) {
        switch event.kind {
        case .asrPartial(let text):
            transcript = text
            transcriptIsFinal = false
        case .asrFinal(let text):
            transcript = text
            transcriptIsFinal = true
        case .asrError(let code, let message):
            failure = Failure(code: code, message: message, duringResearch: false)

        case .researchStarted:
            // A new question: clear what belonged to the previous answer, so a failed
            // run's sources cannot sit under a fresh one.
            searches = []
            answer = ""
            citations = []
            completion = nil
            failure = nil
        case .searchStarted(let index, let query, let depth):
            upsert(index: index) { row in
                row.query = query
                row.depth = depth
            }
        case .searchCompleted(let index, let query, let resultCount, let error):
            upsert(index: index) { row in
                row.query = query
                row.resultCount = resultCount
                row.error = error
            }
        case .synthesizing:
            break
        case .answerDelta(let text):
            // Deltas append. The service streams the final synthesis only, so there is
            // no case where a delta should replace what came before.
            answer += text
        case .researchCompleted(let done):
            completion = done
            citations = done.citedSources
        case .researchFailed(let code, let message):
            failure = Failure(code: code, message: message, duringResearch: true)
        case .researchCancelled:
            break

        case .stateChanged(let newState, let isReady):
            state = newState
            if let isReady { ready = isReady }

        case .unknown(let type):
            unknownEventTypes.append(type)
        }
    }

    /// Insert or update by index rather than appending.
    ///
    /// `search_started` and `search_completed` carry the same index, and a WebSocket can
    /// deliver a completion for a search whose start was missed. Appending would show the
    /// same search twice; keying on the index shows it once, in order, either way.
    private mutating func upsert(index: Int, _ edit: (inout SearchRow) -> Void) {
        if let position = searches.firstIndex(where: { $0.index == index }) {
            edit(&searches[position])
        } else {
            var row = SearchRow(index: index, query: "")
            edit(&row)
            searches.append(row)
            searches.sort { $0.index < $1.index }
        }
    }

    // MARK: - Derived, so the UI does not re-derive it four ways

    /// Whether the record button should be live, given the service's readiness *and* the
    /// session's state. Both have to agree: a ready model mid-research is still not a
    /// moment to start talking.
    public func canRecord(healthGate: ServiceHealth.Gate) -> Bool {
        healthGate.allowsRecording && state.acceptsNewRecording && ready != false
    }

    /// True once the user may edit the transcript and press GO (PLAN §7).
    public var awaitingApproval: Bool { state == .review }

    public var searchesCompleted: Int {
        searches.filter { !$0.isRunning }.count
    }
}
