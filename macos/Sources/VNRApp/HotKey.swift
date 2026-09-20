#if os(macOS)
import Carbon.HIToolbox
import Foundation

/// A system-wide hotkey, registered through Carbon.
///
/// Carbon rather than `NSEvent.addGlobalMonitorForEvents` on purpose:
/// `RegisterEventHotKey` needs **no** Accessibility or Input Monitoring grant, while a
/// global event monitor does. Slice 1 cost an afternoon to a permission that failed
/// silently, so a shortcut that quietly needs a second TCC grant — one macOS does not
/// prompt for, and which reads as "the shortcut just doesn't work" — is not a trade worth
/// making for a nicer API.
///
/// The API is old and C-shaped, and it is still the supported way to do this.
public final class GlobalHotKey {
    private var reference: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let action: () -> Void
    private static var registry: [UInt32: GlobalHotKey] = [:]
    private static var nextID: UInt32 = 1

    private let identifier: UInt32

    /// Default: ⌃⌥Space. Chosen to avoid the obvious collisions — ⌘Space is Spotlight,
    /// ⌥Space is a text input switcher on some layouts.
    public init?(
        keyCode: UInt32 = UInt32(kVK_Space),
        modifiers: UInt32 = UInt32(controlKey | optionKey),
        action: @escaping () -> Void
    ) {
        self.action = action
        identifier = Self.nextID
        Self.nextID += 1
        Self.registry[identifier] = self

        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        var installedHandler: EventHandlerRef?
        let installed = InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, _ -> OSStatus in
                var hotKeyID = EventHotKeyID()
                GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &hotKeyID
                )
                GlobalHotKey.registry[hotKeyID.id]?.action()
                return noErr
            },
            1,
            &eventType,
            nil,
            &installedHandler
        )
        guard installed == noErr else { return nil }
        handler = installedHandler

        let hotKeyID = EventHotKeyID(signature: OSType(0x564E_5231), id: identifier)
        let registered = RegisterEventHotKey(
            keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &reference
        )
        guard registered == noErr else { return nil }
    }

    deinit {
        if let reference { UnregisterEventHotKey(reference) }
        if let handler { RemoveEventHandler(handler) }
        Self.registry[identifier] = nil
    }
}
#endif
