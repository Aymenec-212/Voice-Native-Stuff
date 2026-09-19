import Foundation

/// The commands the UI sends, mirroring the `match` in `src/vnr/service.py`.
///
/// The service rejects an unknown command rather than ignoring it, so these strings are
/// part of the contract and are asserted against the Python side's literals by
/// `VNRKitCheck`. Spelling one wrong is a runtime rejection, not a compile error, which
/// is exactly the kind of mistake a checked constant prevents.
public enum ClientCommand: Equatable, Sendable {
    case startRecording
    case stopRecording
    /// The text the user approved in the review step — which may differ from the ASR's.
    /// PLAN §7: this is the product, and it is why the transcript is editable.
    case submit(query: String)
    case cancel
    case reset

    public var wireType: String {
        switch self {
        case .startRecording: return "recording.start"
        case .stopRecording: return "recording.stop"
        case .submit: return "research.submit"
        case .cancel: return "research.cancel"
        case .reset: return "session.reset"
        }
    }

    /// JSON text frame. Audio goes as a *binary* frame instead and has no envelope.
    public func jsonText() throws -> String {
        var object: [String: Any] = ["type": wireType]
        if case let .submit(query) = self {
            object["query"] = query
        }
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        return String(decoding: data, as: UTF8.self)
    }
}
