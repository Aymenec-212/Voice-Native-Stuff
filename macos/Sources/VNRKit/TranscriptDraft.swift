import Foundation

/// The editable transcript, and what GO or Cancel does with it (PLAN §7).
///
/// This is the product. "Recording end must not start research. Enter REVIEW: editable
/// field, user may correct, cancel, or press GO. GO freezes that text as the request."
///
/// It lives here, in pure Foundation, rather than inside a SwiftUI view for one reason:
/// the rule that matters most is invisible in a screenshot and impossible to check by
/// clicking around — **once the user has typed, the ASR must stop overwriting them**. A
/// final transcript can land after the field is on screen, and a view that binds
/// straight to `SessionModel.transcript` silently discards the correction the user just
/// made. That is the worst possible bug here, because the whole design rests on the
/// submitted text being theirs.
public struct TranscriptDraft: Equatable, Sendable {
    /// What is in the field, exactly as the user would see it.
    public private(set) var text: String = ""
    /// True once the user has changed it, after which the ASR no longer writes here.
    public private(set) var isEdited: Bool = false

    public init() {}

    /// The ASR's latest text. Returns whether the field changed.
    ///
    /// Ignored entirely once `isEdited` — a late partial or the final transcript must not
    /// overwrite a correction. Keeping both texts is also what PLAN §7 asks for: the
    /// difference between them is the ASR quality signal, and it only exists if the edit
    /// survives.
    @discardableResult
    public mutating func follow(_ transcript: String) -> Bool {
        guard !isEdited, transcript != text else { return false }
        text = transcript
        return true
    }

    /// The user typed. From here the field is theirs.
    public mutating func edit(to newText: String) {
        text = newText
        isEdited = true
    }

    /// A new utterance: the field goes back to following the ASR.
    public mutating func reset() {
        text = ""
        isEdited = false
    }

    public var trimmed: String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// GO is refused on an empty field. Nothing useful can be researched from it, and
    /// silently sending it would spend a Tavily credit on nothing.
    public var canSubmit: Bool { !trimmed.isEmpty }
}

/// What the review step does, expressed as the command it sends.
///
/// The distinction the UI has to get right: **Cancel in review is `session.reset`, not
/// `research.cancel`.** Nothing is running yet — `research.cancel` cancels research in
/// flight, and sending it here would be rejected by the controller as a command the
/// session cannot accept. They read alike in English and are different commands.
public enum ReviewDecision: Equatable, Sendable {
    case submit(ClientCommand)
    /// GO with nothing to send. The UI disables the button; this exists so the rule is
    /// stated once rather than implied by a disabled control.
    case refused(reason: String)

    public static func go(_ draft: TranscriptDraft) -> ReviewDecision {
        guard draft.canSubmit else {
            return .refused(reason: "Nothing to research — the transcript is empty.")
        }
        // Trimmed, because trailing whitespace from an edit is not part of the question.
        return .submit(.submit(query: draft.trimmed))
    }

    /// Discard the utterance and go back to idle, ready to record again.
    public static let discard = ReviewDecision.submit(.reset)
}
