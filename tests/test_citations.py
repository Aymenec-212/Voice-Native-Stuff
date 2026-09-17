from vnr.research.citations import CitationRewriter, build_sources_section, tidy
from vnr.research.sources import SourceRegistry

from .conftest import result


def registry_with(n: int) -> SourceRegistry:
    registry = SourceRegistry()
    registry.add_all([result(i) for i in range(1, n + 1)], "q")
    return registry


def test_ids_are_renumbered_in_order_of_first_appearance():
    rewriter = CitationRewriter(registry_with(3))
    out = rewriter.rewrite("Claim A [S3]. Claim B [S1]. Claim C [S3].")
    assert out == "Claim A [1]. Claim B [2]. Claim C [1]."
    assert [s.id for s in rewriter.report.cited] == ["S3", "S1"]


def test_citations_split_across_stream_deltas_are_held_back():
    rewriter = CitationRewriter(registry_with(12))
    deltas = ["The model ", "says this [", "S1", "2] and ", "that [S", "2]."]
    visible = "".join(rewriter.feed(d) for d in deltas) + rewriter.flush()
    assert visible == "The model says this [1] and that [2]."
    # nothing was shown before the citation could be resolved
    assert "[S" not in visible


def test_invented_ids_are_dropped_and_reported():
    rewriter = CitationRewriter(registry_with(2))
    out = rewriter.rewrite("Real [S1]. Invented [S99].")
    assert out == "Real [1]. Invented."
    assert tidy(out) == out
    assert rewriter.report.invalid_ids == ["S99"]
    assert [s.id for s in rewriter.report.cited] == ["S1"]


def test_multiple_ids_in_one_bracket():
    rewriter = CitationRewriter(registry_with(3))
    assert rewriter.rewrite("Both agree [S2, S3].") == "Both agree [1][2]."


def test_ordinary_brackets_are_untouched():
    rewriter = CitationRewriter(registry_with(2))
    text = "Use arr[0] and [see below] and [Section 3]."
    assert rewriter.rewrite(text) == text
    assert rewriter.report.cited == []


def test_sources_section_is_built_from_retrieved_urls():
    registry = registry_with(3)
    rewriter = CitationRewriter(registry)
    rewriter.rewrite("A [S2]. B [S1].")
    section = build_sources_section(rewriter.report, registry)
    assert section.splitlines() == [
        "Sources",
        "[1] Result 2 — https://example.com/article-2",
        "[2] Result 1 — https://example.com/article-1",
    ]


def test_uncited_answers_still_list_what_was_retrieved():
    registry = registry_with(2)
    rewriter = CitationRewriter(registry)
    rewriter.rewrite("No citations here.")
    assert rewriter.report.uncited is True
    assert build_sources_section(rewriter.report, registry).startswith("Sources consulted")


def test_no_section_when_nothing_was_retrieved():
    registry = SourceRegistry()
    rewriter = CitationRewriter(registry)
    rewriter.rewrite("Could not find evidence.")
    assert build_sources_section(rewriter.report, registry) == ""


def test_trailing_partial_citation_is_flushed_verbatim():
    rewriter = CitationRewriter(registry_with(1))
    assert rewriter.feed("ends with [S") == "ends with"
    assert rewriter.flush() == " [S"  # held back verbatim, space included


def test_dropping_a_citation_leaves_no_orphan_space_even_across_deltas():
    rewriter = CitationRewriter(registry_with(1))
    visible = "".join(rewriter.feed(d) for d in ["could not be corroborated [S", "9]."])
    assert visible + rewriter.flush() == "could not be corroborated."


def test_a_kept_citation_keeps_its_leading_space():
    rewriter = CitationRewriter(registry_with(1))
    assert rewriter.rewrite("as shown [S1].") == "as shown [1]."


def test_word_by_word_streaming_survives_dropped_citations():
    """The shape real deltas arrive in: one word at a time, space at the end."""
    rewriter = CitationRewriter(registry_with(1))
    words = ["a", "claim", "[S1]", "and", "one", "[S99]", "that", "is", "not"]
    visible = "".join(rewriter.feed(w + " ") for w in words) + rewriter.flush()
    assert visible.strip() == "a claim [1] and one that is not"
