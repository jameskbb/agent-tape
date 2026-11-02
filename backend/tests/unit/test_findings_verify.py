"""The decision-level half of the verification suite: every verdict branch and every
``not_checkable`` reason, as pure fixtures against constructed inputs (no database, no
``Session`` - matching ``findings/verify.py``'s own "pure, no I/O" convention).

The semantic normalization fixtures (CRLF, NFKC, redaction markers themselves, ...) live in
``test_findings_normalize.py``; this file is the decision layer built on top of them. Both
files gate the findings UI; neither is skipped in ``make check``.
"""

from __future__ import annotations

from typing import Any

import pytest

from agenttape.findings import verify as verify_module
from agenttape.findings.citations import CITATION_VERDICTS, DECLARED_ONLY
from agenttape.findings.verify import (
    NOT_CHECKABLE_REASONS,
    OBSERVED_GAPS,
    CitationView,
    EventView,
    EvidenceWindow,
    ObservedCoverage,
    ReadView,
    VerdictDraft,
    WriteView,
    blob_id_candidates,
    could_be_whole_file,
    decide_event_citation,
    decide_file_citation,
    evidence_window,
    inputs_digest,
    observed_gaps,
    observed_statement,
)
from agenttape.security.redaction import DEFAULT_REDACTOR

GREETER = ["def greet():", "    return 'hi'"]


def numbered(lines: list[str], start: int = 1) -> tuple[tuple[int, str], ...]:
    return tuple((i, line) for i, line in enumerate(lines, start=start))


def _read(
    *,
    evidence_id: str = "read-1",
    path: str = "src/greeter.py",
    numbered_lines: tuple[tuple[int, str], ...] | None,
    result_sequence: int = 1,
    truncated: bool = False,
    content_state: str = "numbered",
    unavailable_detail: str | None = None,
    limit: int | None = None,
) -> ReadView:
    return ReadView(
        evidence_id=evidence_id,
        path=path,
        tool="Read",
        call_event_id=f"call-{evidence_id}",
        result_event_id=f"result-{evidence_id}",
        result_sequence=result_sequence,
        numbered_lines=numbered_lines,
        truncated=truncated,
        blob_ref=None,
        content_state=content_state,
        unavailable_detail=unavailable_detail,
        limit=limit,
    )


def _citation(
    *,
    path: str | None = "src/greeter.py",
    inside_boundary: bool | None = True,
    line_start: int | None = None,
    line_end: int | None = None,
    quote: str | None = "def greet():",
    quote_truncated: bool = False,
    kind: str = "file",
    event_ref: str | None = None,
    event_id: str | None = None,
) -> CitationView:
    return CitationView(
        kind=kind,
        path=path,
        inside_boundary=inside_boundary,
        line_start=line_start,
        line_end=line_end,
        quote=quote,
        quote_truncated=quote_truncated,
        event_ref=event_ref,
        event_id=event_id,
    )


def _window(
    *,
    complete: bool = True,
    reads: str = "telemetry",
    offline: tuple[dict[str, Any], ...] = (),
    statement: str = "Writes and final changes fully observed; reads available only from "
    "agent telemetry.",
) -> EvidenceWindow:
    return EvidenceWindow(
        complete=complete, reads=reads, offline_windows=offline, statement=statement
    )


def _event(
    *,
    event_id: str = "evt-1",
    type_: str = "tool_result",
    sequence: int = 1,
    content_key: str | None = "output",
    content: str | None,
    truncated: bool = False,
    placeholder: bool = False,
) -> EventView:
    return EventView(
        event_id=event_id,
        type=type_,
        sequence=sequence,
        content_key=content_key,
        content=content,
        truncated=truncated,
        placeholder=placeholder,
    )


# --------------------------------------------------------------------------- never_read / read_not_observed


def test_never_read_vs_read_not_observed() -> None:
    citation = _citation(quote="def greet():")

    complete = decide_file_citation(citation, [], window=_window(complete=True))
    assert complete.verdict == "never_read"
    assert "no recorded read" in complete.reason
    assert complete.evidence["window"]["complete"] is True

    unavailable = decide_file_citation(
        citation, [], window=_window(complete=False, reads="unavailable", statement="stmt-x")
    )
    assert unavailable.verdict == "read_not_observed"
    assert "stmt-x" in unavailable.reason

    offline = decide_file_citation(
        citation,
        [],
        window=_window(complete=False, offline=({"offline_from": "t0", "offline_to": "t1"},)),
    )
    assert offline.verdict == "read_not_observed"


