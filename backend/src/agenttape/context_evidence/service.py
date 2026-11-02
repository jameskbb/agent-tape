"""Read models over context evidence, and the reviewed learning loop.

Every summary is computed from the append-only rows on read, in a fixed order, so the
same rows always produce the same summary (no stored aggregates to drift or duplicate).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, like_pattern
from agenttape.application.services.findings import FindingsService
from agenttape.context_evidence.common import (
    CLASSIFICATIONS,
    DESTINATIONS,
    FEEDBACK_KINDS,
    INJECTED_DESTINATIONS,
    PROPOSAL,
    STAGES,
    sanitize,
)
from agenttape.domain.enums import EventType
from agenttape.domain.errors import ConflictError, NotFoundError, ValidationError
from agenttape.infrastructure.clock import Clock
from agenttape.infrastructure.db.extra_models.context_evidence import (
    ContextEvidenceEvent,
    ContextFeedback,
    ContextPack,
    ContextSource,
    EvidenceLink,
)
from agenttape.infrastructure.db.models import (
    EvaluationResult,
    Event,
    Project,
    PromptTemplate,
    PromptVersion,
    Run,
)

MAX_PAGE = 200
#: Stated on every summary and export; see docs/CONTEXT-EVIDENCE.md.
PROVENANCE_STATEMENT = (
    "AgentTape records what was available, what it selected into the context pack, what "
    "it injected into the agent's input itself (the rendered prompt and approved lessons), "
    "what the adapter or agent reported retrieving, and what the agent's structured output "
    "cited. It cannot see hidden model reasoning, provider-internal prompts, or which "
    "supplied items a model attended to."
)


log = logging.getLogger(__name__)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def source_out(row: ContextSource) -> dict[str, Any]:
    return {
        "id": row.id,
        "key": row.source_key,
        "type": row.source_type,
        "name": row.name,
        "provider": row.provider,
        "locator": row.locator,
        "authority": row.authority,
        "live": row.live,
        "version": row.version,
        "synced_at": row.synced_at,
        "sensitivity": row.sensitivity,
        "retention": row.retention,
        "first_seen_at": _iso(row.first_seen_at),
        "last_seen_at": _iso(row.last_seen_at),
    }


def evidence_out(row: ContextEvidenceEvent, cited: set[str] | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "ts": _iso(row.ts),
        "sequence": row.sequence,
        "position": row.position,
        "attempt_id": row.attempt_id,
        "node_key": row.node_key,
        "event_id": row.event_id,
        "stage": row.stage,
        "source_id": row.source_id,
        "source_key": row.source_key,
        "source_type": row.source_type,
        "source_name": row.source_name,
        "provider": row.provider,
        "authority": row.authority,
        "live": row.live,
        "freshness": row.freshness,
        "version": row.version,
        "synced_at": row.synced_at,
        "quality": row.quality,
        "query": row.query,
        "tool": row.tool,
        "result": row.result,
        "reason": row.reason,
        "details": row.details or {},
        "cited_in_run": bool(cited is not None and row.source_key in cited),
    }


def pack_out(pack: ContextPack) -> dict[str, Any]:
    return {
        "id": pack.id,
        "run_id": pack.run_id,
        "attempt_id": pack.attempt_id,
        "node_key": pack.node_key,
        "version": pack.version,
        "policy": pack.policy,
        "policy_version": pack.policy_version,
        "created_at": _iso(pack.created_at),
        "content_hash": pack.content_hash,
        "token_estimate": pack.token_estimate,
        "token_method": pack.token_method,
        "token_status": "reported" if pack.token_estimate is not None else "unavailable",
        "items": pack.items,
        "excluded": pack.excluded,
        "redaction": pack.redaction,
        "injected_text": pack.injected_text,
    }


def feedback_out(row: ContextFeedback) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "project_id": row.project_id,
        "kind": row.kind,
        "text": row.text,
        "source_key": row.source_key,
        "event_id": row.event_id,
        "reported_by": row.reported_by,
        "suggested_classification": row.suggested_classification,
        "status": row.status,
        "classification": row.classification,
        "proposed_destination": row.proposed_destination,
        "injects": row.proposed_destination in INJECTED_DESTINATIONS,
        "lesson_text": row.lesson_text,
        "lesson_version": row.lesson_version,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": _iso(row.reviewed_at),
        "review_note": row.review_note,
        "created_at": _iso(row.created_at),
    }


def _cited_keys(session: Session, run_id: str) -> set[str]:
    return {
        k
        for k in session.scalars(
            select(ContextEvidenceEvent.source_key).where(
                ContextEvidenceEvent.run_id == run_id, ContextEvidenceEvent.stage == "cited"
            )
        )
        if k
    }


def _usage(run: Run) -> dict[str, Any]:
    usage = dict(run.usage or {})
    if not usage:
        return {"status": "unavailable", "values": {}}
    values = {k: v for k, v in usage.items() if isinstance(v, int | float) and "token" in k}
    return {"status": "simulated" if usage.get("simulated") else "reported", "values": values}


def _window(session: Session, run_id: str) -> dict[str, Any]:
    """The evidence window is incomplete when AgentTape was offline during the run
    (reported by a runtime reconnect event); nothing is fabricated for the gap."""
    gaps = []
    for ev in session.scalars(
        select(Event)
        .where(Event.run_id == run_id, Event.type == EventType.RUNTIME.value)
        .order_by(Event.sequence)
    ):
        p = ev.payload or {}
        if p.get("action") == "reconnect":
            gaps.append({"offline_from": p.get("offline_from"), "offline_to": p.get("offline_to")})
    return {"complete": not gaps, "offline": gaps}


# --------------------------------------------------------------------------- summary


#: Cap on the individual records a summary lists (warnings, conflicts, gaps, exclusions).
MAX_LISTED = 200


def run_summary(session: Session, run_id: str) -> dict[str, Any]:
    run = get_or_404(session, Run, run_id, "run")
    E = ContextEvidenceEvent
    packs = list(
        session.scalars(
            select(ContextPack).where(ContextPack.run_id == run_id).order_by(ContextPack.version)
        )
    )
    latest = packs[-1] if packs else None
    per_stage = {
        stage: (int(n), int(sources))
        for stage, n, sources in session.execute(
            select(E.stage, func.count(), func.count(func.distinct(E.source_key)))
            .where(E.run_id == run_id)
            .group_by(E.stage)
        )
    }

    def distinct_sources(live: bool) -> int:
        return int(
            session.scalar(
                select(func.count(func.distinct(E.source_key))).where(
                    E.run_id == run_id, E.live.is_(live), E.source_key.is_not(None)
                )
            )
            or 0
        )

    def listed(*where: Any) -> list[ContextEvidenceEvent]:
        return list(
            session.scalars(
                select(E)
                .where(E.run_id == run_id, *where)
                .order_by(E.ts, E.sequence)
                .limit(MAX_LISTED)
            )
        )

    snap = dict(run.snapshot or {})
    prompt = dict(snap.get("prompt") or {})
    profile = dict(snap.get("agent_profile") or {})
    items = latest.items if latest else []

    def of(kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        return [i for i in items if i.get("kind") in kinds]

    prompt_item = next(iter(of(("prompt",))), None)
    trigger_item = next(iter(of(("trigger",))), None)
    feedback = list(
        session.scalars(
            select(ContextFeedback)
            .where(ContextFeedback.run_id == run_id)
            .order_by(ContextFeedback.created_at, ContextFeedback.id)
        )
    )
    findings = session.scalar(
        select(func.count()).where(Event.run_id == run_id, Event.type == EventType.FINDING.value)
    )
    return {
        "run_id": run_id,
        "status": run.status,
        "statement": PROVENANCE_STATEMENT,
        "trigger": trigger_item["details"] if trigger_item else {"source": run.source},
        "original_request": (prompt_item or {}).get("details", {}).get("original_request"),
        "prompt": {
            "name": prompt.get("name") or ("Inline prompt" if prompt.get("inline") else None),
            "version": prompt.get("version"),
            "template_hash": prompt.get("content_hash"),
            "rendered_hash": (prompt_item or {}).get("content_hash"),
            "stage": (prompt_item or {}).get("stage"),
        }
        if prompt
        else None,
        "agent": {
            "profile": profile.get("name"),
            "profile_version": profile.get("version"),
            "adapter_type": run.adapter_type,
            "model": (profile.get("config") or {}).get("model"),
        },
        "pack": {
            "id": latest.id,
            "version": latest.version,
            "versions": len(packs),
            "policy": f"{latest.policy} v{latest.policy_version}",
            "content_hash": latest.content_hash,
            "created_at": _iso(latest.created_at),
            "items": len(latest.items),
            "excluded": len(latest.excluded),
            "token_estimate": latest.token_estimate,
            "token_status": "reported" if latest.token_estimate is not None else "unavailable",
        }
        if latest
        else None,
        "instructions": of(("instruction_file", "planning_ref")),
        "skills": of(("skill",)),
        "lessons": of(("lesson",)),
        "excluded": (latest.excluded if latest else [])
        + [
            {"kind": r.source_type, "name": r.source_name, "reason": r.reason}
            for r in listed(E.stage == "excluded", E.quality != "assembled")
        ],
        "stages": [
            {
                "stage": stage,
                "records": per_stage.get(stage, (0, 0))[0],
                "sources": per_stage.get(stage, (0, 0))[1],
            }
            for stage in STAGES
        ],
        "live_sources": distinct_sources(True),
        "snapshot_sources": distinct_sources(False),
        "freshness_warnings": [evidence_out(r) for r in listed(E.stage == "stale")],
        "conflicts": [evidence_out(r) for r in listed(E.stage == "conflict")],
        "gaps": [evidence_out(r) for r in listed(E.stage.in_(("gap", "unavailable")))],
        "findings": int(findings or 0),
        "feedback": [feedback_out(f) for f in feedback],
        "usage": _usage(run),
        "window": _window(session, run_id),
    }


def list_packs(session: Session, run_id: str) -> list[dict[str, Any]]:
    get_or_404(session, Run, run_id, "run")
    return [
        pack_out(p)
        for p in session.scalars(
            select(ContextPack).where(ContextPack.run_id == run_id).order_by(ContextPack.version)
        )
    ]


# --------------------------------------------------------------------------- explorer


def list_evidence(
    session: Session,
    run_id: str,
    *,
    stage: str | None = None,
    source_type: str | None = None,
    provider: str | None = None,
    node_key: str | None = None,
    authority: str | None = None,
    freshness: str | None = None,
    quality: str | None = None,
    source_id: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    get_or_404(session, Run, run_id, "run")
    E = ContextEvidenceEvent
    base = select(E).where(E.run_id == run_id)
    for column, value in (
        (E.stage, stage),
        (E.source_type, source_type),
        (E.provider, provider),
        (E.node_key, node_key),
        (E.authority, authority),
        (E.freshness, freshness),
        (E.quality, quality),
        (E.source_id, source_id),
    ):
        if value:
            base = base.where(column == value)
    if q and q.strip():
        like = like_pattern(q.strip()[:200])
        columns = (E.source_name, E.source_key, E.query, E.reason, E.provider)
        base = base.where(or_(*(func.lower(c).like(like, escape="\\") for c in columns)))
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    limit = max(1, min(limit, MAX_PAGE))
    rows = session.scalars(
        base.order_by(E.ts, E.sequence, E.position, E.id).limit(limit).offset(offset)
    )
    cited = _cited_keys(session, run_id)

    def facet(column: Any) -> list[str]:
        return sorted(
            v
            for v in session.scalars(select(column).where(E.run_id == run_id).distinct())
            if v is not None
        )

    return {
        "items": [evidence_out(r, cited) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < total else None,
        "facets": {
            "stage": [s for s in STAGES if s in set(facet(E.stage))],
            "source_type": facet(E.source_type),
            "provider": facet(E.provider),
            "node_key": facet(E.node_key),
            "authority": facet(E.authority),
            "freshness": facet(E.freshness),
            "quality": facet(E.quality),
        },
    }


def source_detail(session: Session, run_id: str, source_id: str) -> dict[str, Any]:
    run = get_or_404(session, Run, run_id, "run")
    source = get_or_404(session, ContextSource, source_id, "context source")
    rows = list(
        session.scalars(
            select(ContextEvidenceEvent)
            .where(
                ContextEvidenceEvent.run_id == run_id,
                ContextEvidenceEvent.source_id == source_id,
            )
            .order_by(ContextEvidenceEvent.ts, ContextEvidenceEvent.sequence)
        )
    )
    links = list(
        session.scalars(
            select(EvidenceLink)
            .where(EvidenceLink.run_id == run_id, EvidenceLink.source_id == source_id)
            .order_by(EvidenceLink.created_at)
        )
    )
    other_runs = session.scalar(
        select(func.count(func.distinct(ContextEvidenceEvent.run_id))).where(
            ContextEvidenceEvent.source_id == source_id, ContextEvidenceEvent.run_id != run.id
        )
    )
    excerpts = []
    for pack in session.scalars(select(ContextPack).where(ContextPack.run_id == run_id)):
        for item in pack.items:
            if item.get("source_key") == source.source_key and item.get("excerpt"):
                excerpts.append(
                    {"pack_version": pack.version, "text": item["excerpt"], "redacted": True}
                )
    return {
        "source": source_out(source),
        "freshness": rows[-1].freshness if rows else "unknown",
        "access": [evidence_out(r) for r in rows],
        "links": [
            {
                "subject_type": ln.subject_type,
                "subject_key": ln.subject_key,
                "subject_text": ln.subject_text,
                "relation": ln.relation,
                "origin": ln.origin,
            }
            for ln in links
        ],
        "other_runs": int(other_runs or 0),
        "excerpts": excerpts,
        "file_path": source.locator if source.source_type == "repository_file" else None,
    }


def findings(session: Session, run_id: str) -> list[dict[str, Any]]:
    """Findings with their supporting/conflicting/missing legacy links, citations, and each
    citation's latest verdict and check count. ``FindingsService`` is the source of a
    finding's identity (``key``, disambiguated on a re-used payload id) and citations;
    legacy ``supports``/``conflicts``/``missing`` links are grouped by the origin FINDING
    event id, which is unique even when two findings share a payload id."""
    get_or_404(session, Run, run_id, "run")
    links = list(
        session.scalars(
            select(EvidenceLink)
            .where(EvidenceLink.run_id == run_id)
            .order_by(EvidenceLink.created_at, EvidenceLink.id)
        )
    )
    sources = {
        s.id: s
        for s in session.scalars(
            select(ContextSource).where(
                ContextSource.id.in_({ln.source_id for ln in links if ln.source_id})
            )
        )
    }
    out: dict[str, dict[str, Any]] = {}
    for f in FindingsService.findings_for_run(session, run_id):
        out[f["event_id"]] = {
            "key": f["key"],
            "kind": f["kind"],
            "text": f["text"],
            "source": f["source"],
            "event_id": f["event_id"],
            "ts": f["ts"],
            "supports": [],
            "conflicts": [],
            "missing": [],
            "id": f["id"],
            "citations": f["citations"],
            "declared_only": f["declared_only"],
            "details": f["details"],
            "confirmation": f["confirmation"],
        }
    for ln in links:
        # Every FINDING event has a Finding row (the normalizer writes a legacy link only
        # after FindingsService.ingest_event returned one, and backfill_findings gives the
        # events of runs recorded before the findings table theirs when the worker starts),
        # so a miss is an orphan not yet back-filled: skip it rather than invent a finding
        # entry. Logged at debug, since every read of the run would repeat it.
        entry = out.get(ln.event_id or "")
        if entry is None:
            log.debug("context findings: link %s names no finding row; skipped", ln.id)
            continue
        src = sources.get(ln.source_id or "")
        entry[ln.relation].append(
            {
                "source_id": ln.source_id,
                "key": ln.source_key,
                "name": src.name if src else ln.source_key,
                "type": src.source_type if src else "unknown",
                "live": src.live if src else None,
                "authority": src.authority if src else "unknown",
                "origin": ln.origin,
            }
        )
    return list(out.values())


# --------------------------------------------------------------------------- comparison


def _side(session: Session, run_id: str) -> dict[str, Any]:
    run = get_or_404(session, Run, run_id, "run")
    E = ContextEvidenceEvent
    pack = session.scalars(
        select(ContextPack).where(ContextPack.run_id == run_id).order_by(ContextPack.version.desc())
    ).first()
    stages: dict[str, set[str]] = defaultdict(set)
    names: dict[str, str] = {}
    versions: dict[str, tuple[str | None, str]] = {}
    queries: set[str] = set()
    gaps: set[str] = set()
    cited: set[str] = set()
    mcp: dict[str, str | None] = {}
    # Only the columns a comparison needs (never the result/details JSON of every row).
    for key, stage, name, version, freshness, query, reason, source_type, tool in session.execute(
        select(
            E.source_key,
            E.stage,
            E.source_name,
            E.version,
            E.freshness,
            E.query,
            E.reason,
            E.source_type,
            E.tool,
        )
        .where(E.run_id == run_id)
        .order_by(E.ts, E.sequence)
    ):
        if query:
            queries.add(query)
        if stage in ("gap", "unavailable"):
            # The reason names the gap; one source can carry several (a reported gap and a
            # finding's "missing" link), and only the reported one may be resolved later.
            gaps.add(f"{stage}: {reason or name}")
        if not key:
            continue
        stages[key].add(stage)
        names[key] = name or key
        # the latest reported version wins; records without one keep the earlier value
        if version or key not in versions:
            versions[key] = (version, freshness)
        if stage == "cited":
            cited.add(key)
        if source_type in ("mcp_tool", "mcp_server") or (tool or {}).get("server"):
            # agent-declared availability carries no tool payload: its stage is the status
            mcp[key] = (tool or {}).get("status") or stage
    items = pack.items if pack else []
    for item in items:
        if item.get("kind") == "mcp_server":
            mcp[item["source_key"]] = "available"
    return {
        "run": run,
        "stages": stages,
        "names": names,
        "versions": versions,
        "queries": sorted(queries),
        "mcp": mcp,
        "gaps": sorted(gaps),
        "cited": sorted(cited),
        "instructions": {
            i["name"]: i.get("content_hash")
            for i in items
            if i.get("kind") in ("instruction_file", "planning_ref", "skill")
        },
        "lessons": [i["name"] for i in items if i.get("kind") == "lesson"],
        "prompt": next((i for i in items if i.get("kind") == "prompt"), None),
        "findings": sorted(f["text"] or "" for f in findings(session, run_id)),
        "pack_hash": pack.content_hash if pack else None,
    }


def compare(session: Session, a_id: str, b_id: str) -> dict[str, Any]:
    a, b = _side(session, a_id), _side(session, b_id)
    keys = sorted(set(a["stages"]) | set(b["stages"]))
    sources = []
    for k in keys:
        sa, sb = sorted(a["stages"].get(k, ())), sorted(b["stages"].get(k, ()))
        change = "added" if not sa else "removed" if not sb else "changed" if sa != sb else "same"
        sources.append(
            {
                "key": k,
                "name": b["names"].get(k) or a["names"].get(k) or k,
                "a_stages": sa,
                "b_stages": sb,
                "change": change,
            }
        )
    version_changes = [
        {
            "key": k,
            "name": b["names"].get(k) or a["names"].get(k) or k,
            "a": {"version": a["versions"][k][0], "freshness": a["versions"][k][1]},
            "b": {"version": b["versions"][k][0], "freshness": b["versions"][k][1]},
        }
        for k in sorted(set(a["versions"]) & set(b["versions"]))
        if a["versions"][k] != b["versions"][k]
    ]
    instr = sorted(set(a["instructions"]) | set(b["instructions"]))
    pa, pb = a["prompt"] or {}, b["prompt"] or {}

    def diff(x: list[str], y: list[str]) -> dict[str, list[str]]:
        return {"a_only": sorted(set(x) - set(y)), "b_only": sorted(set(y) - set(x))}

    mcp_keys = sorted(set(a["mcp"]) | set(b["mcp"]))
    return {
        "a_run_id": a_id,
        "b_run_id": b_id,
        "same_pack": a["pack_hash"] is not None and a["pack_hash"] == b["pack_hash"],
        "sources": sources,
        "counts": dict(Counter(s["change"] for s in sources)),
        "prompt": {
            "a": {
                "name": pa.get("name"),
                "version": pa.get("version"),
                "hash": pa.get("content_hash"),
            },
            "b": {
                "name": pb.get("name"),
                "version": pb.get("version"),
                "hash": pb.get("content_hash"),
            },
            "same": pa.get("content_hash") == pb.get("content_hash"),
        },
        "instructions": [
            {
                "name": n,
                "a_hash": a["instructions"].get(n),
                "b_hash": b["instructions"].get(n),
                "changed": a["instructions"].get(n) != b["instructions"].get(n),
            }
            for n in instr
        ],
        "lessons": diff(a["lessons"], b["lessons"]),
        "versions": version_changes,
        "queries": diff(a["queries"], b["queries"]),
        "mcp": [
            {"tool": k, "a": a["mcp"].get(k), "b": b["mcp"].get(k)}
            for k in mcp_keys
            if a["mcp"].get(k) != b["mcp"].get(k)
        ],
        "gaps": {
            "resolved": sorted(set(a["gaps"]) - set(b["gaps"])),
            "introduced": sorted(set(b["gaps"]) - set(a["gaps"])),
        },
        "citations": diff(a["cited"], b["cited"]),
        "conclusions": diff(a["findings"], b["findings"]),
    }


# --------------------------------------------------------------------------- activity


def _sample_runs(session: Session, stmt: Any, n: int = 10) -> dict[Any, list[str]]:
    """Up to ``n`` run ids per group from ``stmt`` (columns: group key, run id, recency),
    newest first, ranked in SQL so no group is loaded whole."""
    pairs = stmt.distinct().subquery()
    key, run, recency = pairs.c
    rank = func.row_number().over(partition_by=key, order_by=(recency.desc(), run))
    ranked = select(key.label("key"), run.label("run"), rank.label("rank")).subquery()
    out: dict[Any, list[str]] = defaultdict(list)
    for group, run_id in session.execute(
        select(ranked.c.key, ranked.c.run).where(ranked.c.rank <= n).order_by(ranked.c.rank)
    ):
        out[group].append(run_id)
    return out


def project_activity(session: Session, project_id: str, limit: int = 10) -> dict[str, Any]:
    """Grouped SQL throughout: nothing here loads every run, record, or feedback item of
    the project."""
    get_or_404(session, Project, project_id, "project")
    E, F = ContextEvidenceEvent, ContextFeedback
    in_project = E.run_id.in_(select(Run.id).where(Run.project_id == project_id))

    def metrics(counts: Any, stages: tuple[str, ...]) -> list[dict[str, Any]]:
        ids = [row[0] for row in counts]
        sources = {
            s.id: s for s in session.scalars(select(ContextSource).where(ContextSource.id.in_(ids)))
        }
        runs = _sample_runs(
            session,
            select(E.source_id, E.run_id, E.ts).where(
                in_project, E.source_id.in_(ids), E.stage.in_(stages)
            ),
        )
        return [
            {
                "source": source_out(sources[sid]) if sid in sources else {"id": sid},
                "count": int(count),
                "run_count": int(run_count),
                "runs": sorted(runs.get(sid, [])),
            }
            for sid, count, run_count in counts
        ]

    def top(stages: tuple[str, ...]) -> list[dict[str, Any]]:
        counts = session.execute(
            select(E.source_id, func.count(), func.count(func.distinct(E.run_id)))
            .where(in_project, E.stage.in_(stages), E.source_id.is_not(None))
            .group_by(E.source_id)
            .order_by(func.count().desc(), E.source_id)
            .limit(limit)
        ).all()
        return metrics(counts, stages)

    used = select(E.source_id).where(
        in_project,
        E.stage.in_(("selected", "retrieved", "injected", "referenced", "cited")),
        E.source_id.is_not(None),
    )
    runs_available = func.count(func.distinct(E.run_id))
    available = session.execute(
        select(E.source_id, runs_available, runs_available)
        .where(
            in_project,
            E.stage == "available",
            E.source_id.is_not(None),
            E.source_id.not_in(used),
        )
        .group_by(E.source_id)
        .order_by(E.source_id)
        .limit(limit)
    ).all()

    in_feedback = F.project_id == project_id
    kinds = session.execute(
        select(F.kind, func.count()).where(in_feedback).group_by(F.kind).order_by(F.kind)
    ).all()
    kind_runs = _sample_runs(session, select(F.kind, F.run_id, F.created_at).where(in_feedback))
    corrected = select(F.run_id).where(in_feedback, F.kind == "correction")
    associated = list(
        session.scalars(
            select(F.run_id)
            .join(Run, Run.id == F.run_id)
            .where(
                in_feedback,
                F.kind.in_(("gap", "unavailable", "stale")),
                or_(Run.status.in_(("failed", "timed_out")), F.run_id.in_(corrected)),
            )
            .distinct()
            .order_by(F.run_id)
            .limit(20)
        )
    )

    # Prompt versions and evaluation outcomes, grouped by the run's prompt version (never
    # by loading run snapshots). A run with a prompt but no template is "Inline prompt".
    has_prompt = (
        Run.project_id == project_id,
        or_(Run.prompt_version_id.is_not(None), Run.rendered_prompt.is_not(None)),
    )
    names = {
        vid: (name, version)
        for vid, name, version in session.execute(
            select(PromptVersion.id, PromptTemplate.name, PromptVersion.version)
            .join(PromptTemplate, PromptTemplate.id == PromptVersion.template_id)
            .where(PromptVersion.id.in_(select(Run.prompt_version_id).where(*has_prompt)))
        )
    }
    outcomes: dict[Any, Counter[str]] = defaultdict(Counter)
    for vid, status, n in session.execute(
        select(Run.prompt_version_id, EvaluationResult.status, func.count())
        .join(Run, Run.id == EvaluationResult.run_id)
        .where(*has_prompt)
        .group_by(Run.prompt_version_id, EvaluationResult.status)
    ):
        outcomes[vid][status] += int(n)
    prompt_versions = []
    for vid, runs in _sample_runs(
        session, select(Run.prompt_version_id, Run.id, Run.created_at).where(*has_prompt)
    ).items():
        name, version = names.get(vid, ("Inline prompt", None))
        passed, failed = outcomes[vid]["passed"], outcomes[vid]["failed"]
        prompt_versions.append(
            {
                "prompt": name,
                "version": version,
                "runs": runs,
                "passed": passed,
                "failed": failed,
                "evaluated": passed + failed > 0,
            }
        )

    lessons = [
        feedback_out(f)
        for f in session.scalars(
            select(F)
            .where(in_feedback, F.status == "approved")
            .order_by(F.created_at, F.id)
            .limit(MAX_PAGE)
        )
    ]
    injected: dict[str, int] = {
        key: int(n)
        for key, n in session.execute(
            select(E.source_key, func.count(func.distinct(E.run_id)))
            .where(in_project, E.stage == "injected", E.source_key.like("lesson:%"))
            .group_by(E.source_key)
        )
    }
    for lesson in lessons:
        if lesson["lesson_version"] is not None:
            lesson["injected_runs"] = injected.get(
                f"lesson:{lesson['id']}@v{lesson['lesson_version']}", 0
            )
    return {
        "project_id": project_id,
        "note": "Counts are associations between recorded evidence and runs, not causes.",
        "most_retrieved": top(("retrieved",)),
        "most_cited": top(("cited",)),
        "rarely_selected": metrics(available, ("available",)),
        "stale_or_unavailable": top(("stale", "unavailable")),
        "gaps_by_category": [
            {"kind": kind, "count": int(n), "runs": sorted(kind_runs.get(kind, []))}
            for kind, n in kinds
        ],
        "runs_with_missing_context_and_failure_or_correction": associated,
        "prompt_versions": sorted(prompt_versions, key=lambda e: (e["prompt"], e["version"] or 0)),
        "lessons": lessons,
        "pending_feedback": int(
            session.scalar(select(func.count()).where(in_feedback, F.status == "pending")) or 0
        ),
    }


# --------------------------------------------------------------------------- feedback


def list_feedback(
    session: Session,
    *,
    run_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(ContextFeedback)
    if run_id:
        stmt = stmt.where(ContextFeedback.run_id == run_id)
    if project_id:
        stmt = stmt.where(ContextFeedback.project_id == project_id)
    if status:
        stmt = stmt.where(ContextFeedback.status == status)
    stmt = stmt.order_by(ContextFeedback.created_at, ContextFeedback.id).limit(500)
    return [feedback_out(f) for f in session.scalars(stmt)]


def create_feedback(
    session: Session,
    clock: Clock,
    run_id: str,
    *,
    kind: str,
    text: str,
    source_key: str | None,
    actor: str,
) -> dict[str, Any]:
    run = get_or_404(session, Run, run_id, "run")
    if kind not in FEEDBACK_KINDS:
        raise ValidationError(f"kind must be one of: {', '.join(FEEDBACK_KINDS)}")
    clean = str(sanitize(text.strip()))[:4000]
    if not clean:
        raise ValidationError("text is required")
    now = clock.now()
    count = session.scalar(
        select(func.count()).where(
            ContextFeedback.run_id == run_id, ContextFeedback.reported_by == "user"
        )
    )
    row = ContextFeedback(
        run_id=run_id,
        project_id=run.project_id,
        dedupe_key=f"user:{int(count or 0) + 1}:{now.isoformat()}",
        kind=kind,
        text=clean,
        source_key=source_key[:300] if source_key else None,
        reported_by="user",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    record_audit(
        session,
        clock,
        action="context_feedback.create",
        resource_type="context_feedback",
        resource_id=row.id,
        actor=actor,
        run_id=run_id,
        details={"kind": kind},
    )
    return feedback_out(row)


def _pending(session: Session, feedback_id: str) -> ContextFeedback:
    row = session.get(ContextFeedback, feedback_id)
    if row is None:
        raise NotFoundError("context feedback", feedback_id)
    if row.status != "pending":
        raise ConflictError(f"This feedback was already {row.status}")
    return row


def classify(
    session: Session, clock: Clock, feedback_id: str, classification: str, *, actor: str
) -> dict[str, Any]:
    if classification not in CLASSIFICATIONS:
        raise ValidationError(f"classification must be one of: {', '.join(CLASSIFICATIONS)}")
    row = _pending(session, feedback_id)
    row.classification = classification
    row.proposed_destination = PROPOSAL[classification]
    row.lesson_text = row.lesson_text or row.text
    row.updated_at = clock.now()
    record_audit(
        session,
        clock,
        action="context_feedback.classify",
        resource_type="context_feedback",
        resource_id=row.id,
        actor=actor,
        run_id=row.run_id,
        details={"classification": classification, "proposed": row.proposed_destination},
    )
    return feedback_out(row)


def propose(
    session: Session,
    clock: Clock,
    feedback_id: str,
    destination: str,
    lesson_text: str | None,
    *,
    actor: str,
) -> dict[str, Any]:
    if destination not in DESTINATIONS:
        raise ValidationError(f"destination must be one of: {', '.join(DESTINATIONS)}")
    row = _pending(session, feedback_id)
    if row.classification is None:
        raise ValidationError("Classify the feedback before proposing a destination")
    row.proposed_destination = destination
    if lesson_text is not None:
        clean = str(sanitize(lesson_text.strip()))[:4000]
        if not clean:
            raise ValidationError("lesson_text cannot be empty")
        row.lesson_text = clean
    row.updated_at = clock.now()
    record_audit(
        session,
        clock,
        action="context_feedback.propose",
        resource_type="context_feedback",
        resource_id=row.id,
        actor=actor,
        run_id=row.run_id,
        details={"destination": destination},
    )
    return feedback_out(row)


def approve(
    session: Session, clock: Clock, feedback_id: str, *, actor: str, note: str | None = None
) -> dict[str, Any]:
    """A person approves the proposal. Only now can a lesson reach future runs."""
    row = _pending(session, feedback_id)
    if row.classification is None or row.proposed_destination is None:
        raise ValidationError("Classify the feedback and choose a destination before approving")
    if row.proposed_destination in INJECTED_DESTINATIONS:
        if not row.project_id:
            raise ValidationError(
                "Only runs in a project can promote a lesson; this run has no project"
            )
        current = session.scalar(
            select(func.max(ContextFeedback.lesson_version)).where(
                ContextFeedback.project_id == row.project_id
            )
        )
        row.lesson_version = int(current or 0) + 1
    now = clock.now()
    row.status = "approved"
    row.reviewed_by = actor[:200]
    row.reviewed_at = now
    row.review_note = note
    row.updated_at = now
    record_audit(
        session,
        clock,
        action="context_feedback.approve",
        resource_type="context_feedback",
        resource_id=row.id,
        actor=actor,
        run_id=row.run_id,
        details={
            "classification": row.classification,
            "destination": row.proposed_destination,
            "lesson_version": row.lesson_version,
        },
    )
    return feedback_out(row)


def reject(
    session: Session, clock: Clock, feedback_id: str, *, actor: str, note: str | None = None
) -> dict[str, Any]:
    row = _pending(session, feedback_id)
    now = clock.now()
    row.status = "rejected"
    row.reviewed_by = actor[:200]
    row.reviewed_at = now
    row.review_note = note
    row.updated_at = now
    record_audit(
        session,
        clock,
        action="context_feedback.reject",
        resource_type="context_feedback",
        resource_id=row.id,
        actor=actor,
        run_id=row.run_id,
        details={"note": note},
    )
    return feedback_out(row)
