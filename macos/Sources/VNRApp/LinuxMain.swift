#if !os(macOS)
import Foundation

/// Linux CI builds every target. This one is SwiftUI, AppKit and AVFoundation, so there
/// is nothing to build here — but the target still needs an entry point, and `@main` on a
/// type is the form that coexists with the SwiftUI `App` on the other side of the guard.
/// (`main.swift` cannot: top-level code and `@main` are mutually exclusive.)
@main
struct VNRAppLinuxStub {
    static func main() {
        print("VNRApp requires macOS — the UI is SwiftUI and the capture is AVFoundation.")
    }
}
#endif
