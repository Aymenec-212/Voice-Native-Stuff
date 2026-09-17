from vnr.research.sources import MAX_SNIPPET_CHARS, SourceRegistry, canonical_url, truncate
from vnr.research.tavily import normalize_results

from .conftest import result


def test_normalization_keeps_only_the_fields_the_model_needs():
    payload = {
        "results": [
            {
                "title": "Kyutai STT",
                "url": "https://kyutai.org/stt",
                "content": "  streaming   speech to text  ",
                "score": "0.87",
                "published_date": "2026-01-02",
                "raw_content": "should be ignored",
                "favicon": "https://kyutai.org/f.ico",
            }
        ]
    }
    [item] = normalize_results(payload, max_results=5)
    assert item.title == "Kyutai STT"
    assert item.content == "streaming speech to text"
    assert item.score == 0.87
    assert item.published_date == "2026-01-02"
    assert not hasattr(item, "favicon")


def test_normalization_is_tolerant_of_junk():
    payload = {
        "results": [
            {"title": "no url"},
            "not a dict",
            {"url": "https://a.test/1", "score": "n/a"},
        ]
    }
    results = normalize_results(payload, max_results=5)
    assert [r.url for r in results] == ["https://a.test/1"]
    assert results[0].score is None
    assert results[0].title == "https://a.test/1"  # falls back to the URL
    assert normalize_results({}, max_results=5) == []
    assert normalize_results({"results": "nope"}, max_results=5) == []


def test_normalization_respects_max_results():
    payload = {"results": [{"url": f"https://a.test/{i}"} for i in range(10)]}
    assert len(normalize_results(payload, max_results=3)) == 3


def test_long_snippets_are_truncated():
    long_text = "word " * 1000
    assert len(truncate(long_text)) <= MAX_SNIPPET_CHARS


def test_canonical_url_ignores_cosmetic_differences():
    assert canonical_url("https://WWW.Example.com/a/") == canonical_url("https://example.com/a")
    assert canonical_url("https://example.com/a#frag") == canonical_url("https://example.com/a")
    assert canonical_url("https://example.com/a?x=1") != canonical_url("https://example.com/a")


def test_ids_are_sequential_and_stable_across_searches():
    registry = SourceRegistry()
    first = registry.add_all([result(1), result(2)], "query one")
    assert [s.id for s in first] == ["S1", "S2"]

    # result(1) again, plus a genuinely new one
    second = registry.add_all([result(1), result(3)], "query two")
    assert [s.id for s in second] == ["S1", "S3"]
    assert len(registry) == 3
    assert registry.get("s1") is registry.get("S1")
    assert registry.get("S9") is None
    # the repeat keeps the query that first produced it
    assert registry.get("S1").query == "query one"


def test_prompt_block_carries_id_title_url_and_content():
    registry = SourceRegistry()
    [source] = registry.add_all([result(1)], "q")
    block = source.to_prompt_block()
    assert "[S1]" in block
    assert "https://example.com/article-1" in block
    assert "Body of result 1." in block
