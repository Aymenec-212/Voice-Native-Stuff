import Foundation

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// The socket, behind a protocol.
///
/// Splitting here is the same move that made `MlxEngine` testable: everything around the
/// transport is checkable off-device, and only the transport itself needs a Mac. A fake
/// conforming to this drives `ServiceClient` through a whole session inside
/// `VNRKitCheck`, on Linux, with no service running.
public protocol WebSocketChannel: AnyObject, Sendable {
    func send(text: String) async throws
    func send(binary: Data) async throws
    /// One frame. Returns nil when the peer closed cleanly.
    func receive() async throws -> WebSocketFrame?
    func close()
}

public enum WebSocketFrame: Equatable, Sendable {
    case text(String)
    case binary(Data)
}

public enum ServiceClientError: Error, Equatable {
    /// The service refuses a second connection: one user, one resident model.
    case busy
    case closed(code: Int, reason: String)
    case notConnected
}

/// Drives a session over a channel and folds every event into a `SessionModel`.
///
/// Deliberately has no opinion about *what* the events mean — that is `SessionModel`'s
/// job, and this type's job is to move bytes and hand them over.
public actor ServiceClient {
    /// The close code the service sends when another session already holds the model.
    public static let busyCode = 4409

    private let channel: WebSocketChannel
    private var model = SessionModel()
    /// Set the moment the socket is known to be gone, so nothing else is written to it.
    private var ended: ServiceClientError?

    public init(channel: WebSocketChannel) {
        self.channel = channel
    }

    public var currentModel: SessionModel { model }

    /// Why the session ended, or nil while it is live.
    public var endedBecause: ServiceClientError? { ended }

    /// Ends the session and closes the channel. Idempotent — the first reason wins,
    /// because it is the one that explains the others.
    private func end(_ reason: ServiceClientError) {
        guard ended == nil else { return }
        ended = reason
        channel.close()
    }

    public func send(_ command: ClientCommand) async throws {
        try guardOpen()
        try await channel.send(text: command.jsonText())
    }

    /// Refuses to write to a socket that is already gone.
    ///
    /// Without this a rejected connection was silent in the worst way: the reader failed
    /// with `.busy`, printed that, and the scripted sender carried on issuing commands
    /// into a dead socket — so the run looked like it had done something.
    private func guardOpen() throws {
        if let ended { throw ended }
    }

    /// One frame of PCM, exactly as `PCMFramer` produced it.
    ///
    /// Binary, with no envelope: `src/vnr/service.py` reads `message["bytes"]` as the
    /// `audio.frame` command. Wrapping it in JSON would base64 it for no reason, on the
    /// hottest path in the app.
    public func send(audioFrame: Data) async throws {
        try guardOpen()
        try await channel.send(binary: audioFrame)
    }

    /// Reads until the socket closes, applying every event and calling *onEvent* after
    /// each one with the updated model.
    ///
    /// The model is handed over already updated rather than left for the caller to
    /// update, so there is no ordering in which a UI can render a stale one.
    public func run(
        onEvent: @Sendable (ServiceEvent, SessionModel, Bool) -> Void
    ) async throws {
        do {
            try await readUntilClosed(onEvent: onEvent)
        } catch let error as ServiceClientError {
            end(error)
            throw error
        } catch {
            end(.closed(code: 0, reason: error.localizedDescription))
            throw error
        }
        // A clean close is still the end of the session, and a sender that keeps going
        // after it is writing into nothing.
        end(.notConnected)
    }

    private func readUntilClosed(
        onEvent: @Sendable (ServiceEvent, SessionModel, Bool) -> Void
    ) async throws {
        while let frame = try await channel.receive() {
            guard case let .text(json) = frame else {
                // The service never sends binary. Ignore rather than fail: an unexpected
                // frame is not a reason to drop a working session.
                continue
            }
            let event: ServiceEvent
            do {
                event = try ServiceEvent.decode(from: Data(json.utf8))
            } catch {
                // A frame we cannot parse is not a frame we should die on — the same
                // tolerance `ServiceEvent` applies to unknown types.
                continue
            }
            // `changed` is passed on rather than used to suppress the callback: an
            // event can matter to a consumer without altering the model (synthesizing
            // says nothing new but means "show the spinner"). The caller decides.
            let changed = model.apply(event)
            onEvent(event, model, changed)
        }
    }

    public func close() {
        end(.notConnected)
    }
}

#if os(macOS)
/// `URLSessionWebSocketTask` behind the protocol.
///
/// macOS-only by guard rather than by accident: `URLSessionWebSocketTask` is not reliably
/// present in swift-corelibs-foundation, and CI builds this package on Linux. The
/// protocol above is what keeps the rest of the client checkable there.
public final class URLSessionChannel: WebSocketChannel, @unchecked Sendable {
    private let task: URLSessionWebSocketTask
    private let session: URLSession

    public init(endpoint: ServiceEndpoint) {
        session = URLSession(configuration: .ephemeral)
        task = session.webSocketTask(with: endpoint.websocketURL)
        task.resume()
    }

    public func send(text: String) async throws {
        try await task.send(.string(text))
    }

    public func send(binary: Data) async throws {
        try await task.send(.data(binary))
    }

    public func receive() async throws -> WebSocketFrame? {
        do {
            switch try await task.receive() {
            case .string(let text): return .text(text)
            case .data(let data): return .binary(data)
            @unknown default: return nil
            }
        } catch {
            // A closed socket surfaces as an error, and the close code is the only way to
            // tell "another session holds the model" from "the service went away".
            let code = task.closeCode.rawValue
            if code == ServiceClient.busyCode { throw ServiceClientError.busy }
            if code != 0 {
                let reason = task.closeReason.map { String(decoding: $0, as: UTF8.self) } ?? ""
                throw ServiceClientError.closed(code: code, reason: reason)
            }
            throw error
        }
    }

    public func close() {
        task.cancel(with: .goingAway, reason: nil)
        session.invalidateAndCancel()
    }
}

/// `GET /health`, which gates the record button.
public func fetchHealth(_ endpoint: ServiceEndpoint) async -> ServiceHealth.Gate {
    var request = URLRequest(url: endpoint.healthURL)
    request.timeoutInterval = 2
    do {
        let (data, _) = try await URLSession(configuration: .ephemeral).data(for: request)
        return try JSONDecoder().decode(ServiceHealth.self, from: data).gate
    } catch {
        // Nothing answered, or answered with something else. Either way the button stays
        // off and the message names the fix.
        return .unreachable
    }
}
#endif
