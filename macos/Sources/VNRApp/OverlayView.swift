#if os(macOS)
import SwiftUI
import VNRKit

/// The overlay: live transcript, the editable review field, progress, and the answer.
///
/// Every value shown comes from `SessionModel` or the draft. The view decides nothing
/// about the session — pressing GO sends a command and waits to be told what happened.
public struct OverlayView: View {
    @ObservedObject var store: SessionStore
    @FocusState private var fieldFocused: Bool
    @State private var reasoningExpanded = false

    public init(store: SessionStore) {
        self.store = store
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if let notice = store.notice {
                Label(notice, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
            content
        }
        .padding(16)
        // minWidth rather than width: with a fixed width the panel resizes and the
        // content stays pinned at 460pt, which looks like the resize did nothing.
        .frame(minWidth: 380, maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 8) {
            if store.isPreparing { ProgressView().controlSize(.small) }
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
            Text(statusText).font(.headline)
            Spacer()
            if store.model.state.isResearching {
                Button("Cancel") { Task { await store.cancelResearch() } }
                    .buttonStyle(.link)
            }
        }
    }

    /// Spelled out as `Color` throughout: `.secondary` in a `fill` is a different type
    /// from `.red`, and a ternary mixing them does not type-check.
    private var statusColor: Color {
        if store.isRecording { return .red }
        return store.gate.allowsRecording ? .green : Color.secondary
    }

    private var statusText: String {
        if store.isPreparing { return "Loading speech recognition… Wait until Listening to speak." }
        if store.isRecording { return "Listening…" }
        if !store.gate.allowsRecording && store.model.state == .idle { return store.gate.explanation }
        switch store.model.state {
        case .review: return "Check the text, then press GO"
        case .finalizingTranscript: return "Finishing the transcript…"
        case .synthesizing: return "Writing the answer…"
        case .completed: return "Done"
        default: return store.model.state.isResearching ? "Researching…" : "Ready"
        }
    }

    // MARK: - Body

    @ViewBuilder
    private var content: some View {
        if store.model.state.isResearching || store.model.completion != nil || store.model.failure?.duringResearch == true {
            researchView
        } else {
            reviewView
        }
    }

    /// PLAN §7. The transcript is editable, GO freezes what is in the field, and Cancel
    /// discards the utterance — it does not cancel research, because none is running.
    private var reviewView: some View {
        VStack(alignment: .leading, spacing: 10) {
            TextEditor(text: Binding(
                get: { store.draft.text },
                // Any keystroke marks the draft edited, after which the ASR stops
                // writing to it — so a final transcript arriving late cannot overwrite
                // the correction the user just made.
                set: { store.draft.edit(to: $0) }
            ))
            .font(.body)
            .frame(minHeight: 72)
            .overlay(alignment: .topLeading) {
                if store.draft.text.isEmpty {
                    Text(store.isRecording ? "Listening…" : "Press ⌃⌥Space and speak")
                        .foregroundStyle(.secondary)
                        .padding(.top, 8)
                        .padding(.leading, 5)
                        .allowsHitTesting(false)
                }
            }
            .focused($fieldFocused)

            HStack {
                if store.draft.isEdited {
                    Text("edited").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Discard") { Task { await store.discard() } }
                Button(store.isRecording ? "Stop" : "GO") {
                    Task {
                        if store.isRecording {
                            await store.stopRecording()
                        } else {
                            await store.submit()
                        }
                    }
                }
                .keyboardShortcut(.return)
                .buttonStyle(.borderedProminent)
                .disabled(!store.isRecording && !store.draft.canSubmit)
            }
        }
        .onChange(of: store.model.state) { state in
            // The field takes focus the moment it becomes editable, so the correction
            // the whole design rests on does not need a click first.
            if state == .review { fieldFocused = true }
        }
    }

    private var researchView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                if let failure = store.model.failure, failure.duringResearch {
                    Label(failure.message, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                    Button("Retry research") { Task { await store.submit() } }
                    Button("Ask something else") { Task { await store.discard() } }
                }
                if !store.model.searches.isEmpty {
                    DisclosureGroup("Search activity · \(store.model.searchesCompleted) completed") {
                        ForEach(store.model.searches) { row in
                            HStack(spacing: 6) {
                                if row.isRunning {
                                    ProgressView().controlSize(.small)
                                } else if row.error != nil {
                                    Image(systemName: "xmark.circle").foregroundStyle(.orange)
                                } else {
                                    Image(systemName: "checkmark.circle").foregroundStyle(.green)
                                }
                                Text(row.query).font(.callout).lineLimit(1)
                                Spacer()
                                if let count = row.resultCount {
                                    Text("\(count)").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    Divider()
                }

                if !store.model.reasoning.isEmpty {
                    DisclosureGroup("Model reasoning", isExpanded: $reasoningExpanded) {
                        Text(store.model.reasoning)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 8)
                    }
                    .font(.callout)
                }
                if store.model.answer.isEmpty && store.model.state.isResearching {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Preparing the answer…").foregroundStyle(.secondary)
                    }.padding(.vertical, 8)
                }
                if !store.model.answer.isEmpty {
                    AnswerView(markdown: store.model.answer)
                }

                // Clickable because the URLs come from Tavily, not the model — the
                // renumbering in citations.py is what makes an invented one impossible.
                if !store.model.citations.isEmpty {
                    Divider()
                    Text("Sources").font(.headline)
                    ForEach(store.model.citations, id: \.id) { citation in
                        if let url = URL(string: citation.url) {
                            Link(destination: url) {
                                Text("[\(citation.number)] \(citation.title)")
                                    .font(.callout)
                                    .multilineTextAlignment(.leading)
                            }
                        }
                    }
                }

                if store.model.completion != nil {
                    Button("Ask something else") { Task { await store.discard() } }
                        .padding(.top, 4)
                }
            }
            .padding(8)
            .frame(maxWidth: 760, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        // No max height: the panel is resizable now, so the answer should use whatever
        // room it is given rather than scrolling inside a fixed box.
        .frame(maxHeight: .infinity)
        .onChange(of: store.model.state) { state in
            if state == .submitted || state == .idle || state == .listening {
                reasoningExpanded = false
            }
        }
    }
}
#endif
