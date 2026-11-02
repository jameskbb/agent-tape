"""Side-by-side comparison of two runs (``GET /runs/{a}/compare/{b}``).

The data comes from :class:`RunService` (detail and events). The diff helpers below are
pure so they can be unit tested. Unknown values stay ``None``: a run without reported
usage is "unavailable", simulated usage is labelled "simulated", and no delta is invented
from either.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from agenttape.application.context import AppContext
from agenttape.application.schemas.git import Comparison, RunSide
from agenttape.application.schemas.runs import RunDetail
from agenttape.application.services.runs import RunService

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def usage_status(usage: dict[str, Any] | None) -> str:
    """``reported``, ``simulated``, or ``unavailable`` (never a made-up zero)."""
    if not usage or not any(isinstance(usage.get(k), int | float) for k in USAGE_KEYS):
        return "unavailable"
    return "simulated" if usage.get("simulated") else "reported"


def numeric_delta(a: float | int | None, b: float | int | None) -> float | int | None:
    if a is None or b is None:
        return None
    return b - a


def count_diff(a: dict[str, int], b: dict[str, int]) -> list[dict[str, Any]]:
    keys = sorted(set(a) | set(b), key=lambda k: (-(a.get(k, 0) + b.get(k, 0)), k))
    return [
        {"key": k, "a": a.get(k, 0), "b": b.get(k, 0), "delta": b.get(k, 0) - a.get(k, 0)}
        for k in keys
    ]


def dict_diff(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """Keys whose values differ (missing keys count as ``None``)."""
    return [
        {"key": k, "a": a.get(k), "b": b.get(k)}
        for k in sorted(set(a) | set(b))
        if a.get(k) != b.get(k)
    ]


def named_diff(
    a: list[dict[str, Any]], b: list[dict[str, Any]], *, key: str, fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Match two lists by ``key`` and report each item's fields on both sides."""
    left = {str(x.get(key)): x for x in a}
    right = {str(x.get(key)): x for x in b}
    out: list[dict[str, Any]] = []
    for name in sorted(set(left) | set(right)):
        la, rb = left.get(name), right.get(name)
        entry: dict[str, Any] = {key: name, "in_a": la is not None, "in_b": rb is not None}
        changed = la is None or rb is None
        for f in fields:
            va = la.get(f) if la else None
            vb = rb.get(f) if rb else None
            entry[f"a_{f}"] = va
            entry[f"b_{f}"] = vb
            if va != vb:
                changed = True
        entry["changed"] = changed
        out.append(entry)
    return out


