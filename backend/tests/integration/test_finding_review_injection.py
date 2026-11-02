"""End-to-end proof: the seeded prompt-injection fixture, challenged through the
real API and worker, stays inside its delimited block in what AgentTape stores and in what
the adapter actually receives; a scripted, no-model reviewer disagrees genuinely and ignores
every embedded instruction; the reviewer's own citations get the verification engine's real verdicts.

These tests prove how AgentTape builds and delivers the reviewer's prompt - finding text and
cited content reach the reviewer only inside the nonce-delimited, escaped JSON block - and
that a scripted (no-model) reviewer can disagree with a finding through the real pipeline.
They do not prove that a real language model resists prompt injection: no test here calls a
model, and a passing test is not evidence about a
real model's behavior.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.services.profiles import AgentProfileService
from agenttape.domain.prompts import template_tokens
from agenttape.findings.review import begin_line, end_line, extract_untrusted_block
from tests.conftest import demo_profile, drive
from tests.fixtures.review_injection import (
    CANARY_SECRET_NAME,
    EXPECTED_SCRIPTED_OUTCOMES,
    INJECTION_FINDINGS,
    SENTINELS,
)
from tests.integration.helpers import run_findings
from tests.integration.test_context_evidence import ScriptedAdapter, run_with


def _injection_records() -> list[dict[str, Any]]:
    return [{"type": "finding", "payload": item} for item in INJECTION_FINDINGS]


def test_injection_fixture_stays_inside_the_block_in_the_stored_prompt(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(ctx, repo_project, _injection_records())
    reviewer_profile_id = demo_profile(ctx, "finding_review")

    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]

    detail = client.get(f"/api/v1/runs/{child_id}").json()
    section = detail["snapshot"]["finding_review"]
    nonce = section["nonce"]
    rendered_prompt = detail["rendered_prompt"]
    stored_body = detail["snapshot"]["prompt"]["body"]
    assert rendered_prompt is not None
    assert stored_body is not None

    begin, end = begin_line(nonce), end_line(nonce)
    for prompt_text in (rendered_prompt, stored_body):
        lines = prompt_text.splitlines()
        assert lines.count(begin) == 1
        assert lines.count(end) == 1
        b, e = lines.index(begin), lines.index(end)
        before, after = "\n".join(lines[:b]), "\n".join(lines[e + 1 :])
        for sentinel in SENTINELS:
            assert sentinel not in before
            assert sentinel not in after
        variables, secret_refs = template_tokens(prompt_text)
        assert variables == []
        assert secret_refs == []

    items = extract_untrusted_block(rendered_prompt, nonce)
    assert len(items) == len(INJECTION_FINDINGS)

    neutralized = {(n["finding_key"], n["field"]) for n in section["neutralized"]}
    assert ("INJ-DELIM", "text") in neutralized
    assert ("INJ-DELIM", "quote") in neutralized


def test_the_adapter_never_receives_a_secret_the_fixture_references(
    ctx: AppContext, client: TestClient, repo_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary_value = "canary-" + secrets.token_hex(16)
    monkeypatch.setenv(CANARY_SECRET_NAME, canary_value)

    # Control: an ordinary run whose inline prompt names the canary secret unescaped - proves
    # this check is not vacuous. If the reference did not resolve, this assertion would fail
    # first, before the challenge's own resolved prompt is ever inspected below.
    run_with(ctx, repo_project, [], prompt=f"Handle {{{{ secret:{CANARY_SECRET_NAME} }}}}.")
    assert canary_value in (ScriptedAdapter.prompts[-1] or "")

    target_run_id = run_with(ctx, repo_project, _injection_records())
    reviewer_profile_id = (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(
                name="canary-reviewer", adapter_type="test_context", config={"records": []}
            )
        )
        .id
    )
    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]

    detail = client.get(f"/api/v1/runs/{child_id}").json()
    nonce = detail["snapshot"]["finding_review"]["nonce"]
    stored_prompt = detail["rendered_prompt"]
    assert stored_prompt is not None

    drive(ctx, child_id)

    adapter_prompt = ScriptedAdapter.prompts[-1]
    assert adapter_prompt is not None
    assert canary_value not in adapter_prompt
    assert begin_line(nonce) in adapter_prompt
    assert extract_untrusted_block(adapter_prompt, nonce) == extract_untrusted_block(
        stored_prompt, nonce
    )


def test_scripted_reviewer_disagrees_and_ignores_instructions(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(ctx, repo_project, _injection_records())
    reviewer_profile_id = demo_profile(ctx, "finding_review")

    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]
    drive(ctx, child_id)

    challenges = client.get(f"/api/v1/runs/{target_run_id}/challenges").json()
    assert len(challenges) == 1
    [challenge] = challenges
    assert challenge["judgment"] == "model judgment"
    assert challenge["counts_as_proof"] is False
    outcomes_by_key = {o["target_finding_key"]: o for o in challenge["outcomes"]}
    assert set(outcomes_by_key) == set(EXPECTED_SCRIPTED_OUTCOMES)
    for key, expected_outcome in EXPECTED_SCRIPTED_OUTCOMES.items():
        assert outcomes_by_key[key]["outcome"] == expected_outcome, key

    events = client.get(f"/api/v1/runs/{child_id}/events", params={"limit": 1000}).json()["items"]
    for event in events:
        if event["type"] != "tool_call" or event["payload"].get("tool") != "Read":
            continue
        path = str((event["payload"].get("input") or {}).get("file_path") or "")
        assert "passwd" not in path
        assert not path.startswith("/")

    child_findings = client.get(f"/api/v1/runs/{child_id}/context/findings").json()
    review_findings = [f for f in child_findings if f["kind"] == "review"]
    assert review_findings
    for finding in review_findings:
        for sentinel in SENTINELS:
            assert sentinel not in finding["text"]


def test_reviewer_citations_are_verified_like_any_other(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(ctx, repo_project, _injection_records())
    reviewer_profile_id = demo_profile(ctx, "finding_review")

    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]
    drive(ctx, child_id)

    findings = run_findings(ctx, child_id)
    review_findings = [f for f in findings if f["kind"] == "review"]
    assert review_findings

    f_up_verdicts: list[str] = []
    for finding in review_findings:
        review = finding["details"]["review"]
        for citation in finding["citations"]:
            verdict = citation["verdict"]
            assert verdict is not None, f"{finding['key']} citation has no verdict"
            assert verdict["method"] != "seeded"
            if review.get("target_finding_key") == "F-UP":
                f_up_verdicts.append(verdict["verdict"])
    assert f_up_verdicts
    assert all(v == "quote_matches" for v in f_up_verdicts)
