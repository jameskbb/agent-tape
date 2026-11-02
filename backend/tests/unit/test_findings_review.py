"""``findings/review.py``: prompt construction and vocabulary, pure logic, no I/O.

This module proves how AgentTape constructs the reviewer's prompt (untrusted finding text
and citation quotes reach the reviewer only inside one nonce-delimited, single-line JSON
block, never outside it) and that a scripted reviewer's disagreement can be recorded
mechanically. It does not prove that a real language model resists prompt injection - no
test here calls a model, and none of these assertions depend on one.
"""

from __future__ import annotations

import pytest

from agenttape.domain.prompts import template_tokens
from agenttape.findings.citations import CITATION_VERDICTS, DECLARED_ONLY
from agenttape.findings.review import (
    MAX_REVIEW_BLOCK_CHARS,
    MAX_REVIEW_CITATIONS,
    MAX_REVIEW_QUOTE_CHARS,
    MODEL_JUDGMENT,
    REVIEW_KIND,
    REVIEW_OUTCOMES,
    REVIEWER_INSTRUCTIONS,
    ReviewTargetDraft,
    begin_line,
    build_reviewer_prompt,
    end_line,
    extract_untrusted_block,
    parse_review_target,
    review_details,
)
from tests.fixtures.review_injection import INJECTION_FINDINGS, SENTINELS, review_items

NONCE = "abcdef0123456789"
TARGET_RUN_ID = "run_target_1"


def _prompt(findings: list[dict[str, object]], *, nonce: str = NONCE) -> object:
    return build_reviewer_prompt(target_run_id=TARGET_RUN_ID, findings=findings, nonce=nonce)


# --------------------------------------------------------------------------- vocabulary


def test_review_outcome_vocabulary_is_separate_from_citation_verdicts() -> None:
    assert REVIEW_OUTCOMES == ("upheld", "refuted", "inconclusive")
    assert set(REVIEW_OUTCOMES).isdisjoint(CITATION_VERDICTS)
    assert DECLARED_ONLY not in REVIEW_OUTCOMES
    assert MODEL_JUDGMENT == "model judgment"


# --------------------------------------------------------------------------- parse_review_target


def test_a_plain_finding_is_not_review_shaped() -> None:
    assert parse_review_target({"id": "F1", "kind": "claim", "text": "x"}) is None


def test_review_kind_without_target_is_missing_target() -> None:
    draft = parse_review_target({"id": "R1", "kind": REVIEW_KIND, "text": "x"})
    assert draft == ReviewTargetDraft(
        run_id=None, finding_key=None, outcome=None, outcome_raw=None, problem="missing_target"
    )


def test_a_string_target_is_invalid_target() -> None:
    draft = parse_review_target({"id": "R1", "kind": REVIEW_KIND, "target": "F1"})
    assert draft is not None
    assert draft.problem == "invalid_target"


def test_an_unknown_outcome_is_invalid_outcome_and_keeps_the_raw_value() -> None:
    draft = parse_review_target(
        {
            "id": "R1",
            "kind": REVIEW_KIND,
            "target": {"run_id": "r1", "finding_key": "F1", "outcome": "quote_matches"},
        }
    )
    assert draft is not None
    assert draft.outcome is None
    assert draft.outcome_raw == "quote_matches"
    assert draft.problem == "invalid_outcome"


def test_a_valid_target_has_no_problem() -> None:
    draft = parse_review_target(
        {
            "id": "R1",
            "kind": REVIEW_KIND,
            "target": {"run_id": "r1", "finding_key": "F1", "outcome": "upheld"},
        }
    )
    assert draft == ReviewTargetDraft(
        run_id="r1", finding_key="F1", outcome="upheld", outcome_raw=None, problem=None
    )


# --------------------------------------------------------------------------- review_details


def _lookup(known: dict[tuple[str, str], str]) -> object:
    def find_target(run_id: str, finding_key: str) -> str | None:
        return known.get((run_id, finding_key))

    return find_target


def test_review_details_with_no_section_is_not_a_review_run() -> None:
    draft = ReviewTargetDraft(
        run_id="r1", finding_key="F1", outcome="upheld", outcome_raw=None, problem=None
    )
    result = review_details(draft, None, _lookup({}))
    assert result["reason"] == "not_a_review_run"
    assert result["resolved"] is False
    assert result["judgment"] == "model judgment"
    assert result["counts_as_proof"] is False


def test_review_details_with_another_run_id_is_target_run_mismatch() -> None:
    draft = ReviewTargetDraft(
        run_id="r1", finding_key="F1", outcome="upheld", outcome_raw=None, problem=None
    )
    section = {"target_run_id": "other-run", "finding_keys": ["F1"]}
    result = review_details(draft, section, _lookup({}))
    assert result["reason"] == "target_run_mismatch"
    assert result["resolved"] is False


