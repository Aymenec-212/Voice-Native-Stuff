import Foundation
import VNRKit

/// A channel that replays a scripted session and records what was sent.
///
/// This is the double that makes slice 3 checkable on Linux. It is deliberately a fake
/// *transport*, not a fake client: it cannot hide a bug in how events are folded into
/// the model, because it does not do any folding. (Slice 2's lesson — ask what class of
/// bug a double makes invisible. This one hides bugs in `URLSessionWebSocketTask` use,
/// and nothing else, which is precisely the part that needs a Mac anyway.)
final class ScriptedChannel: WebSocketChannel, @unchecked Sendable {
    private var inbound: [WebSocketFrame]
    private(set) var sentText: [String] = []
    private(set) var sentBinary: [Data] = []
    private(set) var closed = false
    private let lock = NSLock()

    /// Thrown from `receive()` instead of returning a frame, to stand in for a socket
    /// the service refused or dropped.
    var failWith: ServiceClientError?

    init(events json: [String], failWith: ServiceClientError? = nil) {
        inbound = json.map { .text($0) }
        self.failWith = failWith
    }

    func send(text: String) async throws {
        lock.lock(); defer { lock.unlock() }
        sentText.append(text)
    }

    func send(binary: Data) async throws {
        lock.lock(); defer { lock.unlock() }
        sentBinary.append(binary)
    }

    func receive() async throws -> WebSocketFrame? {
        lock.lock(); defer { lock.unlock() }
        if !inbound.isEmpty { return inbound.removeFirst() }
        if let failWith { throw failWith }
        return nil
    }

    func close() {
        lock.lock(); defer { lock.unlock() }
        closed = true
    }
}

/// A counter a `@Sendable` closure may increment.
///
/// A captured local `var` cannot be mutated from concurrently-executing code, and the
/// event callback crosses an actor boundary. A reference box is the honest way to say
/// "this is shared" rather than fighting the compiler about it.
private final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0

    func increment() {
        lock.lock(); defer { lock.unlock() }
        value += 1
    }

    var count: Int {
        lock.lock(); defer { lock.unlock() }
        return value
    }
}

/// Applies one raw event to *model*, failing a check rather than trapping if it will not
/// decode. Keeps the checks below readable without a fallback event that could itself lie.
private func apply(_ json: String, to model: inout SessionModel) {
    guard let event = try? ServiceEvent.decode(from: Data(json.utf8)) else {
        Check.that(false, "could not decode \(json)")
        return
    }
    model.apply(event)
}

