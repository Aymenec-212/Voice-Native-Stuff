// swift-tools-version: 5.9
import PackageDescription

// No test target on purpose. XCTest on Darwin lives in the Xcode Platform directory, not
// in the SDK, so a Command Line Tools install cannot run `swift test` at all — and this
// project is developed without Xcode. Assertions live in the VNRKitCheck executable
// instead: it runs anywhere `swift run` does, including Linux CI, and exits non-zero on
// failure. Verification for this package must be reachable from `swift build`,
// `swift run` or a script.
let package = Package(
    name: "VoiceNativeResearch",
    // MenuBarExtra (the eventual activation surface) needs macOS 13.
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "VNRKit", targets: ["VNRKit"]),
        .executable(name: "VNRKitCheck", targets: ["VNRKitCheck"]),
        .executable(name: "VNRProbe", targets: ["VNRProbe"]),
        .executable(name: "VNRCapture", targets: ["VNRCapture"]),
        .executable(name: "VNRClient", targets: ["VNRClient"]),
        .executable(name: "VNRApp", targets: ["VNRApp"]),
    ],
    targets: [
        // Pure Foundation: the event vocabulary and the audio framing rules. No
        // AVFoundation and no UI, so it builds and runs on Linux too.
        .target(name: "VNRKit"),

        // The test suite, as a runnable program.
        .executableTarget(name: "VNRKitCheck", dependencies: ["VNRKit"]),

        // macOS-only bodies, guarded so the package still builds on Linux CI.
        .executableTarget(name: "VNRProbe"),
        .executableTarget(name: "VNRCapture", dependencies: ["VNRKit"]),
        // The WebSocket transport is URLSession-only; everything it carries is in VNRKit
        // and is checked on Linux.
        .executableTarget(name: "VNRClient", dependencies: ["VNRKit"]),
        // The menu-bar app: SwiftUI, AppKit, AVFoundation and a Carbon hotkey, so
        // macOS-only. Everything it decides lives in VNRKit and is checked on Linux.
        .executableTarget(name: "VNRApp", dependencies: ["VNRKit"]),
    ]
)