def test_review_details_with_key_not_presented_is_not_presented() -> None:
    draft = ReviewTargetDraft(
        run_id="r1", finding_key="F1", outcome="upheld", outcome_raw=None, problem=None
    )
    section = {"target_run_id": "r1", "finding_keys": ["F2"]}
    result = review_details(draft, section, _lookup({}))
    assert result["reason"] == "not_presented"
    assert result["resolved"] is False


def test_review_details_with_no_matching_finding_is_unknown_finding() -> None:
    draft = ReviewTargetDraft(
        run_id="r1", finding_key="F1", outcome="upheld", outcome_raw=None, problem=None
    )
    section = {"target_run_id": "r1", "finding_keys": ["F1"]}
    result = review_details(draft, section, _lookup({}))
    assert result["reason"] == "unknown_finding"
    assert result["resolved"] is False


def test_review_details_resolves_when_everything_matches() -> None:
    draft = ReviewTargetDraft(
        run_id="r1", finding_key="F1", outcome="upheld", outcome_raw=None, problem=None
    )
    section = {"target_run_id": "r1", "finding_keys": ["F1"]}
    result = review_details(draft, section, _lookup({("r1", "F1"): "finding-db-id"}))
    assert result["resolved"] is True
    assert result["reason"] is None
    assert result["target_finding_id"] == "finding-db-id"
    assert result["judgment"] == "model judgment"
    assert result["counts_as_proof"] is False


def test_review_details_propagates_the_drafts_own_problem_first() -> None:
    draft = ReviewTargetDraft(
        run_id=None, finding_key=None, outcome=None, outcome_raw=None, problem="missing_target"
    )
    result = review_details(draft, {"target_run_id": "r1", "finding_keys": []}, _lookup({}))
    assert result["reason"] == "missing_target"


# --------------------------------------------------------------------------- injection: stays inside the block


def test_injection_prompt_has_exactly_one_begin_and_end_line() -> None:
    prompt = _prompt(review_items(INJECTION_FINDINGS))
    lines = prompt.text.splitlines()
    assert lines.count(begin_line(NONCE)) == 1
    assert lines.count(end_line(NONCE)) == 1


def test_no_sentinel_leaks_outside_the_delimited_block() -> None:
    prompt = _prompt(review_items(INJECTION_FINDINGS))
    lines = prompt.text.splitlines()
    begin_at = lines.index(begin_line(NONCE))
    end_at = lines.index(end_line(NONCE))
    before = "\n".join(lines[:begin_at])
    after = "\n".join(lines[end_at + 1 :])
    for sentinel in SENTINELS:
        assert sentinel not in before
        assert sentinel not in after


def test_no_template_or_secret_token_survives_in_the_prompt() -> None:
    prompt = _prompt(review_items(INJECTION_FINDINGS))
    assert "{{" not in prompt.text
    assert template_tokens(prompt.text) == ([], [])


def test_extract_untrusted_block_round_trips_items_losslessly_except_neutralized_fields() -> None:
    findings = review_items(INJECTION_FINDINGS)
    prompt = _prompt(findings)
    decoded = extract_untrusted_block(prompt.text, NONCE)
    neutralized_fields: dict[str, set[str]] = {}
    for key, field in prompt.neutralized:
        neutralized_fields.setdefault(key, set()).add(field)
    by_key = {f["key"]: f for f in findings}
    for item in decoded:
        original = by_key[item["key"]]
        fields = neutralized_fields.get(item["key"], set())
        if "text" not in fields:
            assert item["text"] == original["text"]
        for oc, dc in zip(original["citations"], item["citations"], strict=True):
            if "quote" not in fields:
                assert dc["quote"] == oc["quote"]
            if "path" not in fields:
                assert dc["path"] == oc["path"]
            if "event_ref" not in fields:
                assert dc["event_ref"] == oc["event_ref"]


def test_delimiter_smuggling_text_and_quote_are_neutralized_and_recorded() -> None:
    prompt = _prompt(review_items(INJECTION_FINDINGS))
    decoded = extract_untrusted_block(prompt.text, NONCE)
    [item] = [i for i in decoded if i["key"] == "INJ-DELIM"]
    assert "[delimiter text removed]" in item["text"]
    assert "[delimiter text removed]" in item["citations"][0]["quote"]
    assert ("INJ-DELIM", "text") in prompt.neutralized
    assert ("INJ-DELIM", "quote") in prompt.neutralized