func runClientChecks(fixtures: [Fixture]) async {
    Check.section("Service endpoint")
    // PLAN §2: raw audio never leaves the Mac. The client refuses a non-loopback host so
    // that rule cannot be undone by a config value.
    if let local = try? ServiceEndpoint() {
        Check.equal(local.websocketURL.absoluteString, "ws://127.0.0.1:8765/ws", "ws URL")
        Check.equal(local.healthURL.absoluteString, "http://127.0.0.1:8765/health", "health URL")
    } else {
        Check.that(false, "the default endpoint builds")
    }
    Check.that((try? ServiceEndpoint(host: "localhost")) != nil, "localhost is loopback")
    Check.that((try? ServiceEndpoint(host: "127.0.0.1")) != nil, "127.0.0.1 is loopback")
    Check.that((try? ServiceEndpoint(host: "example.com")) == nil, "a remote host is refused")
    Check.that((try? ServiceEndpoint(host: "0.0.0.0")) == nil, "the wildcard address is refused")
    Check.that((try? ServiceEndpoint(host: "  LocalHost ")) != nil, "case and spaces tolerated")
    Check.that((try? ServiceEndpoint(port: 0)) == nil, "port 0 is refused")
    Check.that((try? ServiceEndpoint(port: 70_000)) == nil, "an out-of-range port is refused")
    if let six = try? ServiceEndpoint(host: "::1") {
        Check.equal(six.websocketURL.absoluteString, "ws://[::1]:8765/ws", "IPv6 is bracketed")
    }

    Check.section("Client commands")
    // These strings are matched literally by src/vnr/service.py, which rejects anything
    // it does not recognise. A typo here is a runtime rejection, not a compile error.
    Check.equal(ClientCommand.startRecording.wireType, "recording.start", "start")
    Check.equal(ClientCommand.stopRecording.wireType, "recording.stop", "stop")
    Check.equal(ClientCommand.submit(query: "x").wireType, "research.submit", "submit")
    Check.equal(ClientCommand.cancel.wireType, "research.cancel", "cancel")
    Check.equal(ClientCommand.reset.wireType, "session.reset", "reset")
    Check.equal(
        try? ClientCommand.startRecording.jsonText(),
        #"{"type":"recording.start"}"#,
        "a bare command carries only its type"
    )
    Check.equal(
        try? ClientCommand.submit(query: "compare Kyutai and Nebius").jsonText(),
        #"{"query":"compare Kyutai and Nebius","type":"research.submit"}"#,
        "submit carries the approved text"
    )
    // The edited text is the product (PLAN §7), so quoting must survive the trip.
    Check.equal(
        try? ClientCommand.submit(query: #"say "hi" \ now"#).jsonText(),
        #"{"query":"say \"hi\" \\ now","type":"research.submit"}"#,
        "quotes and backslashes are escaped, not mangled"
    )

    Check.section("Health gate")
    Check.that(
        ServiceHealth(ready: true, engine: "mlx", model: "m").gate.allowsRecording,
        "a ready service enables recording"
    )
    Check.that(
        !ServiceHealth(ready: false, engine: "mlx", model: "m").gate.allowsRecording,
        "an unready service does not"
    )
    // The service deliberately stays up when the model fails to load, so "answering" and
    // "usable" are different questions and a load error must not read as merely loading.
    Check.equal(
        ServiceHealth(ready: false, engine: "mlx", model: "m", error: "no weights").gate,
        .unusable("no weights"),
        "a load error is reported as unusable, not as loading"
    )
    Check.equal(
        ServiceHealth(ready: false, engine: "mlx", model: "m", error: "").gate,
        .sleeping,
        "an empty error string is not an error"
    )
    let everyGate: [ServiceHealth.Gate] = [
        .recordingEnabled, .loading, .unusable("x"), .unreachable,
    ]
    for gate in everyGate {
        // A dead button with no explanation is the worst version of this screen.
        Check.that(!gate.explanation.isEmpty, "every gate explains itself")
    }
    if let decoded = try? JSONDecoder().decode(
        ServiceHealth.self,
        from: Data(#"{"ready":true,"engine":"mlx","model":"n/N","error":null}"#.utf8)
    ) {
        Check.equal(decoded.ready, true, "health decodes from the service's JSON")
        Check.equal(decoded.engine, "mlx", "health carries the engine name")
        Check.that(decoded.error == nil, "a null error decodes as nil")
    } else {
        Check.that(false, "health decodes from the service's JSON")
    }

    Check.section("Session model")
    var model = SessionModel()
    Check.equal(model.transcript, "", "a fresh model shows nothing")
    Check.that(!model.transcriptIsFinal, "and nothing is final yet")
    Check.that(model.ready == nil, "readiness is unknown until the service says")

    // Readiness is unknown, not false — but recording still waits for it, because a
    // record button that works before the model is in produces an unusable utterance.
    Check.that(
        model.canRecord(healthGate: .recordingEnabled),
        "an unstated readiness does not block recording once health says ready"
    )
    Check.that(
        !model.canRecord(healthGate: .loading),
        "a loading model blocks the record button"
    )

    for fixture in fixtures {
        if let decoded = try? ServiceEvent.decode(from: Data(fixture.json.utf8)) {
            model.apply(decoded)
        }
    }
    Check.that(!model.unknownEventTypes.isEmpty, "an unknown event type is kept, not dropped")
    Check.equal(
        model.unknownEventTypes.first, "research.future_thing", "and it is named"
    )

    Check.section("Rendering from the stream alone")
    var stream = SessionModel()
    apply(#"{"type":"asr.partial","data":{"text":"find recent"}}"#, to: &stream)
    Check.equal(stream.transcript, "find recent", "a partial shows immediately")
    apply(#"{"type":"asr.partial","data":{"text":"find recent work"}}"#, to: &stream)
    Check.equal(stream.transcript, "find recent work", "a later partial replaces it")
    Check.that(!stream.transcriptIsFinal, "partials are not final")
    apply(
        #"{"type":"asr.final","data":{"text":"find recent work on ASR"}}"#, to: &stream
    )
    Check.equal(stream.transcript, "find recent work on ASR", "the final replaces")
    Check.that(stream.transcriptIsFinal, "and is marked final, so the field becomes editable")

    apply(
        #"{"type":"session.state_changed","data":{"state":"REVIEW"}}"#, to: &stream
    )
    Check.that(stream.awaitingApproval, "REVIEW is the approval step (PLAN §7)")
    Check.that(stream.canRecord(healthGate: .recordingEnabled), "and re-recording is allowed")

    // Answer deltas accumulate; anything else would lose the stream.
    apply(#"{"type":"research.answer_delta","data":{"text":"Kyutai "}}"#, to: &stream)
    apply(#"{"type":"research.answer_delta","data":{"text":"ships "}}"#, to: &stream)
    apply(#"{"type":"research.answer_delta","data":{"text":"MLX [1]."}}"#, to: &stream)
    Check.equal(stream.answer, "Kyutai ships MLX [1].", "deltas append in order")

    Check.section("Search rows")
    var rows = SessionModel()
    apply(
        #"{"type":"research.search_started","data":{"index":0,"query":"kyutai stt"}}"#,
        to: &rows
    )
    Check.equal(rows.searches.count, 1, "a started search appears at once")
    Check.that(rows.searches[0].isRunning, "with no outcome yet")
    Check.equal(rows.searchesCompleted, 0, "and is not counted as done")
    apply(
        #"""
        {"type":"research.search_completed",
         "data":{"index":0,"query":"kyutai stt","result_count":5}}
        """#,
        to: &rows
    )
    // Keyed on index, not appended: the same search must not show twice.
    Check.equal(rows.searches.count, 1, "completing updates the row rather than adding one")
    Check.equal(rows.searches[0].resultCount, 5, "the result count lands")
    Check.that(!rows.searches[0].isRunning, "and the row stops spinning")
    Check.equal(rows.searchesCompleted, 1, "now counted as done")

    // A dropped `search_started` must not lose the search entirely.
    apply(
        #"""
        {"type":"research.search_completed",
         "data":{"index":2,"query":"late","result_count":1}}
        """#,
        to: &rows
    )
    Check.equal(rows.searches.count, 2, "a completion with no start still shows")
    Check.equal(rows.searches.last?.index, 2, "ordered by index, not arrival")

    Check.section("A new question clears the last answer")
    var reused = SessionModel()
    apply(#"{"type":"research.answer_delta","data":{"text":"old answer"}}"#, to: &reused)
    apply(
        #"{"type":"research.search_started","data":{"index":0,"query":"old"}}"#, to: &reused
    )
    apply(
        #"{"type":"research.failed","data":{"code":"x","message":"boom"}}"#, to: &reused
    )
    Check.that(reused.failure != nil, "a failure is recorded")
    apply(#"{"type":"research.started","data":{"query":"new"}}"#, to: &reused)
    Check.equal(reused.answer, "", "the previous answer is cleared")
    Check.equal(reused.searches.count, 0, "and so are its searches")
    Check.that(reused.failure == nil, "a stale failure cannot sit under a fresh run")

    Check.section("Client over a scripted channel")
    do {
        let channel = ScriptedChannel(events: [
            #"{"type":"session.state_changed","data":{"state":"LISTENING"}}"#,
            #"{"type":"asr.partial","data":{"text":"compare"}}"#,
            #"not json at all"#,                      // must not kill the session
            #"{"type":"asr.final","data":{"text":"compare Kyutai and Nebius"}}"#,
            #"{"type":"session.state_changed","data":{"state":"REVIEW"}}"#,
        ])
        let client = ServiceClient(channel: channel)

        try? await client.send(.startRecording)
        try? await client.send(audioFrame: Data(repeating: 0, count: 3840))
        try? await client.send(.stopRecording)
        // GO carries the *edited* text, which is the whole point of the review step.
        // Sent before draining, because a clean close now ends the session.
        try? await client.send(.submit(query: "compare Kyutai, Nebius and Tavily"))

        let seen = Counter()
        try? await client.run { _, _, _ in seen.increment() }

        let final = await client.currentModel
        Check.equal(seen.count, 4, "one callback per decodable event; the junk frame is skipped")
        Check.equal(final.transcript, "compare Kyutai and Nebius", "the model reflects the run")
        Check.equal(final.state, .review, "and ends in REVIEW")
        Check.equal(channel.sentText.count, 3, "three commands went out")
        Check.equal(channel.sentText.first, #"{"type":"recording.start"}"#, "start first")
        Check.equal(channel.sentBinary.count, 1, "the audio frame went as binary")
        Check.equal(channel.sentBinary.first?.count, 3840, "one whole frame, unwrapped")
        Check.that(
            channel.sentText.last?.contains("Tavily") == true,
            "submit sends what the user approved, not the ASR's text"
        )
    }

    Check.section("A dead socket stops the sender")
    do {
        // The reported bug: with another client holding the model, the reader failed
        // with `.busy`, printed it, and the sender went on issuing commands into a
        // socket that was already gone — so the run looked like it had done something.
        let refused = ScriptedChannel(events: [], failWith: .busy)
        let client = ServiceClient(channel: refused)

        var readerError: ServiceClientError?
        do {
            try await client.run { _, _, _ in }
        } catch let error as ServiceClientError {
            readerError = error
        } catch {}
        Check.equal(readerError, .busy, "the reader surfaces the refusal")

        var sendError: ServiceClientError?
        do {
            try await client.send(.submit(query: "should never reach the wire"))
        } catch let error as ServiceClientError {
            sendError = error
        } catch {}
        Check.equal(sendError, .busy, "a later send fails with the reason the session ended")
        Check.equal(refused.sentText.count, 0, "and nothing was written to the dead socket")
        Check.that(refused.closed, "the channel was closed")

        let ended = await client.endedBecause
        Check.equal(ended, .busy, "the client remembers why it ended")
    }

    Check.section("A clean close also ends the session")
    do {
        // Not an error, but still the end: a sender that keeps going writes into nothing.
        let finished = ScriptedChannel(events: [
            #"{"type":"session.state_changed","data":{"state":"COMPLETED"}}"#,
        ])
        let client = ServiceClient(channel: finished)
        try? await client.run { _, _, _ in }

        var sendError: ServiceClientError?
        do {
            try await client.send(.reset)
        } catch let error as ServiceClientError {
            sendError = error
        } catch {}
        Check.equal(sendError, .notConnected, "sending after a clean close is refused")
    }

    Check.section("Repeated partials report no change")
    do {
        // Streaming ASR re-emits the same text many times — around thirty in one short
        // utterance. Redrawing each is a flicker in the overlay and a wasted SwiftUI pass.
        var model = SessionModel()
        let partial = #"{"type":"asr.partial","data":{"text":"compare Kyutai"}}"#
        guard let event = try? ServiceEvent.decode(from: Data(partial.utf8)) else {
            Check.that(false, "the partial decodes")
            return
        }
        Check.that(model.apply(event), "the first partial is a change")
        Check.that(!model.apply(event), "an identical partial is not")
        Check.that(!model.apply(event), "and still is not, however many arrive")

        let moved = #"{"type":"asr.partial","data":{"text":"compare Kyutai and"}}"#
        if let next = try? ServiceEvent.decode(from: Data(moved.utf8)) {
            Check.that(model.apply(next), "new text is a change again")
        }

        // The final carries the same text as the last partial but flips the editable
        // flag, so it must not be mistaken for a repeat.
        let final = #"{"type":"asr.final","data":{"text":"compare Kyutai and"}}"#
        if let done = try? ServiceEvent.decode(from: Data(final.utf8)) {
            Check.that(model.apply(done), "a final with identical text still changes state")
            Check.that(model.transcriptIsFinal, "and marks the transcript editable")
        }

        // An event that changes nothing still reaches the callback — the view layer
        // decides — but reports honestly.
        var other = SessionModel()
        if let synth = try? ServiceEvent.decode(
            from: Data(#"{"type":"research.synthesizing","data":{"source_count":5}}"#.utf8)
        ) {
            Check.that(!other.apply(synth), "synthesizing stores nothing")
        }
        if let state = try? ServiceEvent.decode(
            from: Data(#"{"type":"session.state_changed","data":{"state":"IDLE"}}"#.utf8)
        ) {
            Check.that(!other.apply(state), "a state change to the state already held is not one")
        }
        if let listening = try? ServiceEvent.decode(
            from: Data(#"{"type":"session.state_changed","data":{"state":"LISTENING"}}"#.utf8)
        ) {
            Check.that(other.apply(listening), "a real state change is")
        }
    }
}
