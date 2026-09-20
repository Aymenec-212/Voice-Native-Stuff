#if os(macOS)
import AppKit
import SwiftUI
import VNRKit

// Milestone 4, slice 4: the menu-bar app.
//
//     uv run vnr-service                        # terminal 1, from the repo root
//     cd macos && PRODUCT=VNRApp ./scripts/make-app.sh run
//
// ⌃⌥Space starts and stops recording from anywhere; the panel shows the live transcript,
// the editable review field, search progress and the cited answer.
//
// The bundle matters as much as it did in slice 1: a bare SwiftPM binary has no
// NSMicrophoneUsageDescription, and TCC answers that with digital silence or a kill.
// Always launch through make-app.sh.

@main
struct VoiceNativeResearchApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra("Voice Research", systemImage: delegate.menuBarSymbol) {
            MenuContent(delegate: delegate)
        }
    }
}

/// Owns the store, the hotkey and the panel.
///
/// An `NSPanel` rather than a SwiftUI window: the overlay has to appear over whatever the
/// user is doing without taking focus from it, which is what a non-activating panel is
/// for. Driving a `MenuBarExtra`'s own window programmatically has no supported API.
final class AppDelegate: NSObject, NSApplicationDelegate, ObservableObject {
    @Published var menuBarSymbol = "waveform"
    private(set) var store: SessionStore!
    private var hotKey: GlobalHotKey?
    private var panel: NSPanel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Loopback only, by construction: this app streams raw audio, and PLAN §2 says
        // that never leaves the Mac.
        guard let endpoint = try? ServiceEndpoint() else {
            NSApp.terminate(nil)
            return
        }
        store = SessionStore(endpoint: endpoint)
        store.startHealthPolling()

        hotKey = GlobalHotKey { [weak self] in
            Task { @MainActor in
                guard let self else { return }
                self.showPanel()
                await self.store.toggleRecording()
                self.menuBarSymbol = self.store.isRecording ? "waveform.circle.fill" : "waveform"
            }
        }
    }

    @MainActor
    func showPanel() {
        if let panel {
            panel.orderFrontRegardless()
            return
        }
        let hosting = NSHostingView(rootView: OverlayView(store: store))
        let created = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 240),
            styleMask: [.titled, .closable, .utilityWindow, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        created.title = "Voice Research"
        created.contentView = hosting
        created.isFloatingPanel = true
        created.level = .floating
        created.hidesOnDeactivate = false
        created.center()
        created.orderFrontRegardless()
        panel = created
    }
}

/// The menu itself stays small: the panel is where the work happens.
struct MenuContent: View {
    @ObservedObject var delegate: AppDelegate

    var body: some View {
        Button("Show overlay") {
            Task { @MainActor in delegate.showPanel() }
        }
        Button(delegate.store?.isRecording == true ? "Stop recording" : "Record (⌃⌥Space)") {
            Task { @MainActor in
                delegate.showPanel()
                await delegate.store?.toggleRecording()
            }
        }
        .disabled(delegate.store == nil)
        Divider()
        Button("Quit") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}
#endif
