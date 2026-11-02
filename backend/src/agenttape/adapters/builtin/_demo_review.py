"""The Demo Agent's scripted ``finding_review`` scenario: a no-model reviewer.

Reads the reviewer's own findings from the delimited block in its own prompt (never as
instructions - only structured fields, never an item's own text, are acted on), re-reads
each cited file in its own private clone of the AgentTape demo fixture (never the target
run's project, and never outside the clone), and records one ``kind="review"`` finding per
item with its own citation. Used by tests exercising Challenge findings end to end with no
model call.

``_``-prefixed: not auto-imported by anything scanning ``adapters/builtin/`` for adapter
classes; ``demo.py`` imports :func:`play_review` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agenttape.adapters.base import AdapterResult
from agenttape.context_evidence.common import OUTSIDE_PREFIX
from agenttape.domain.enums import FailureCategory
from agenttape.findings.normalize import to_cat_n
from agenttape.findings.review import extract_untrusted_block
from agenttape.security.paths import safe_join

if TYPE_CHECKING:
    from agenttape.adapters.builtin.demo import _DemoRun


def _line_of(content: str, index: int) -> int:
    """1-based line number of character offset ``index`` in ``content``."""
    return 1 + content.count("\n", 0, index)


def _first_file_citation(citations: list[Any]) -> dict[str, Any] | None:
    for citation in citations:
        if isinstance(citation, dict) and citation.get("path"):
            return citation
    return None


async def play_review(run: _DemoRun) -> AdapterResult:
    section = run.ctx.run.snapshot.get("finding_review")
    if not isinstance(section, dict) or not section.get("nonce"):
        return AdapterResult.failure(
            FailureCategory.CONFIG_ERROR,
            "The finding_review scenario reviews findings; start it with Challenge findings "
            "or a finding_review workflow node",
        )
    nonce = str(section["nonce"])
    target_run_id = section.get("target_run_id")
    try:
        items = extract_untrusted_block(run.ctx.prompt or "", nonce)
    except ValueError as exc:
        return AdapterResult.failure(FailureCategory.VALIDATION_FAILED, str(exc))

    await run.say(
        f"Scripted reviewer (model judgment, no model call): reviewing {len(items)} finding(s)."
    )
    clone = await run.prepare_clone()

    counts = {"upheld": 0, "refuted": 0, "inconclusive": 0}
    for position, item in enumerate(items, start=1):
        key = str(item.get("key") or "") if isinstance(item, dict) else ""
        citations = item.get("citations") if isinstance(item, dict) else None
        citation = _first_file_citation(citations if isinstance(citations, list) else [])
        outcome, text, links = await _review_one(run, clone, citation)
        counts[outcome] += 1
        await run.record(
            {
                "type": "finding",
                "id": f"R{position}",
                "kind": "review",
                "text": text,
                "target": {"run_id": target_run_id, "finding_key": key, "outcome": outcome},
                "links": links,
            }
        )

    input_tokens = 200 + 40 * len(items)
    output_tokens = 60 + 20 * len(items)
    await run.record(
        {"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens}
    )
    return AdapterResult.success(
        exit_code=0,
        outputs={"reviewed": len(items), **counts},
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens, "simulated": True},
    )


async def _review_one(
    run: _DemoRun, clone: Path, citation: dict[str, Any] | None
) -> tuple[str, str, list[dict[str, Any]]]:
    if (
        citation is None
        or citation.get("inside_boundary") is False
        or str(citation.get("path", "")).startswith(OUTSIDE_PREFIX)
    ):
        return (
            "inconclusive",
            "Inconclusive: no in-boundary file citation to independently check.",
            [],
        )
    path = str(citation["path"])
    try:
        target = safe_join(clone, path)
        content = target.read_text(encoding="utf-8")
    except Exception:
        return "inconclusive", f"Inconclusive: could not read {path} in the fixture clone.", []
    await run.tool("Read", {"file_path": path}, to_cat_n(content.splitlines()))
    quote = citation.get("quote")
    if isinstance(quote, str) and quote and quote in content:
        idx = content.index(quote)
        line_start = _line_of(content, idx)
        line_end = line_start + quote.count("\n")
        text = f"Upheld: {path} lines {line_start}-{line_end} match the cited quote."
        return (
            "upheld",
            text,
            [{"path": path, "line_start": line_start, "line_end": line_end, "quote": quote}],
        )
    lines = content.splitlines()
    cited_start, cited_end = citation.get("line_start"), citation.get("line_end")
    if (
        isinstance(cited_start, int)
        and isinstance(cited_end, int)
        and 1 <= cited_start <= cited_end <= len(lines)
    ):
        line_start, line_end = cited_start, cited_end
    else:
        line_start, line_end = 1, max(1, min(3, len(lines)))
    actual = "\n".join(lines[line_start - 1 : line_end])
    text = f"Refuted: {path} lines {line_start}-{line_end} do not match the cited quote."
    return (
        "refuted",
        text,
        [{"path": path, "line_start": line_start, "line_end": line_end, "quote": actual}],
    )