def test_partial_read_outside_window_is_never_read_when_complete() -> None:
    citation = _citation(line_start=75, line_end=75, quote="line 75")
    read = _read(numbered_lines=numbered([f"line {i}" for i in range(50, 70)], start=50))
    draft = decide_file_citation(citation, [read], window=_window(complete=True))
    assert draft.verdict == "never_read"
    assert "50-69" in draft.reason


def test_partial_read_outside_window_is_read_not_observed_when_incomplete() -> None:
    citation = _citation(line_start=75, line_end=75, quote="line 75")
    read = _read(numbered_lines=numbered([f"line {i}" for i in range(50, 70)], start=50))
    draft = decide_file_citation(
        citation, [read], window=_window(complete=False, reads="unavailable")
    )
    assert draft.verdict == "read_not_observed"


def test_partial_read_inside_window_matches() -> None:
    citation = _citation(line_start=55, line_end=55, quote="line 55")
    read = _read(numbered_lines=numbered([f"line {i}" for i in range(50, 70)], start=50))
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "quote_matches"
    assert draft.evidence["line_range"] == [55, 55]


# --------------------------------------------------------------------------- quote_not_found


def test_quote_not_found_when_the_read_covers_the_claimed_lines() -> None:
    citation = _citation(line_start=1, line_end=2, quote="def greet(name):")
    read = _read(numbered_lines=numbered(GREETER))
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "quote_not_found"


def test_quote_not_found_with_no_claim_when_any_numbered_read_exists() -> None:
    citation = _citation(quote="not in the file anywhere")
    read = _read(numbered_lines=numbered(GREETER))
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "quote_not_found"


# --------------------------------------------------------------------------- redaction


def test_redacted_span_when_quote_absent_but_claimed_range_is_redacted() -> None:
    citation = _citation(line_start=1, line_end=2, quote="something else entirely")
    read = _read(numbered_lines=numbered(["def greet():", "    return [REDACTED:secret]"]))
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "redacted_at_cited_span"


def test_redacted_span_when_matched_lines_contain_the_marker() -> None:
    citation = _citation(line_start=1, line_end=1, quote="[REDACTED:secret]")
    read = _read(numbered_lines=numbered(["[REDACTED:secret]", "other line"]))
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "redacted_at_cited_span"


def test_redacted_span_with_no_claim_and_marker_anywhere_with_no_match() -> None:
    citation = _citation(quote="not present at all")
    read = _read(numbered_lines=numbered(["def greet():", "    return [REDACTED:secret]"]))
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "redacted_at_cited_span"


# --------------------------------------------------------------------------- truncation


def test_truncation_quote_absent_claim_beyond_last_returned_line() -> None:
    citation = _citation(line_start=10, line_end=10, quote="never returned")
    read = _read(numbered_lines=numbered(GREETER), truncated=True)
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "size_cap_exceeded"


def test_truncation_no_claim_and_quote_absent_is_also_size_cap_exceeded() -> None:
    citation = _citation(quote="never returned")
    read = _read(numbered_lines=numbered(GREETER), truncated=True)
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "size_cap_exceeded"


def test_truncation_quote_found_before_the_cut_still_matches() -> None:
    citation = _citation(line_start=1, line_end=1, quote="def greet():")
    read = _read(numbered_lines=numbered(GREETER), truncated=True)
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "quote_matches"


# --------------------------------------------------------------------------- not_checkable reasons


