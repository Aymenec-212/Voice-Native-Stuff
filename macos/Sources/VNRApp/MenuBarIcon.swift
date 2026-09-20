#if os(macOS)
import AppKit
import SwiftUI

/// What the menu bar shows, and how to replace it with your own artwork.
///
/// Two sources, in priority order:
///
/// 1. **A bundled image.** Drop a file named `MenuBarIcon` (`.pdf`, `.png` or `.svg`)
///    into `macos/Resources/` and `make-app.sh` copies it into the bundle. A PDF is the
///    better choice — it stays sharp on every display scale.
/// 2. **An SF Symbol**, named by `VNR_MENUBAR_SYMBOL` or the default below.
///
/// The image is marked as a **template**, which is what makes a menu-bar icon behave:
/// macOS then recolours it for light and dark menu bars and for the highlighted state.
/// A non-template image stays whatever colour it was drawn in and looks wrong in at
/// least one of those three.
///
/// Artwork wants to be roughly 18×18 pt, monochrome, with transparency doing the work —
/// the template rule means only the alpha channel is used.
enum MenuBarIcon {
    /// `mic.fill` reads as "this records" at 18pt, where a bare `waveform` reads as
    /// "audio, somehow". Override with `VNR_MENUBAR_SYMBOL`.
    static let defaultSymbol = "mic.fill"
    static let recordingSymbol = "mic.circle.fill"
    static let bundledName = "MenuBarIcon"

    static var symbol: String {
        ProcessInfo.processInfo.environment["VNR_MENUBAR_SYMBOL"] ?? defaultSymbol
    }

    /// The bundled image, already marked as a template, or nil if none was shipped.
    static var bundledImage: NSImage? {
        guard let image = Bundle.main.image(forResource: bundledName) else { return nil }
        image.isTemplate = true
        image.size = NSSize(width: 18, height: 18)
        return image
    }
}

/// The label, which prefers your artwork and falls back to a symbol.
///
/// `MenuBarExtra(content:label:)` rather than the `systemImage:` convenience, because the
/// convenience cannot show a bundled image at all.
struct MenuBarLabel: View {
    /// Shown while recording, so the menu bar says whether the mic is live even when the
    /// overlay is behind something.
    var isRecording: Bool

    var body: some View {
        if let image = MenuBarIcon.bundledImage {
            Image(nsImage: image)
                .opacity(isRecording ? 1.0 : 0.85)
        } else {
            Image(systemName: isRecording ? MenuBarIcon.recordingSymbol : MenuBarIcon.symbol)
        }
    }
}
#endif