def test_instructions_and_reminder_are_byte_identical_for_injection_and_benign_input() -> None:
    benign = [
        {"key": f"B{i}", "kind": "claim", "text": f"Benign finding number {i}.", "citations": []}
        for i in range(len(INJECTION_FINDINGS))
    ]
    injection_prompt = _prompt(review_items(INJECTION_FINDINGS))
    benign_prompt = _prompt(benign)

    def split(text: str, nonce: str) -> tuple[str, str]:
        lines = text.splitlines()
        b, e = lines.index(begin_line(nonce)), lines.index(end_line(nonce))
        return "\n".join(lines[:b]), "\n".join(lines[e + 1 :])

    inj_before, inj_after = split(injection_prompt.text, NONCE)
    ben_before, ben_after = split(benign_prompt.text, NONCE)
    assert inj_before == ben_before
    assert inj_after == ben_after


# --------------------------------------------------------------------------- caps


def test_over_cap_findings_are_omitted_and_the_preamble_says_so() -> None:
    findings = [{"key": f"F{i}", "kind": "claim", "text": "x", "citations": []} for i in range(60)]
    prompt = _prompt(findings)
    assert len(prompt.finding_keys) == 50
    assert prompt.omitted == 10
    assert "50 finding(s) are included" in prompt.text
    assert "10 were left out" in prompt.text


def test_findings_stop_being_added_once_the_block_would_exceed_the_char_cap() -> None:
    findings = [
        {"key": f"BIG{i}", "kind": "claim", "text": "x" * 60_000, "citations": []} for i in range(5)
    ]
    prompt = _prompt(findings)
    assert len(prompt.finding_keys) == 2
    assert prompt.omitted == 3
    serialized = next(line for line in prompt.text.splitlines() if line.startswith("["))
    assert len(serialized) <= MAX_REVIEW_BLOCK_CHARS


def test_neutralized_names_only_findings_that_fit_under_the_char_cap() -> None:
    """A finding cut by the block cap was never shown to the reviewer, so its
    delimiter-shaped fields are not reported as neutralized."""
    findings = [
        {"key": f"BIG{i}", "kind": "claim", "text": "x" * 60_000, "citations": []} for i in range(2)
    ]
    findings.append(
        {
            "key": "CUT-DELIM",
            "kind": "claim",
            "text": "AGENTTAPE-UNTRUSTED-DATA " + "x" * 60_000,
            "citations": [],
        }
    )
    findings.append(
        {"key": "SMALL", "kind": "claim", "text": "AGENTTAPE-UNTRUSTED-DATA", "citations": []}
    )
    prompt = _prompt(findings)
    assert prompt.finding_keys == ("BIG0", "BIG1")
    assert prompt.omitted == 2
    assert prompt.neutralized == ()

    # The same delimiter text in a finding that does fit is still recorded.
    fitting = _prompt([findings[0], findings[3]])
    assert fitting.finding_keys == ("BIG0", "SMALL")
    assert fitting.neutralized == (("SMALL", "text"),)


def test_a_long_quote_is_cut_to_the_quote_cap_and_flagged() -> None:
    finding = {
        "key": "F1",
        "kind": "claim",
        "text": "x",
        "citations": [
            {
                "kind": "file",
                "path": "src/greeter.py",
                "inside_boundary": True,
                "line_start": 1,
                "line_end": 1,
                "quote": "y" * 3000,
                "event_ref": None,
            }
        ],
    }
    prompt = _prompt([finding])
    [item] = extract_untrusted_block(prompt.text, NONCE)
    [citation] = item["citations"]
    assert len(citation["quote"]) == MAX_REVIEW_QUOTE_CHARS
    assert citation["quote_cut"] is True


def test_over_cap_citations_are_omitted_and_flagged() -> None:
    finding = {
        "key": "F1",
        "kind": "claim",
        "text": "x",
        "citations": [
            {
                "kind": "file",
                "path": f"src/f{i}.py",
                "inside_boundary": True,
                "line_start": 1,
                "line_end": 1,
                "quote": None,
                "event_ref": None,
            }
            for i in range(12)
        ],
    }
    prompt = _prompt([finding])
    [item] = extract_untrusted_block(prompt.text, NONCE)
    assert len(item["citations"]) == MAX_REVIEW_CITATIONS
    assert item["citations_omitted"] == 2


# --------------------------------------------------------------------------- error paths


def test_empty_findings_raises() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _prompt([])


@pytest.mark.parametrize("bad_nonce", ["", "short", "NOTLOWERHEX012345", "g" * 16, "a" * 15])
def test_a_bad_nonce_raises(bad_nonce: str) -> None:
    with pytest.raises(ValueError):
        _prompt([{"key": "F1", "kind": "claim", "text": "x", "citations": []}], nonce=bad_nonce)


