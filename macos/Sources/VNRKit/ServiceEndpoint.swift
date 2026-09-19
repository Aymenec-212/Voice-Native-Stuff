import Foundation

/// Where the local service lives, and the refusal to talk to anything else.
///
/// PLAN §2 and §19: raw audio never leaves the Mac. The service binds 127.0.0.1 and the
/// client must be equally unwilling to reach anywhere else — a host typed into a config
/// file is exactly how "local only" quietly stops being true. So this type is the single
/// place a URL is built, and a non-loopback host is rejected here rather than trusted.
public struct ServiceEndpoint: Equatable, Sendable {
    public let host: String
    public let port: Int

    /// Hosts that are unambiguously this machine. A name that merely *resolves* to
    /// loopback today is not on the list: DNS is not a security boundary.
    public static let loopbackHosts: Set<String> = ["127.0.0.1", "localhost", "::1", "[::1]"]

    public enum Failure: Error, Equatable {
        case notLoopback(String)
        case portOutOfRange(Int)
    }

    public init(host: String = "127.0.0.1", port: Int = 8765) throws {
        let normalised = host.trimmingCharacters(in: .whitespaces).lowercased()
        guard Self.loopbackHosts.contains(normalised) else {
            throw Failure.notLoopback(host)
        }
        guard (1...65_535).contains(port) else {
            throw Failure.portOutOfRange(port)
        }
        self.host = normalised
        self.port = port
    }

    /// Bracketed for IPv6, bare otherwise — `ws://::1:8765/ws` is not a URL.
    private var authority: String {
        host.contains(":") && !host.hasPrefix("[") ? "[\(host)]:\(port)" : "\(host):\(port)"
    }

    public var websocketURL: URL { URL(string: "ws://\(authority)/ws")! }
    public var healthURL: URL { URL(string: "http://\(authority)/health")! }
}
