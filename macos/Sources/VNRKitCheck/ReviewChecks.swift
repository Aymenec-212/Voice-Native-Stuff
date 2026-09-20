import Foundation
import VNRKit

/// PLAN §7 — the review step, which is the product.
func runReviewChecks() {
    Check.section("The field follows the ASR until the user types")
    var draft = TranscriptDraft()
    Check.equal(draft.text, "", "a fresh draft is empty")
    Check.that(!draft.isEdited, "and unedited")
    Check.that(!draft.canSubmit, "GO is refused on an empty field")

    Check.that(draft.follow("find recent"), "a partial fills the field")
    Check.equal(draft.text, "find recent", "with the ASR's text")
    Check.that(draft.follow("find recent work"), "a later partial replaces it")
    Check.that(!draft.follow("find recent work"), "an identical one changes nothing")
    Check.that(!draft.isEdited, "following is not editing")
    Check.that(draft.canSubmit, "GO is allowed once there is text")

    Check.section("Once the user types, the ASR stops writing")
    // The bug this exists to prevent: the final transcript lands *after* the field is on
    // screen, and a view bound straight to SessionModel.transcript throws away the
    // correction the user just made. The submitted text must be theirs.
    draft.edit(to: "find recent work on Darija ASR")
    Check.that(draft.isEdited, "editing marks the draft")
    Check.that(
        !draft.follow("find recent work on daria ASR"),
        "a late partial does not overwrite the correction"
    )
    Check.equal(
        draft.text, "find recent work on Darija ASR", "the user's text survives verbatim"
    )
    // The final is the most dangerous one: it arrives last and looks authoritative.
    Check.that(!draft.follow("whatever the model heard"), "nor does the final transcript")
    Check.equal(draft.text, "find recent work on Darija ASR", "still theirs")

    Check.section("A new utterance starts clean")
    draft.reset()
    Check.equal(draft.text, "", "reset empties the field")
    Check.that(!draft.isEdited, "and the ASR may write again")
    Check.that(draft.follow("a new question"), "which it does")

    Check.section("GO freezes the text as the request")
    var ready = TranscriptDraft()
    ready.follow("  compare Kyutai and Nebius  ")
    if case .submit(let command) = ReviewDecision.go(ready) {
        Check.equal(command.wireType, "research.submit", "GO submits")
        // Trailing whitespace from an edit is not part of the question.
        Check.equal(
            try? command.jsonText(),
            #"{"query":"compare Kyutai and Nebius","type":"research.submit"}"#,
            "the query is trimmed and carries the field's text"
        )
    } else {
        Check.that(false, "GO on a filled field submits")
    }

    var edited = TranscriptDraft()
    edited.follow("compare kutai and nebius")
    edited.edit(to: "compare Kyutai and Nebius")
    if case .submit(let command) = ReviewDecision.go(edited),
       case .submit(let query) = command {
        // PLAN §7: raw_asr_transcript and submitted_query may differ, and the difference
        // is the ASR quality signal. It only exists if GO sends the edit.
        Check.equal(query, "compare Kyutai and Nebius", "GO sends the edit, not the ASR text")
    } else {
        Check.that(false, "GO sends the edited text")
    }

    Check.section("GO is refused on an empty field")
    var blank = TranscriptDraft()
    blank.edit(to: "   \n  ")
    Check.that(!blank.canSubmit, "whitespace is not a question")
    if case .refused(let reason) = ReviewDecision.go(blank) {
        Check.that(!reason.isEmpty, "and the refusal says why")
    } else {
        Check.that(false, "GO on an empty field is refused")
    }

    Check.section("Cancel in review is a reset, not a research cancel")
    // These read alike in English and are different commands: research.cancel stops a
    // run in flight, and nothing is running yet — the controller would reject it.
    if case .submit(let command) = ReviewDecision.discard {
        Check.equal(command.wireType, "session.reset", "Cancel sends session.reset")
        Check.that(command.wireType != "research.cancel", "not research.cancel")
    } else {
        Check.that(false, "discard is a command")
    }
}