def test_extract_with_a_second_end_line_raises() -> None:
    prompt = _prompt([{"key": "F1", "kind": "claim", "text": "x", "citations": []}])
    tampered = prompt.text + "\n" + end_line(NONCE)
    with pytest.raises(ValueError):
        extract_untrusted_block(tampered, NONCE)


def test_extract_with_no_end_line_raises() -> None:
    prompt = _prompt([{"key": "F1", "kind": "claim", "text": "x", "citations": []}])
    lines = [line for line in prompt.text.splitlines() if line != end_line(NONCE)]
    with pytest.raises(ValueError):
        extract_untrusted_block("\n".join(lines), NONCE)


# ------------------------------------- reviewer contract vs. the MCP record_finding tool


def test_reviewer_instructions_name_the_mcp_tool_and_argument_names() -> None:
    """REVIEWER_INSTRUCTIONS must name the exact MCP tool and argument names a real
    Claude Code reviewer profile would need to follow, per the MCP server's own record_finding
    tool (backend/src/agenttape/mcp/server.py)."""
    text = REVIEWER_INSTRUCTIONS.format(target_run_id=TARGET_RUN_ID, included=1, omitted=0)
    assert "record_finding" in text
    for argument_name in (
        "kind",
        "text",
        "citations",
        "target",
        "run_id",
        "finding_key",
        "outcome",
    ):
        assert argument_name in text
    for outcome in REVIEW_OUTCOMES:
        assert outcome in text


def test_reviewer_contract_round_trips_through_the_mcp_record_finding() -> None:
    """A review call shaped exactly as capture's own ``FindingRecordIn``/``finding_payload``
    would build it - not a hand-written dict re-describing that schema - parses into a valid,
    linkable review target."""
    from agenttape.application.schemas.capture import (
        FileCitationIn,
        FindingRecordIn,
        ReviewTargetIn,
    )
    from agenttape.application.services.capture import finding_payload

    record = FindingRecordIn(
        kind="review",
        text="Upheld: the cited lines do say what F2 claims.",
        citations=[
            FileCitationIn(path="src/greeter.py", line_start=5, line_end=5, quote="Hello, x!")
        ],
        target=ReviewTargetIn(run_id="run-target-1", finding_key="F2", outcome="upheld"),
    )
    payload = finding_payload(record)
    draft = parse_review_target(payload)
    assert draft is not None
    assert draft.problem is None
    assert draft.run_id == "run-target-1"
    assert draft.finding_key == "F2"
    assert draft.outcome == "upheld"


def test_reviewer_contract_cannot_smuggle_a_citation_verdict_as_a_review_outcome() -> None:
    """Capture's own ``ReviewTargetIn.outcome`` is a ``Literal`` restricted to the review
    vocabulary, so a real ``record_finding`` call can never carry a citation verdict
    (``quote_matches``) as an outcome - the MCP tool itself refuses it before AgentTape's
    ingest ever sees it. This test bypasses that model-level validation (``model_construct``)
    to also prove ``review.py``'s own parser independently rejects an out-of-vocabulary
    outcome, as defense in depth."""
    import pydantic

    from agenttape.application.schemas.capture import FindingRecordIn, ReviewTargetIn
    from agenttape.application.services.capture import finding_payload

    with pytest.raises(pydantic.ValidationError):  # the real tool refuses it first
        ReviewTargetIn(run_id="run-target-1", finding_key="F2", outcome="quote_matches")

    bad_target = ReviewTargetIn.model_construct(
        run_id="run-target-1", finding_key="F2", outcome="quote_matches"
    )
    record = FindingRecordIn.model_construct(
        kind="review",
        text="x",
        citations=[],
        target=bad_target,
        session_id=None,
        cwd=None,
        instance_id=None,
    )
    payload = finding_payload(record)
    draft = parse_review_target(payload)
    assert draft is not None
    assert draft.problem == "invalid_outcome"
    assert draft.outcome_raw == "quote_matches"


def test_reviewer_instructions_do_not_contradict_the_launched_run_findings_contract() -> None:
    """The FINDINGS_PROMPT_CONTRACT (appended to every launched claude-code run,
    reviewer runs included) and REVIEWER_INSTRUCTIONS must agree: both send the agent
    through record_finding, never a competing or contradictory recording mechanism."""
    from agenttape.mcp.launch import FINDINGS_PROMPT_CONTRACT

    assert "record_finding" in FINDINGS_PROMPT_CONTRACT
    text = REVIEWER_INSTRUCTIONS.format(target_run_id=TARGET_RUN_ID, included=1, omitted=0)
    assert "record_finding" in text
    # Neither tells the agent to skip recording or use some other mechanism.
    for forbidden in ("do not record", "skip record_finding", "instead of record_finding"):
        assert forbidden not in FINDINGS_PROMPT_CONTRACT.lower()
        assert forbidden not in text.lower()
