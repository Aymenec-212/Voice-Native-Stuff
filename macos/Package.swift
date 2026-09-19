// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "VoiceNativeResearch",
    // MenuBarExtra (the eventual activation surface) needs macOS 13.
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "VNRKit", targets: ["VNRKit"]),
        .executable(name: "VNRProbe", targets: ["VNRProbe"]),
    ],
    targets: [
        // Pure logic: the event vocabulary the service speaks. No AVFoundation, no UI,
        // so it stays testable with `swift test` and nothing else.
        .target(name: "VNRKit"),

        // Deliberately does NOT depend on VNRKit: the microphone-permission check must
        // still build and run if anything in the kit is broken.
        .executableTarget(name: "VNRProbe"),

        .testTarget(
            name: "VNRKitTests",
            dependencies: ["VNRKit"],
            resources: [.copy("Fixtures")]
        ),
    ]
)
