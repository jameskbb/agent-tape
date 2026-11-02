"""``parse_citations`` and the citation verdict vocabulary: pure logic, no I/O."""

from __future__ import annotations

import pytest

from agenttape.findings.citations import (
    CITATION_VERDICTS,
    DECLARED_ONLY,
    MAX_CITATIONS,
    MAX_EVENT_REF_CHARS,
    MAX_LINE,
    MAX_PATH_CHARS,
    MAX_QUOTE_CHARS,
    CitationDraft,
    parse_citations,
)


@pytest.mark.parametrize("links", [None, {}, "not a list"])
def test_non_list_links_yield_nothing(links: object) -> None:
    assert parse_citations(links) == ([], 0)


def test_file_link_yields_a_file_draft() -> None:
    drafts, dropped = parse_citations(
        [{"path": "src/a.py", "line_start": 1, "line_end": 2, "quote": "def f():"}]
    )
    assert dropped == 0
    [draft] = drafts
    assert draft == CitationDraft(
        kind="file",
        path="src/a.py",
        line_start=1,
        line_end=2,
        quote="def f():",
        quote_truncated=False,
        event_ref=None,
        relation="supports",
    )


def test_event_link_yields_an_event_draft() -> None:
    drafts, dropped = parse_citations([{"event_ref": "cmd_1", "quote": "2 checks passed"}])
    assert dropped == 0
    [draft] = drafts
    assert draft.kind == "event"
    assert draft.path is None
    assert draft.event_ref == "cmd_1"
    assert draft.quote == "2 checks passed"


def test_link_with_both_path_and_event_ref_is_a_file_draft_that_keeps_event_ref() -> None:
    drafts, dropped = parse_citations([{"path": "a.py", "event_ref": "cmd_1"}])
    assert dropped == 0
    [draft] = drafts
    assert draft.kind == "file"
    assert draft.path == "a.py"
    assert draft.event_ref == "cmd_1"


def test_legacy_and_junk_links_yield_no_draft_and_are_not_dropped() -> None:
    drafts, dropped = parse_citations(
        [{"source_id": "kb:pricing", "relation": "supports"}, "not a dict", 42, None]
    )
    assert drafts == []
    assert dropped == 0


def test_positions_are_contiguous_over_citation_shaped_links_only() -> None:
    drafts, dropped = parse_citations(
        [
            {"source_id": "kb:pricing", "relation": "supports"},
            {"path": "a.py"},
            {"source_id": "kb:other", "relation": "missing"},
            {"event_ref": "cmd_1"},
        ]
    )
    assert dropped == 0
    assert [d.path or d.event_ref for d in drafts] == ["a.py", "cmd_1"]


@pytest.mark.parametrize(
    "value",
    [True, False, "1", 1.5, 0, -1, MAX_LINE + 1],
)
def test_invalid_line_numbers_become_none(value: object) -> None:
    drafts, _ = parse_citations([{"path": "a.py", "line_start": value, "line_end": value}])
    [draft] = drafts
    assert draft.line_start is None
    assert draft.line_end is None


def test_valid_line_numbers_are_kept_even_when_end_is_before_start() -> None:
    drafts, _ = parse_citations([{"path": "a.py", "line_start": 10, "line_end": 3}])
    [draft] = drafts
    assert draft.line_start == 10
    assert draft.line_end == 3


def test_long_quote_is_truncated_and_flagged() -> None:
    quote = "x" * (MAX_QUOTE_CHARS + 500)
    drafts, _ = parse_citations([{"path": "a.py", "quote": quote}])
    [draft] = drafts
    assert draft.quote == "x" * MAX_QUOTE_CHARS
    assert draft.quote_truncated is True


def test_non_string_quote_becomes_none() -> None:
    drafts, _ = parse_citations([{"path": "a.py", "quote": 123}])
    [draft] = drafts
    assert draft.quote is None
    assert draft.quote_truncated is False


def test_long_event_ref_is_cut() -> None:
    ref = "r" * (MAX_EVENT_REF_CHARS + 50)
    drafts, _ = parse_citations([{"event_ref": ref}])
    [draft] = drafts
    assert draft.event_ref == ref[:MAX_EVENT_REF_CHARS]


def test_path_with_nul_is_invalid_and_dropped() -> None:
    drafts, dropped = parse_citations([{"path": "a\x00.py"}])
    assert drafts == []
    assert dropped == 1


def test_path_over_max_chars_is_invalid_and_dropped() -> None:
    drafts, dropped = parse_citations([{"path": "a" * (MAX_PATH_CHARS + 1)}])
    assert drafts == []
    assert dropped == 1


def test_more_than_max_citations_keeps_the_first_and_drops_the_rest() -> None:
    links = [{"path": f"f{i}.py"} for i in range(MAX_CITATIONS + 10)]
    drafts, dropped = parse_citations(links)
    assert len(drafts) == MAX_CITATIONS
    assert dropped == 10
    assert [d.path for d in drafts] == [f"f{i}.py" for i in range(MAX_CITATIONS)]


def test_unknown_relation_becomes_supports() -> None:
    drafts, _ = parse_citations([{"path": "a.py", "relation": "nonsense"}])
    [draft] = drafts
    assert draft.relation == "supports"


@pytest.mark.parametrize("relation", ["supports", "conflicts", "missing"])
def test_known_relations_are_kept(relation: str) -> None:
    drafts, _ = parse_citations([{"path": "a.py", "relation": relation}])
    [draft] = drafts
    assert draft.relation == relation


def test_citation_verdict_vocabulary() -> None:
    assert CITATION_VERDICTS == (
        "quote_matches",
        "stale",
        "never_read",
        "read_not_observed",
        "quote_not_found",
        "not_checkable",
    )
    assert DECLARED_ONLY == "declared_only"
    assert DECLARED_ONLY not in CITATION_VERDICTS