def usage_diff(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    sa, sb = usage_status(a), usage_status(b)
    rows = []
    for k in USAGE_KEYS:
        va = a.get(k) if a and sa != "unavailable" else None
        vb = b.get(k) if b and sb != "unavailable" else None
        if va is None and vb is None:
            continue
        rows.append({"key": k, "a": va, "b": vb, "delta": numeric_delta(va, vb)})
    return {"a_status": sa, "b_status": sb, "rows": rows}


def relation(a: RunDetail, b: RunDetail) -> str | None:
    if b.run.parent_run_id == a.run.id:
        return f"b_is_{b.run.parent_relation or 'child'}_of_a"
    if a.run.parent_run_id == b.run.id:
        return f"a_is_{a.run.parent_relation or 'child'}_of_b"
    if a.run.parent_run_id and a.run.parent_run_id == b.run.parent_run_id:
        return "siblings"
    return None


def _git_side(detail: RunDetail) -> dict[str, Any]:
    changes = detail.file_changes
    if changes:
        attempts = {a.id: a.attempt_number for a in detail.attempts}
        latest = max(attempts.get(c.attempt_id or "", 0) for c in changes)
        changes = [c for c in changes if attempts.get(c.attempt_id or "", 0) == latest]
    snap_git = detail.snapshot.get("git") or {}
    return {
        "files": len(changes),
        "insertions": sum(c.insertions or 0 for c in changes),
        "deletions": sum(c.deletions or 0 for c in changes),
        "protected": sum(1 for c in changes if c.is_protected),
        "binary": sum(1 for c in changes if c.is_binary),
        "head": snap_git.get("head"),
        "branch": snap_git.get("branch"),
        "dirty": snap_git.get("dirty"),
        "changes": [
            {
                "path": c.path,
                "change_type": c.change_type,
                "insertions": c.insertions,
                "deletions": c.deletions,
                "is_binary": c.is_binary,
                "is_protected": c.is_protected,
            }
            for c in changes
        ],
    }


def build_side(detail: RunDetail, event_types: dict[str, int]) -> RunSide:
    snap = detail.snapshot
    prompt = snap.get("prompt") or None
    profile = snap.get("agent_profile") or None
    return RunSide(
        run=detail.run,
        attempts=len(detail.attempts),
        event_count=detail.event_count,
        event_types=event_types,
        usage=detail.run.usage,
        usage_status=usage_status(detail.run.usage),
        cost=detail.run.cost,
        evaluations=[
            {
                "name": e.name,
                "status": e.status,
                "required": e.required,
                "evaluator_type": e.evaluator_type,
                "explanation": e.explanation,
            }
            for e in detail.evaluations
        ],
        artifacts=[
            {"name": a.name, "kind": a.kind, "size_bytes": a.size_bytes, "sha256": a.sha256}
            for a in detail.artifacts
        ],
        git=_git_side(detail),
        prompt=(
            {
                "template_id": prompt.get("template_id"),
                "name": prompt.get("name") or ("Inline prompt" if prompt.get("inline") else None),
                "version_id": prompt.get("version_id"),
                "version": prompt.get("version"),
                "content_hash": prompt.get("content_hash"),
                "rendered": prompt.get("rendered"),
            }
            if prompt
            else None
        ),
        profile=(
            {
                "profile_id": profile.get("profile_id"),
                "name": profile.get("name"),
                "version_id": profile.get("version_id"),
                "version": profile.get("version"),
                "adapter_type": profile.get("adapter_type"),
                "config_hash": profile.get("config_hash"),
                "config": profile.get("config") or {},
            }
            if profile
            else None
        ),
        inputs=dict(detail.inputs),
    )


def compare_sides(a: RunSide, b: RunSide) -> dict[str, Any]:
    """Every difference the compare view highlights, computed from two sides."""
    pa, pb = a.prompt or {}, b.prompt or {}
    fa, fb = a.profile or {}, b.profile or {}
    return {
        "status": {"a": a.run.status, "b": b.run.status, "same": a.run.status == b.run.status},
        "duration_ms": {
            "a": a.run.duration_ms,
            "b": b.run.duration_ms,
            "delta": numeric_delta(a.run.duration_ms, b.run.duration_ms),
        },
        "attempts": {"a": a.attempts, "b": b.attempts, "delta": b.attempts - a.attempts},
        "events": {
            "a": a.event_count,
            "b": b.event_count,
            "delta": b.event_count - a.event_count,
        },
        "event_types": count_diff(a.event_types, b.event_types),
        "usage": usage_diff(a.usage, b.usage),
        "cost": {"a": a.cost, "b": b.cost},
        "evaluations": named_diff(
            a.evaluations, b.evaluations, key="name", fields=("status", "required")
        ),
        "artifacts": named_diff(
            a.artifacts, b.artifacts, key="name", fields=("size_bytes", "sha256")
        ),
        "files": named_diff(
            a.git["changes"],
            b.git["changes"],
            key="path",
            fields=("change_type", "insertions", "deletions", "is_protected"),
        ),
        "git": {
            k: {"a": a.git.get(k), "b": b.git.get(k)}
            for k in ("files", "insertions", "deletions", "protected", "head")
        },
        "prompt": {
            "same_template": pa.get("template_id") == pb.get("template_id"),
            "same_version": pa.get("version_id") == pb.get("version_id"),
            "same_content": pa.get("content_hash") == pb.get("content_hash"),
            "same_rendered": pa.get("rendered") == pb.get("rendered"),
            "a_version": pa.get("version"),
            "b_version": pb.get("version"),
        },
        "profile": {
            "same_profile": fa.get("profile_id") == fb.get("profile_id"),
            "same_version": fa.get("version_id") == fb.get("version_id"),
            "a_version": fa.get("version"),
            "b_version": fb.get("version"),
            "config": dict_diff(fa.get("config") or {}, fb.get("config") or {}),
        },
        "inputs": dict_diff(a.inputs, b.inputs),
    }


class ComparisonService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.runs = RunService(ctx)

    def _event_types(self, run_id: str) -> dict[str, int]:
        counts: Counter[str] = Counter()
        after = 0
        while True:
            page = self.runs.list_events(run_id, after_sequence=after, limit=1000)
            counts.update(e.type for e in page.items)
            if page.next_after_sequence is None:
                break
            after = page.next_after_sequence
        return dict(counts)

    def compare(self, a_id: str, b_id: str) -> Comparison:
        a_detail = self.runs.get_detail(a_id)
        b_detail = self.runs.get_detail(b_id)
        a = build_side(a_detail, self._event_types(a_id))
        b = build_side(b_detail, self._event_types(b_id))
        return Comparison(
            a=a, b=b, relation=relation(a_detail, b_detail), differences=compare_sides(a, b)
        )
