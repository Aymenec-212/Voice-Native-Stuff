#if os(macOS)
import SwiftUI
import VNRKit

struct AnswerView: View {
    let markdown: String

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(Array(AnswerMarkdown.blocks(markdown).enumerated()), id: \.offset) { _, block in
                blockView(block)
            }
        }
        .font(.system(size: 15))
        .lineSpacing(5)
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func inline(_ text: String) -> Text {
        let attributed = (try? AttributedString(markdown: text, options: .init(
            interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(text)
        return Text(attributed)
    }

    @ViewBuilder private func blockView(_ block: AnswerBlock) -> some View {
        switch block {
        case .heading(let level, let title):
            inline(title)
                .font(.system(size: level == 1 ? 26 : level == 2 ? 21 : 17, weight: .semibold))
                .padding(.top, 8)
                .fixedSize(horizontal: false, vertical: true)
        case .paragraph(let text):
            inline(text).fixedSize(horizontal: false, vertical: true)
        case .item(let marker, let text, let indent):
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(marker).foregroundStyle(.secondary).frame(minWidth: 18, alignment: .trailing)
                inline(text).frame(maxWidth: .infinity, alignment: .leading)
            }.padding(.leading, CGFloat(min(indent, 12)) * 6)
        case .quote(let text):
            HStack(spacing: 12) {
                RoundedRectangle(cornerRadius: 2).fill(Color.accentColor.opacity(0.5)).frame(width: 3)
                inline(text).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
            }.fixedSize(horizontal: false, vertical: true)
        case .code(let text):
            ScrollView(.horizontal) {
                Text(verbatim: text).font(.system(size: 13, design: .monospaced)).padding(12)
            }
            .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
        case .divider:
            Divider().padding(.vertical, 4)
        case .table(let rows):
            ScrollView(.horizontal) {
                Grid(alignment: .topLeading, horizontalSpacing: 20, verticalSpacing: 10) {
                    ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                        GridRow {
                            ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                                inline(cell).fontWeight(index == 0 ? .semibold : .regular)
                                    .frame(maxWidth: 280, alignment: .leading)
                            }
                        }
                        if index == 0 { Divider() }
                    }
                }.padding(12)
            }.background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
        }
    }
}
#endif
