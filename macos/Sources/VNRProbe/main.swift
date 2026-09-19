#if os(macOS)
import AVFoundation
import AppKit
import Foundation

// Milestone 4, slice 1. This asks for the microphone and reports exactly what macOS
// said. It captures no audio — that is the next slice, and it is deliberately not
// written until this prompt has been seen.
//
// The failure this exists to prevent: a bare SwiftPM executable has no Info.plist, so
// NSMicrophoneUsageDescription is absent. TCC then either hands the process digital
// silence or kills it outright, and neither looks like a permission problem from the
// inside. `scripts/make-app.sh` builds the bundle that makes the prompt possible; this
// probe proves the bundle is right before a single frame of audio is captured.

let logURL = FileManager.default.temporaryDirectory.appendingPathComponent("vnr-probe.log")
var transcript: [String] = []

func say(_ line: String) {
    print(line)
    transcript.append(line)
    try? transcript.joined(separator: "\n").appending("\n").write(
        to: logURL, atomically: true, encoding: .utf8
    )
}

func describe(_ status: AVAuthorizationStatus) -> String {
    switch status {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted (policy — a prompt will not help)"
    case .notDetermined: return "not determined (never asked)"
    @unknown default: return "unknown (\(status.rawValue))"
    }
}

// A real app identity, without a Dock icon — the same activation policy the menu-bar app
// will use. TCC attributes the request to this bundle rather than to the terminal.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let bundle = Bundle.main
let identifier = bundle.bundleIdentifier ?? ""
let usageDescription =
    bundle.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String ?? ""

say("Voice-Native Research — microphone permission probe")
say("  bundle path: \(bundle.bundleURL.path)")
say("  bundle id:   \(identifier.isEmpty ? "none" : identifier)")
say("  log:         \(logURL.path)")

// Check the usage string BEFORE asking. Requesting access without it does not fail
// gracefully — the system terminates the process, which reads as a mysterious crash.
guard !usageDescription.isEmpty else {
    say("")
    say("FAIL: NSMicrophoneUsageDescription is missing from Info.plist.")
    say("  Requesting microphone access without it makes macOS kill the process, so this")
    say("  probe stopped instead of asking.")
    say("  You are probably running the bare binary from .build/ rather than the bundle.")
    say("  Build and run the app with: ./scripts/make-app.sh run")
    exit(2)
}

guard !identifier.isEmpty else {
    say("")
    say("FAIL: no bundle identifier. TCC keys permission by bundle id, so an unbundled")
    say("  binary cannot hold a grant. Run ./scripts/make-app.sh run")
    exit(2)
}

say("  usage text:  \"\(usageDescription)\"")
say("")

let before = AVCaptureDevice.authorizationStatus(for: .audio)
say("Status before asking: \(describe(before))")

if before == .notDetermined {
    say("Requesting access — macOS should now show a permission dialog…")
} else {
    say("Already answered once, so no dialog will appear.")
    say("To see the prompt again: tccutil reset Microphone \(identifier)")
}

// requestAccess calls back on an arbitrary queue, so blocking the main thread is safe —
// and necessary, or the process exits before the user can answer.
let answered = DispatchSemaphore(value: 0)
var granted = false
AVCaptureDevice.requestAccess(for: .audio) { allowed in
    granted = allowed
    answered.signal()
}

if answered.wait(timeout: .now() + 120) == .timedOut {
    say("")
    say("FAIL: no answer within 120s. If no dialog appeared, check System Settings →")
    say("  Privacy & Security → Microphone for a stale entry, then:")
    say("  tccutil reset Microphone \(identifier)")
    exit(3)
}

let after = AVCaptureDevice.authorizationStatus(for: .audio)
say("")
say("Answer: \(granted ? "GRANTED" : "DENIED")")
say("Status after asking: \(describe(after))")
say("")

if granted {
    say("PASS — the bundle is correct and the app holds a microphone grant.")
    say("Audio capture is the next slice.")
    exit(0)
}

say("Not granted. Nothing is wrong with the bundle — the request reached TCC and was")
say("refused. Allow it in System Settings → Privacy & Security → Microphone, or reset")
say("with: tccutil reset Microphone \(identifier)")
exit(1)

#else
import Foundation

// Linux CI builds every target; TCC and AVFoundation are macOS-only.
print("VNRProbe requires macOS — microphone permission is a macOS concept.")
exit(0)
#endif
