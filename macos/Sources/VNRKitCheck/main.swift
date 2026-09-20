import Foundation
import VNRKit

// The checks that used to be XCTest cases. `swift run VNRKitCheck` — no Xcode required,
// and it runs on Linux, which is what puts both languages under one CI workflow.

let fixtures = loadFixtures()

func event(_ label: String, occurrence: Int = 0) -> ServiceEvent? {
    let matching = fixtures.filter { $0.label == label }
    guard let fixture = matching.dropFirst(occurrence).first else {
        Check.that(false, "fixture \(label)[\(occurrence)] exists")
        return nil
    }
    guard let decoded = try? ServiceEvent.decode(from: Data(fixture.json.utf8)) else {
        Check.that(false, "\(label) decodes")
        return nil
    }
    return decoded
}

// MARK: - Every fixture decodes

Check.section("Fixtures")
Check.that(!fixtures.isEmpty, "fixtures were loaded (\(fixtures.count))")
for fixture in fixtures {
    let decoded = try? ServiceEvent.decode(from: Data(fixture.json.utf8))
    Check.that(decoded != nil, "\(fixture.label) decodes")
}

if let partial = event("asr.partial") {
    Check.equal(partial.sessionID, "fixture0001", "envelope carries session_id")
    Check.equal(partial.timestamp, 0, "envelope carries ts")
}

// MARK: - Speech

Check.section("Speech events")
if let decoded = event("asr.partial"), case .asrPartial(let text) = decoded.kind {
    Check.equal(text, "find recent work on streaming", "asr.partial text")
} else {
    Check.that(false, "asr.partial is .asrPartial")
}

if let decoded = event("asr.final"), case .asrFinal(let text) = decoded.kind {
    Check.equal(text, "find recent work on streaming ASR for Darija", "asr.final text")
} else {
    Check.that(false, "asr.final is .asrFinal")
}

if let decoded = event("asr.error"), case .asrError(let code, let message) = decoded.kind {
    Check.equal(code, "microphone", "asr.error code")
    Check.that(message.contains("silence"), "asr.error explains the silence")
} else {
    Check.that(false, "asr.error is .asrError")
}

// MARK: - Research progress

Check.section("Research progress")
if let decoded = event("research.search_started"),
   case .searchStarted(let index, let query, let depth) = decoded.kind {
    Check.equal(index, 1, "search index")
    Check.equal(query, "streaming ASR low-resource languages", "search query")
    Check.equal(depth, "advanced", "search depth when present")
} else {
    Check.that(false, "research.search_started is .searchStarted")
}

if let decoded = event("research.search_completed"),
   case .searchCompleted(_, _, let count, let error) = decoded.kind {
    Check.equal(count, 5, "result count")
    Check.that(error == nil, "a successful search carries no error")
} else {
    Check.that(false, "research.search_completed is .searchCompleted")
}

if let decoded = event("research.search_completed", occurrence: 1),
   case .searchCompleted(_, _, let count, let error) = decoded.kind {
    Check.equal(count, 0, "failed search returns no results")
    Check.equal(error, "tavily_rate_limit", "failed search carries the reason")
} else {
    Check.that(false, "the second research.search_completed is .searchCompleted")
}

if let decoded = event("research.answer_delta"), case .answerDelta(let text) = decoded.kind {
    Check.equal(text, "Kyutai streams audio [1].", "answer delta text")
} else {
    Check.that(false, "research.answer_delta is .answerDelta")
}

// MARK: - Completion

Check.section("Completion")
if let decoded = event("research.completed"),
   case .researchCompleted(let completion) = decoded.kind {
    Check.equal(completion.turns, 3, "turns")
    Check.equal(completion.searches, 2, "searches")
    Check.equal(completion.sourceCount, 5, "source count")
    Check.equal(completion.stopReason, "evidence_sufficient", "stop reason")
    Check.equal(completion.tavilyCredits, 2, "tavily credits from metrics")
    Check.equal(completion.totalMilliseconds, 9720.4, "total ms from metrics")
    Check.equal(completion.invalidCitationIDs, ["S42"], "dropped citation ids")

    // The numbers must line up with the [n] markers in the answer text.
    Check.equal(completion.citedSources.count, 1, "cited source count")
    if let citation = completion.citedSources.first {
        Check.equal(citation.number, 1, "citation number matches its [n] marker")
        Check.equal(citation.id, "S1", "citation source id")
        Check.equal(citation.url, "https://kyutai.org/stt", "citation url")
    }
} else {
    Check.that(false, "research.completed is .researchCompleted")
}

