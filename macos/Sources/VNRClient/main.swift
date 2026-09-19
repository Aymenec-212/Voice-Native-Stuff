#if os(macOS)
import Foundation
import VNRKit

// Milestone 4, slice 3: talk to the local service and render purely from its events.
//
//     uv run vnr-service --engine mock          # terminal 1, from the repo root
//     cd macos && swift run VNRClient           # terminal 2
//
// Prints the event stream as it arrives. Nothing here decides what happens next: the
// service owns session state, and this only draws what it is told (PLAN §19). That is
// what makes it a real test of the contract rather than a re-implementation of it.
//
// A scripted session drives the whole loop without a microphone or a UI:
//
//     swift run VNRClient --file /tmp/capture.wav --submit "compare Kyutai and Nebius"
//
// which sends recording.start, streams the WAV as binary frames exactly as the UI will,
// sends recording.stop, and then submits the text — the same path the menu-bar app takes.

let arguments = Array(CommandLine.arguments.dropFirst())

func option(_ name: String) -> String? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else {
        return nil
    }
    return arguments[index + 1]
}

let host = option("--host") ?? "127.0.0.1"
let port = Int(option("--port") ?? "8765") ?? 8765
let wavPath = option("--file")
let submitText = option("--submit")
let listenSeconds = Double(option("--seconds") ?? "0") ?? 0

let endpoint: ServiceEndpoint
do {
    endpoint = try ServiceEndpoint(host: host, port: port)
} catch ServiceEndpoint.Failure.notLoopback(let bad) {
    // Not a validation nicety: PLAN §2 says raw audio never leaves the Mac, and this
    // client streams raw audio.
    print("refusing to connect to \(bad): the service is loopback-only, and so is this.")
    exit(2)
} catch {
    print("bad endpoint: \(error)")
    exit(2)
}

// --- the gate ------------------------------------------------------------------------
// Readiness is asked for before anything else. The model takes seconds to load, and a
// session opened before then produces an utterance the service cannot transcribe.
let gate = await fetchHealth(endpoint)
print("health: \(gate.explanation)")
guard gate.allowsRecording else {
    exit(gate == .unreachable ? 3 : 4)
}

// --- audio, if a file was given -------------------------------------------------------
func framesFromWav(_ path: String) -> [Data]? {
    guard let raw = FileManager.default.contents(atPath: path), raw.count > 44 else {
        print("could not read \(path)")
        return nil
    }
    // Minimal WAV reader: this only ever consumes files the project itself wrote, and
    // the spike is the authority on whether a file is acceptable.
    func u32(_ offset: Int) -> UInt32 {
        raw[offset..<offset + 4].reversed().reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
    }
    let rate = Int(u32(24))
    guard rate == Int(AudioFormat.sampleRate) else {
        print("\(path) is \(rate) Hz; the service expects \(Int(AudioFormat.sampleRate)).")
        print("Convert it: ffmpeg -i \(path) -ar 24000 -ac 1 -sample_fmt s16 /tmp/ok.wav")
        return nil
    }
    let payload = raw.dropFirst(44)
    var samples = [Int16](repeating: 0, count: payload.count / 2)
    for index in 0..<samples.count {
        let low = UInt16(payload[payload.startIndex + index * 2])
        let high = UInt16(payload[payload.startIndex + index * 2 + 1])
        samples[index] = Int16(bitPattern: low | (high << 8))
    }
    // The same framer the capture tool uses, so the wire sees exactly what it will in
    // production rather than something this file invented.
    var framer = PCMFramer()
    return framer.push(samples)
}

var frames: [Data] = []
if let wavPath {
    guard let read = framesFromWav(wavPath) else { exit(5) }
    frames = read
    print("loaded \(frames.count) frames from \(wavPath)")
}

// --- the session ----------------------------------------------------------------------
let channel = URLSessionChannel(endpoint: endpoint)
let client = ServiceClient(channel: channel)

print("connected to \(endpoint.websocketURL)")
print("")