def test_outside_boundary() -> None:
    citation = _citation(path="<outside>/hosts", inside_boundary=False, quote="root:x:0:0")
    draft = decide_file_citation(citation, [], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "outside_boundary"


def test_no_quote_when_quote_is_none() -> None:
    citation = _citation(quote=None, line_start=1)
    read = _read(numbered_lines=numbered(GREETER))
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "no_quote"


def test_no_quote_when_quote_is_blank() -> None:
    citation = _citation(quote="   ")
    read = _read(numbered_lines=numbered(GREETER))
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "no_quote"


def test_quote_truncated() -> None:
    citation = _citation(quote="def greet():", quote_truncated=True)
    read = _read(numbered_lines=numbered(GREETER))
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "quote_truncated"


def test_no_content_recorded_when_only_content_free_reads_exist() -> None:
    citation = _citation(quote="def greet():")
    read = _read(numbered_lines=None, content_state="none")
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "no_content_recorded"


def test_content_not_line_numbered_when_output_has_no_line_numbers() -> None:
    citation = _citation(quote="def greet():")
    read = _read(numbered_lines=None, content_state="unnumbered")
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "content_not_line_numbered"


def test_blob_unavailable_when_a_read_is_unavailable_and_nothing_else_matches() -> None:
    citation = _citation(quote="def greet():")
    read = _read(numbered_lines=None, content_state="unavailable", unavailable_detail="pruned")
    draft = decide_file_citation(citation, [read], window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "blob_unavailable"


def test_every_not_checkable_reason_is_in_the_closed_vocabulary() -> None:
    assert set(NOT_CHECKABLE_REASONS) == {
        "outside_boundary",
        "no_quote",
        "quote_truncated",
        "no_content_recorded",
        "content_not_line_numbered",
        "size_cap_exceeded",
        "redacted_at_cited_span",
        "blob_unavailable",
    }


# --------------------------------------------------------------------------- stale


def test_stale_when_match_has_a_later_write() -> None:
    citation = _citation(line_start=1, line_end=2, quote="def greet():\n    return 'hi'")
    read = _read(numbered_lines=numbered(GREETER))
    write = WriteView(
        evidence_id="write-1",
        source_event_id="evt-write-1",
        operation="modify",
        path="src/greeter.py",
        previous_path=None,
        ts="2026-01-01T00:00:01",
        sequence=2,
        source="agent_hook",
        before_hash=None,
        after_hash="a" * 40,
    )
    draft = decide_file_citation(
        citation, [read], writes_by_read={"read-1": [write]}, window=_window()
    )
    assert draft.verdict == "stale"
    assert draft.evidence["change"]["operation"] == "modify"
    assert draft.evidence["later_writes"] == 1


def test_stale_prefers_the_newest_matching_read() -> None:
    citation = _citation(line_start=1, line_end=1, quote="def greet():")
    older = _read(evidence_id="read-old", numbered_lines=numbered(GREETER), result_sequence=1)
    newer = _read(evidence_id="read-new", numbered_lines=numbered(GREETER), result_sequence=2)
    write = WriteView(
        evidence_id="write-1",
        source_event_id=None,
        operation="modify",
        path="src/greeter.py",
        previous_path=None,
        ts="2026-01-01T00:00:05",
        sequence=5,
        source="agent_hook",
        before_hash=None,
        after_hash=None,
    )
    draft = decide_file_citation(
        citation,
        [older, newer],
        writes_by_read={"read-new": [write]},
        window=_window(),
    )
    assert draft.verdict == "stale"
    assert draft.evidence["read"]["evidence_id"] == "read-new"


@pytest.mark.parametrize(
    ("start", "count", "truncated", "limit", "expected"),
    [
        (1, 5, False, None, True),
        (1, 5, False, 10, True),  # asked for 10, got 5: the read reached the end of the file
        (1, 5, False, 5, False),  # offset=0, limit=5 on a longer file: got exactly 5 back
        (1, 5, False, 3, False),
        (1, 5, True, None, False),  # cut by the size cap
        (2, 5, False, None, False),  # an offset read
        (1, 0, False, None, False),
    ],
)
def test_could_be_whole_file(
    start: int, count: int, truncated: bool, limit: int | None, expected: bool
) -> None:
    """A read cut by its own ``limit`` is never treated as the whole file."""
    lines = numbered([f"line {i}" for i in range(count)], start=start)
    assert could_be_whole_file(lines, truncated=truncated, limit=limit) is expected


def test_could_be_whole_file_refuses_a_gap() -> None:
    lines = ((1, "a"), (2, "b"), (4, "d"))
    assert could_be_whole_file(lines, truncated=False, limit=None) is False


def _stale_with_limit(limit: int | None) -> VerdictDraft:
    """A read of the first two lines of a longer file (offset 0), then a write whose
    recorded before-hash is the whole longer file's."""
    longer = [*GREETER, "", "def other():", "    return 1"]
    whole_hash = blob_id_candidates(numbered(longer))[0]
    citation = _citation(line_start=1, line_end=1, quote="def greet():")
    read = _read(numbered_lines=numbered(GREETER), limit=limit)
    write = WriteView(
        evidence_id="write-1",
        source_event_id=None,
        operation="modify",
        path="src/greeter.py",
        previous_path=None,
        ts="2026-01-01T00:00:05",
        sequence=5,
        source="agent_hook",
        before_hash=whole_hash,
        after_hash=None,
    )
    return decide_file_citation(
        citation, [read], writes_by_read={"read-1": [write]}, window=_window()
    )


def test_stale_never_reports_a_hash_from_a_read_cut_by_its_limit() -> None:
    """Offset 0, limit 2 on a five-line file returns lines 1-2; their hash is
    not the file's hash at the time of the read, so none is reported."""
    draft = _stale_with_limit(2)
    assert draft.verdict == "stale"
    assert draft.evidence["hash_at_read"] is None
    assert draft.evidence["hash_at_read_matches_recorded"] is False


def test_stale_hash_from_an_unlimited_read_is_not_claimed_to_match() -> None:
    draft = _stale_with_limit(None)
    assert draft.evidence["hash_at_read"] == blob_id_candidates(numbered(GREETER))[0]
    assert draft.evidence["hash_at_read_matches_recorded"] is False


# --------------------------------------------------------------------------- event citations


def test_event_citation_missing_event_is_never_read_or_read_not_observed() -> None:
    citation = _citation(kind="event", path=None, quote="2 checks passed", event_ref="cmd_missing")
    complete = decide_event_citation(citation, None, window=_window(complete=True))
    assert complete.verdict == "never_read"
    incomplete = decide_event_citation(citation, None, window=_window(complete=False))
    assert incomplete.verdict == "read_not_observed"


def test_event_citation_placeholder_is_size_cap_exceeded() -> None:
    citation = _citation(kind="event", path=None, quote="2 checks passed")
    event = _event(content=None, placeholder=True)
    draft = decide_event_citation(citation, event, window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "size_cap_exceeded"


def test_event_citation_no_content_is_no_content_recorded() -> None:
    citation = _citation(kind="event", path=None, quote="2 checks passed")
    event = _event(content=None)
    draft = decide_event_citation(citation, event, window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "no_content_recorded"


def test_event_citation_quote_matches() -> None:
    citation = _citation(kind="event", path=None, quote="2 checks passed")
    event = _event(content="2 checks passed")
    draft = decide_event_citation(citation, event, window=_window())
    assert draft.verdict == "quote_matches"
    assert draft.evidence["line_range"] == [1, 1]


def test_event_citation_read_tool_result_uses_cat_n_mapping() -> None:
    content = "     5\tdef greet():\n     6\t    return 'hi'"
    citation = _citation(kind="event", path=None, quote="def greet():\n    return 'hi'")
    event = _event(content=content)
    draft = decide_event_citation(citation, event, window=_window())
    assert draft.verdict == "quote_matches"
    assert draft.evidence["line_range"] == [5, 6]


def test_event_citation_absent_and_output_cut_is_size_cap_exceeded() -> None:
    citation = _citation(kind="event", path=None, quote="not present")
    event = _event(content="something else", truncated=True)
    draft = decide_event_citation(citation, event, window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "size_cap_exceeded"


def test_event_citation_absent_with_marker_is_redacted_at_cited_span() -> None:
    citation = _citation(kind="event", path=None, quote="not present")
    event = _event(content="prefix [REDACTED:secret] suffix")
    draft = decide_event_citation(citation, event, window=_window())
    assert draft.verdict == "not_checkable"
    assert draft.evidence["not_checkable_reason"] == "redacted_at_cited_span"


def test_event_citation_quote_not_found() -> None:
    citation = _citation(kind="event", path=None, quote="not present")
    event = _event(content="line one\nline two")
    draft = decide_event_citation(citation, event, window=_window())
    assert draft.verdict == "quote_not_found"


# --------------------------------------------------------------------------- determinism


_FILE_FIXTURES: list[tuple[CitationView, list[ReadView]]] = [
    (
        _citation(line_start=1, line_end=2, quote="def greet():\n    return 'hi'"),
        [_read(numbered_lines=numbered(GREETER))],
    ),
    (_citation(quote="missing"), [_read(numbered_lines=numbered(GREETER))]),
    (_citation(quote="def greet():"), []),
    (_citation(path="<outside>/etc", inside_boundary=False, quote="x"), []),
    (_citation(quote=None), [_read(numbered_lines=numbered(GREETER))]),
]

_EVENT_FIXTURES: list[tuple[CitationView, EventView | None]] = [
    (
        _citation(kind="event", path=None, quote="2 checks passed"),
        _event(content="2 checks passed"),
    ),
    (_citation(kind="event", path=None, quote="not present"), _event(content="line one")),
    (_citation(kind="event", path=None, quote="anything"), None),
]


def test_file_citation_decisions_are_deterministic_and_in_the_closed_vocabulary() -> None:
    for citation, reads in _FILE_FIXTURES:
        first = decide_file_citation(citation, reads, window=_window())
        second = decide_file_citation(citation, reads, window=_window())
        assert first == second
        assert isinstance(first, VerdictDraft)
        assert first.verdict in CITATION_VERDICTS
        assert first.verdict != DECLARED_ONLY
        assert DEFAULT_REDACTOR.redact_value(first.evidence) == first.evidence


def test_event_citation_decisions_are_deterministic_and_in_the_closed_vocabulary() -> None:
    for citation, event in _EVENT_FIXTURES:
        first = decide_event_citation(citation, event, window=_window())
        second = decide_event_citation(citation, event, window=_window())
        assert first == second
        assert first.verdict in CITATION_VERDICTS
        assert first.verdict != DECLARED_ONLY
        assert DEFAULT_REDACTOR.redact_value(first.evidence) == first.evidence


# ------------------------------------------------------------------ observed-session windows


def _coverage(**overrides: Any) -> ObservedCoverage:
    fields: dict[str, Any] = {
        "source": "hook",
        "recorded": True,
        "first_hook": "SessionStart",
        "first_hook_source": "startup",
        "dropped_hook_events": 0,
        "observer_restarts": 0,
    }
    fields.update(overrides)
    return ObservedCoverage(**fields)


@pytest.mark.parametrize(
    ("overrides", "gaps"),
    [
        ({}, ()),
        ({"source": "mcp", "first_hook": None, "first_hook_source": None}, ("no_hooks",)),
        ({"recorded": False, "first_hook": None, "first_hook_source": None}, ("coverage_unknown",)),
        ({"first_hook": "PreToolUse", "first_hook_source": None}, ("joined_mid_session",)),
        ({"first_hook_source": "resume"}, ("joined_mid_session",)),
        ({"first_hook_source": "compact"}, ("joined_mid_session",)),
        ({"first_hook_source": "fork"}, ("joined_mid_session",)),
        # After /clear Claude Code starts a new conversation with an empty context.
        ({"first_hook_source": "clear"}, ()),
        ({"first_hook_source": None}, ("joined_mid_session",)),
        ({"dropped_hook_events": 3}, ("dropped_hook_events",)),
        ({"observer_restarts": 1}, ("observer_restarted",)),
        (
            {"first_hook": "PostToolUse", "dropped_hook_events": 1, "observer_restarts": 2},
            ("joined_mid_session", "dropped_hook_events", "observer_restarted"),
        ),
    ],
)
def test_observed_gaps(overrides: dict[str, Any], gaps: tuple[str, ...]) -> None:
    assert observed_gaps(_coverage(**overrides)) == gaps
    assert set(gaps) <= set(OBSERVED_GAPS)


def test_a_gap_in_an_observed_session_is_read_not_observed_never_never_read() -> None:
    citation = _citation()
    for gap_overrides in (
        {"source": "mcp"},
        {"recorded": False},
        {"first_hook": "PostToolUse"},
        {"first_hook_source": "resume"},
        {"dropped_hook_events": 1},
        {"observer_restarts": 1},
    ):
        window = evidence_window(
            reads="telemetry",
            offline_windows=[],
            statement="Writes and final changes fully observed.",
            observed=_coverage(**gap_overrides),
        )
        assert window.complete is False
        draft = decide_file_citation(citation, [], window=window)
        assert draft.verdict == "read_not_observed", gap_overrides
        assert draft.evidence["window"]["observed"]["gaps"]
        assert window.statement in draft.reason
        assert DEFAULT_REDACTOR.redact_value(draft.evidence) == draft.evidence


def test_an_offline_window_does_not_repeat_gaps_the_statement_already_names() -> None:
    """File Evidence's statement names an observed run's gaps itself, so an offline window
    (where the window keeps that statement) never says them twice."""
    coverage = _coverage(source="mcp")
    gap_text = observed_statement(coverage)
    statement = (
        f"Partial: AgentTape was offline from A to B; final Git reconciliation ran. {gap_text}"
    )
    window = evidence_window(
        reads="telemetry",
        offline_windows=[{"offline_from": "A", "offline_to": "B"}],
        statement=statement,
        observed=coverage,
    )
    assert window.statement.count(gap_text) == 1
    assert window.statement == statement


def test_an_observed_session_covered_from_startup_is_never_read() -> None:
    window = evidence_window(
        reads="telemetry", offline_windows=[], statement="Writes observed.", observed=_coverage()
    )
    assert window.complete is True
    assert decide_file_citation(_citation(), [], window=window).verdict == "never_read"


#: ``inputs_digest`` of fixed inputs, computed with ``findings/verify.py`` as it was at
#: commit 39d7572 (before observed-run coverage existed, ``VERIFIER_VERSION`` "1"). Pinned
#: values, never recomputed through today's code, so a new key in the payload of a
#: launched run (an ``"observed": null``, say) fails here instead of changing both sides.
_GOLDEN_DIGESTS_AT_39D7572 = {
    "no_reads": "661ade87a3e0ba4b530375785eead207458fbee7974952da34700777dbdfdf08",
    "read_offline": "b7582b19ddb9ed638e1f547c825986148a073c0226245053557a2dbc3191f7af",
    "event": "6ff504212927837cb0903b79dbec5f38f1faec52fafd0255936ef4029866046f",
}


def test_a_launched_run_digest_matches_the_golden_value_from_before_observed_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``observed=None`` (a launched run) decides exactly as before and adds nothing to
    ``inputs_digest``: apart from ``verifier_version`` (part of the payload by design, so
    a verifier change is visible), a launched run's digest is byte-identical to the one
    39d7572 computed, so an automatic pass never re-records it for a key it never had."""
    monkeypatch.setattr(verify_module, "VERIFIER_VERSION", "1")
    window = evidence_window(
        reads="telemetry", offline_windows=[], statement="Writes observed.", observed=None
    )
    assert window.observed is None
    assert decide_file_citation(_citation(), [], window=window).verdict == "never_read"
    assert inputs_digest(_citation(), window=window) == _GOLDEN_DIGESTS_AT_39D7572["no_reads"]

    offline = evidence_window(
        reads="telemetry",
        offline_windows=[{"offline_from": "A", "offline_to": "B"}],
        statement="Partial.",
        observed=None,
    )
    read = _read(numbered_lines=numbered(GREETER))
    assert (
        inputs_digest(
            _citation(), reads=[read], window=offline, content_digests={"read-1": "sha256:abc"}
        )
        == _GOLDEN_DIGESTS_AT_39D7572["read_offline"]
    )

    event_citation = _citation(
        kind="event",
        path=None,
        inside_boundary=None,
        quote="ok",
        event_ref="toolu_1",
        event_id="ev-1",
    )
    event = EventView(
        event_id="ev-1",
        type="tool_result",
        sequence=4,
        content_key="output",
        content="ok\n",
        truncated=False,
        placeholder=False,
    )
    assert (
        inputs_digest(event_citation, window=window, event=event)
        == _GOLDEN_DIGESTS_AT_39D7572["event"]
    )

    covered = evidence_window(
        reads="telemetry", offline_windows=[], statement="Writes observed.", observed=_coverage()
    )
    assert inputs_digest(_citation(), window=covered) != _GOLDEN_DIGESTS_AT_39D7572["no_reads"]


def test_a_gap_in_one_of_several_hosted_sessions_names_which() -> None:
    coverage = _coverage(first_hook_source="resume", observer_restarts=1, sessions=3)
    assert observed_gaps(coverage) == ("joined_mid_session", "observer_restarted")
    statement = observed_statement(coverage)
    assert "after one of this run's 3 Claude Code sessions had started" in statement
    assert "(the first was SessionStart (resume))" in statement
    assert "while observing this run's sessions" in statement
    assert coverage.as_evidence()["sessions"] == 3
