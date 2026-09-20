import Foundation

/// Block structure stays separate from inline Markdown so headings and lists receive
/// actual layout, including while the final paragraph is still streaming.
public enum AnswerBlock: Equatable, Sendable {
    case heading(Int, String)
    case paragraph(String)
    case item(String, String, Int)
    case quote(String)
    case code(String)
    case divider
    case table([[String]])
}

public enum AnswerMarkdown {
    public static func blocks(_ source: String) -> [AnswerBlock] {
        let lines = source.components(separatedBy: .newlines)
        var result: [AnswerBlock] = []
        var paragraph: [String] = []
        var index = 0
        func flush() {
            if !paragraph.isEmpty {
                result.append(.paragraph(paragraph.joined(separator: "\n")))
                paragraph = []
            }
        }
        func cells(_ line: String) -> [String] {
            var text = line.trimmingCharacters(in: .whitespaces)
            if text.hasPrefix("|") { text.removeFirst() }
            if text.hasSuffix("|") { text.removeLast() }
            return text.components(separatedBy: "|").map {
                $0.trimmingCharacters(in: .whitespaces)
            }
        }
        while index < lines.count {
            let raw = lines[index]
            let line = raw.trimmingCharacters(in: .whitespaces)
            index += 1
            if line.isEmpty { flush(); continue }
            if line.hasPrefix("```") || line.hasPrefix("~~~") {
                flush()
                let marker = String(line.prefix(3))
                var code: [String] = []
                while index < lines.count && !lines[index].trimmingCharacters(in: .whitespaces).hasPrefix(marker) {
                    code.append(lines[index]); index += 1
                }
                if index < lines.count { index += 1 }
                result.append(.code(code.joined(separator: "\n")))
                continue
            }
            if line.contains("|"), index < lines.count {
                let separator = cells(lines[index])
                if !separator.isEmpty && separator.allSatisfy({
                    $0.filter { $0 == "-" }.count >= 3 && $0.allSatisfy { "-: ".contains($0) }
                }) {
                    flush()
                    var rows = [cells(line)]
                    index += 1
                    while index < lines.count && lines[index].contains("|") {
                        rows.append(cells(lines[index])); index += 1
                    }
                    result.append(.table(rows)); continue
                }
            }
            let hashes = line.prefix { $0 == "#" }.count
            if (1...6).contains(hashes) {
                flush()
                let title = String(line.dropFirst(hashes)).trimmingCharacters(in: .whitespaces)
                result.append(.heading(hashes, title)); continue
            }
            if index < lines.count, !line.isEmpty {
                let next = lines[index].trimmingCharacters(in: .whitespaces)
                if next.count >= 3 && (next.allSatisfy { $0 == "=" } || next.allSatisfy { $0 == "-" }) {
                    flush(); result.append(.heading(next.first == "=" ? 1 : 2, line))
                    index += 1; continue
                }
            }
            let compact = line.filter { !$0.isWhitespace }
            if compact.count >= 3 && ["-", "*", "_"].contains(where: { marker in
                compact.allSatisfy { String($0) == marker }
            }) { flush(); result.append(.divider); continue }
            if line.hasPrefix(">") {
                flush(); result.append(.quote(String(line.dropFirst()).trimmingCharacters(in: .whitespaces)))
                continue
            }
            let indent = raw.prefix { $0 == " " || $0 == "\t" }.count
            if ["- ", "* ", "+ "].contains(where: { line.hasPrefix($0) }) {
                flush(); result.append(.item("•", String(line.dropFirst(2)), indent)); continue
            }
            let digits = line.prefix { $0.isNumber }
            let tail = line.dropFirst(digits.count)
            if !digits.isEmpty && (tail.hasPrefix(". ") || tail.hasPrefix(") ")) {
                flush()
                result.append(.item(String(digits) + ".", String(tail.dropFirst(2)), indent)); continue
            }
            paragraph.append(raw)
        }
        flush()
        return result
    }
}