let reader = Task {
    do {
        try await client.run { event, model in
            render(event, model)
        }
        print("\n— the service closed the connection —")
    } catch ServiceClientError.busy {
        print("another session already holds the model; close the other client first.")
    } catch {
        print("\n— disconnected: \(error) —")
    }
}

/// Everything printed comes from the event, or from the model the event just produced.
func render(_ event: ServiceEvent, _ model: SessionModel) {
    switch event.kind {
    case .stateChanged(let state, let ready):
        let readiness = ready.map { $0 ? " (ready)" : " (not ready)" } ?? ""
        print("[state] \(state)\(readiness)")
    case .asrPartial:
        print("[asr…] \(model.transcript)")
    case .asrFinal:
        print("[asr!] \(model.transcript)")
        // PLAN §7: this is the step the product is built around. Say so, so a run that
        // silently skips the review step is obvious rather than plausible.
        print("       ^ the text you would edit before pressing GO")
    case .asrError(let code, let message):
        print("[asr✗] \(code): \(message)")
    case .researchStarted(let query):
        print("[go]   \(query)")
    case .searchStarted(let index, let query, let depth):
        print("[\(index)] searching \(depth.map { "(\($0)) " } ?? "")\(query)…")
    case .searchCompleted(let index, _, let count, let error):
        if let error {
            print("[\(index)] failed: \(error)")
        } else {
            let progress = "\(model.searchesCompleted)/\(model.searches.count) done"
            print("[\(index)] \(count) results  (\(progress))")
        }
    case .synthesizing(let sources):
        print("[write] synthesising from \(sources) sources…")
    case .answerDelta:
        // Streamed, so no newline: this is the answer appearing as it is written.
        print(".", terminator: "")
        fflush(stdout)
    case .researchCompleted(let done):
        print("\n\n\(model.answer)\n")
        if !model.citations.isEmpty {
            print("Sources")
            for citation in model.citations {
                print("  [\(citation.number)] \(citation.title)\n      \(citation.url)")
            }
        }
        // The renumbering in src/vnr/research/citations.py is what makes an invented URL
        // structurally impossible, so a mismatch here is worth seeing.
        if !done.invalidCitationIDs.isEmpty {
            let dropped = done.invalidCitationIDs.joined(separator: ", ")
            print("  (dropped invalid citations: \(dropped))")
        }
        print("\n\(done.searches) searches, \(done.turns) turns", terminator: "")
        if let credits = done.tavilyCredits {
            print(", \(credits) Tavily credits", terminator: "")
        }
        if let ms = done.totalMilliseconds { print(", \(Int(ms)) ms", terminator: "") }
        print("")
    case .researchFailed(let code, let message):
        print("\n[fail] \(code): \(message)")
    case .researchCancelled:
        print("\n[cancelled]")
    case .unknown(let type):
        // Carried rather than dropped: a newer service is not a reason to fall over.
        print("[?]    ignoring unknown event \(type)")
    }
}

if !frames.isEmpty {
    try? await client.send(.startRecording)
    for frame in frames {
        try? await client.send(audioFrame: frame)
    }
    try? await client.send(.stopRecording)

    if let submitText {
        // Wait for the transcript to settle before submitting, so the run exercises the
        // real order: speak, review, approve.
        try? await Task.sleep(nanoseconds: 2_000_000_000)
        print("\n[submit] \(submitText)")
        try? await client.send(.submit(query: submitText))
    }
}

if listenSeconds > 0 {
    try? await Task.sleep(nanoseconds: UInt64(listenSeconds * 1_000_000_000))
} else {
    // The reader catches everything, so this task cannot fail — `value` suffices.
    _ = await reader.value
}

await client.close()
exit(0)

#else
import Foundation

// Linux CI builds every target. URLSessionWebSocketTask is not reliably available in
// swift-corelibs-foundation, so the transport is macOS-only — but everything it carries
// (ServiceEndpoint, ClientCommand, ServiceHealth, SessionModel, ServiceClient) is pure
// Foundation and is checked by VNRKitCheck on this platform.
print("VNRClient requires macOS — the WebSocket transport is URLSession-only.")
exit(0)
#endif
