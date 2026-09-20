#if os(macOS)
import AppKit
import Combine
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
        // `label:` rather than `systemImage:`: the convenience initialiser cannot show
        // a bundled image, and bundled artwork is the point — see MenuBarIcon.swift.
        MenuBarExtra {
            MenuContent(delegate: delegate)
        } label: {
            MenuBarLabel(isRecording: delegate.isRecording)
        }
    }
}

/// Owns the store, the hotkey and the panel.
///
/// An `NSPanel` rather than a SwiftUI window: the overlay has to appear over whatever the
/// user is doing without taking focus from it, which is what a non-activating panel is
/// for. Driving a `MenuBarExtra`'s own window programmatically has no supported API.
final class AppDelegate: NSObject, NSApplicationDelegate, ObservableObject {
    /// Republished from the store rather than set by hand at each call site.
    ///
    /// `MenuBarExtra`'s label observes this object, not the store, so the flag has to
    /// live here — but assigning it manually after every toggle would go stale the first
    /// time something else changed it, and the overlay's own Stop button is exactly that.
    @Published var isRecording = false
    private(set) var store: SessionStore!
    private var hotKey: GlobalHotKey?
    private var panel: NSPanel?
    private var subscriptions = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Loopback only, by construction: this app streams raw audio, and PLAN §2 says
        // that never leaves the Mac.
        guard let endpoint = try? ServiceEndpoint() else {
            NSApp.terminate(nil)
            return
        }
        store = SessionStore(endpoint: endpoint)
        store.startHealthPolling()
        store.recordingChanged.assign(to: &$isRecording)
        store.$model
            .map { $0.state.isResearching || $0.completion != nil }
            .removeDuplicates()
            .sink { [weak self] showingAnswer in
                guard showingAnswer, let panel = self?.panel else { return }
                var frame = panel.frame
                let height = max(frame.height, 520)
                frame.origin.y -= height - frame.height
                frame.size = NSSize(width: max(frame.width, 640), height: height)
                panel.setFrame(frame, display: true, animate: true)
            }
            .store(in: &subscriptions)

        hotKey = GlobalHotKey { [weak self] in
            Task { @MainActor in
                guard let self else { return }
                self.showPanel()
                await self.store.toggleRecording()
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
            contentRect: NSRect(x: 0, y: 0, width: 680, height: 560),
            styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        created.title = "Voice Research"
        created.contentView = hosting
        created.isFloatingPanel = true
        created.level = .floating
        created.hidesOnDeactivate = false
        created.minSize = NSSize(width: 380, height: 200)
        // `.resizable` alone only lets the *frame* move: the content has to stop pinning
        // itself to a fixed width too, which is why OverlayView uses minWidth.
        //
        // Autosave persists the size and position across launches — one line, and the
        // name is the key it is stored under, so changing it forgets the old frame.
        created.setFrameAutosaveName("VNROverlay")
        if created.frame.origin == .zero { created.center() }
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
        Button(delegate.isRecording ? "Stop recording" : "Record (⌃⌥Space)") {
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