if let decoded = event("research.failed"), case .researchFailed(let code, let message) = decoded.kind {
    Check.equal(code, "nebius_auth", "failure code")
    Check.that(!message.isEmpty, "failure carries a message")
} else {
    Check.that(false, "research.failed is .researchFailed")
}

if let decoded = event("research.cancelled") {
    if case .researchCancelled = decoded.kind {
        Check.that(true, "research.cancelled is .researchCancelled")
    } else {
        Check.that(false, "research.cancelled is .researchCancelled")
    }
}

// MARK: - Forward compatibility

Check.section("Forward compatibility")
if let decoded = event("research.future_thing"), case .unknown(let type) = decoded.kind {
    Check.equal(type, "research.future_thing", "an unrecognised type decodes to .unknown")
} else {
    Check.that(false, "an unrecognised type must not throw")
}

if let decoded = try? ServiceEvent.decode(from: Data(#"{"type": "asr.partial"}"#.utf8)),
   case .asrPartial(let text) = decoded.kind {
    Check.equal(text, "", "a missing data payload defaults rather than throwing")
} else {
    Check.that(false, "a missing data payload must not lose the event")
}

let wrongType = #"{"type": "research.synthesizing", "data": {"source_count": "five"}}"#
if let decoded = try? ServiceEvent.decode(from: Data(wrongType.utf8)),
   case .synthesizing(let count) = decoded.kind {
    Check.equal(count, 0, "a wrongly typed field falls back rather than throwing")
} else {
    Check.that(false, "a wrongly typed field must not lose the event")
}

// MARK: - Session state

Check.section("Session state")
Check.equal(SessionState(wireName: "REVIEW"), .review, "REVIEW maps")
Check.equal(SessionState(wireName: "ANSWER_STREAMING"), .answerStreaming, "ANSWER_STREAMING maps")
Check.equal(SessionState(wireName: "WAT"), .unknown("WAT"), "an unknown state is carried")

let ready = #"{"type": "session.state_changed", "data": {"state": "IDLE", "ready": false}}"#
if let decoded = try? ServiceEvent.decode(from: Data(ready.utf8)),
   case .stateChanged(let state, let isReady) = decoded.kind {
    Check.equal(state, .idle, "opening state")
    Check.equal(isReady, false, "readiness gates the record button")
} else {
    Check.that(false, "session.state_changed is .stateChanged")
}

Check.that(SessionState.idle.acceptsNewRecording, "IDLE accepts a new recording")
Check.that(SessionState.completed.acceptsNewRecording, "COMPLETED accepts a new recording")
Check.that(SessionState.failed.acceptsNewRecording, "FAILED accepts a new recording")
Check.that(!SessionState.listening.acceptsNewRecording, "LISTENING refuses a new recording")
Check.that(!SessionState.synthesizing.acceptsNewRecording, "SYNTHESIZING refuses a new recording")
Check.that(SessionState.answerStreaming.isResearching, "ANSWER_STREAMING is researching")
Check.that(!SessionState.review.isResearching, "REVIEW is not researching")

runAudioChecks()

// Top-level `await` rather than a semaphore: top-level code is main-actor isolated, so
// blocking it while a Task tries to finish is a deadlock waiting for a slow machine.
await runClientChecks(fixtures: fixtures)
runReviewChecks()

Check.section("Answer Markdown")
Check.equal(AnswerMarkdown.blocks("#Title\n\nText **bold** [1]."),
            [.heading(1, "Title"), .paragraph("Text **bold** [1].")], "compact heading and inline text")
Check.equal(AnswerMarkdown.blocks("## Heading\n- one\n  - two\n2. next"),
            [.heading(2, "Heading"), .item("•", "one", 0), .item("•", "two", 2),
             .item("2.", "next", 0)], "headings and nested numbered/bullet lists")
Check.equal(AnswerMarkdown.blocks("```swift\n# not a heading\nunfinished"),
            [.code("# not a heading\nunfinished")], "streaming unclosed fence preserves code")
Check.equal(AnswerMarkdown.blocks("> quoted\n\n---\n\nTitle\n==="),
            [.quote("quoted"), .divider, .heading(1, "Title")], "quote, rule, setext heading")
Check.equal(AnswerMarkdown.blocks("| A | B |\n| --- | :---: |\n| 1 | 2 |"),
            [.table([["A", "B"], ["1", "2"]])], "table structure")
Check.equal(AnswerMarkdown.blocks(""), [], "empty streaming answer")

Check.finish()
