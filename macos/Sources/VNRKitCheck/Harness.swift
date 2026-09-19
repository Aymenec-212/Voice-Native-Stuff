import Foundation

/// A three-function stand-in for XCTest.
///
/// `swift test` cannot run here: XCTest on Darwin ships inside Xcode's Platform directory
/// rather than the SDK, so a Command Line Tools install has no way to run it. Rather than
/// require a 15 GB Xcode for a few hundred assertions, the checks are an ordinary program
/// — which also means they run unchanged on Linux CI.
enum Check {
    // Plain statics: `nonisolated(unsafe)` needs Swift 5.10+, and this package targets
    // 5.9 so it builds on whatever toolchain is installed. Single-threaded by design.
    static var failures: [String] = []
    static var passed = 0
    static var currentSection = ""

    static func section(_ name: String) {
        currentSection = name
        print("\n\(name)")
    }

    static func that(_ condition: Bool, _ description: String, line: UInt = #line) {
        if condition {
            passed += 1
            print("  ok   \(description)")
        } else {
            let failure = "\(currentSection) — \(description) (line \(line))"
            failures.append(failure)
            print("  FAIL \(description)   [line \(line)]")
        }
    }

    static func equal<T: Equatable>(
        _ actual: T, _ expected: T, _ description: String, line: UInt = #line
    ) {
        if actual == expected {
            passed += 1
            print("  ok   \(description)")
        } else {
            let failure = "\(currentSection) — \(description): got \(actual), want \(expected)"
            failures.append(failure + " (line \(line))")
            print("  FAIL \(description)")
            print("       got:  \(actual)")
            print("       want: \(expected)")
        }
    }

    /// Exit code is the point: this is what CI and `swift run` read.
    static func finish() -> Never {
        print("\n" + String(repeating: "-", count: 60))
        if failures.isEmpty {
            print("\(passed) checks passed")
            exit(0)
        }
        print("\(passed) passed, \(failures.count) FAILED\n")
        for failure in failures { print("  • \(failure)") }
        exit(1)
    }
}

/// Locates `macos/Fixtures/events.json`.
///
/// Resolved from `#filePath` rather than `Bundle.module`: an executable's resource bundle
/// sits next to the binary and moves with the build directory, while the source layout is
/// fixed. An explicit path may be passed as the first argument, or in `VNR_FIXTURES`.
func fixturesURL() -> URL {
    if let argument = CommandLine.arguments.dropFirst().first {
        return URL(fileURLWithPath: argument)
    }
    if let fromEnvironment = ProcessInfo.processInfo.environment["VNR_FIXTURES"] {
        return URL(fileURLWithPath: fromEnvironment)
    }
    // .../macos/Sources/VNRKitCheck/Harness.swift → .../macos
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    return packageRoot.appendingPathComponent("Fixtures/events.json")
}

struct Fixture: Decodable {
    let label: String
    let json: String
}

func loadFixtures() -> [Fixture] {
    let url = fixturesURL()
    guard let data = try? Data(contentsOf: url) else {
        print("FAIL: no fixtures at \(url.path)")
        print("  Generate them with:")
        print("    VNR_UPDATE_FIXTURES=1 uv run pytest tests/test_event_contract.py")
        exit(2)
    }
    guard let fixtures = try? JSONDecoder().decode([Fixture].self, from: data) else {
        print("FAIL: \(url.path) is not a fixture list")
        exit(2)
    }
    return fixtures
}
