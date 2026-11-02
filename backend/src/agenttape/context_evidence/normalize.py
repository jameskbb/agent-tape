"""Normalize persisted ``events`` rows into context evidence (append-only, idempotent).

Inputs (all already persisted and redacted by the event sink):

* ``context``  - the agent's structured telemetry: ``stage`` + ``source`` + query/tool/
  result/reason. ``stage == "correction"`` is a user correction relayed by the agent and
  becomes a pending feedback item, never an instruction.
* ``finding``  - a finding/recommendation/decision with links to source ids
  (supports/conflicts/missing) -> evidence links + ``cited`` / ``gap`` records. Links
  shaped as a citation (a ``path``, with optional line range and quote) also become a
  finding and its citations through ``FindingsService``.
* ``file_read`` - a repository file the adapter reported reading -> ``retrieved``,
  linked to File Evidence by path.
* ``tool_result`` (paired with its ``tool_call``) - MCP tools (``mcp__server__tool`` from
  Claude Code, ``server.tool`` from Codex), file reads (``Read``), and searches -> one
  ``retrieved`` (or ``unavailable`` on error) record with sanitized arguments, status,
  size, row count, schema, and a result hash. Result content is never stored. A file read
  records the requested range (``offset``/``limit``, or whole file) and the range actually
  returned (from the ``cat -n`` line numbers in the result). A bounded set of read-only
  shell commands (``cat``, ``head``, ``tail``, ``sed -n``; see ``domain/shell_reads.py``)
  is classified the same way: one ``retrieved`` record per file read, with the command's
  own program and line range as ``shell_read`` details.

Every record is keyed on the source event id, so re-running over the same events (monitor,
terminal re-scan, restart) never duplicates anything. A repository file is always the
source ``file:<path>``, whatever id agent telemetry gave it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agenttape.application.services.findings import FindingsService
from agenttape.context_evidence.common import (
    CLASSIFICATIONS,
    FEEDBACK_STAGES,
    RELATIONS,
    STAGES,
    add_evidence,
    outside_label,
    safe_locator,
    sanitize,
    scope_for,
    sha256_text,
    upsert_source,
)
from agenttape.domain.enums import EventType
from agenttape.domain.shell_reads import ShellRead, classify_shell_read
from agenttape.file_evidence.ledger import resolve_path
from agenttape.infrastructure.clock import Clock
from agenttape.infrastructure.db.extra_models.context_evidence import (
    ContextEvidenceEvent,
    ContextFeedback,
    EvidenceLink,
)
from agenttape.infrastructure.db.models import Event, Run

log = logging.getLogger(__name__)
RELEVANT_TYPES = (
    EventType.CONTEXT.value,
    EventType.FINDING.value,
    EventType.FILE_READ.value,
    EventType.TOOL_RESULT.value,
)
BATCH = 500
FILE_TOOLS = ("Read", "read_file", "View", "view")
#: Tool calls whose ``command`` string may be a bounded read-only shell read.
SHELL_TOOLS = ("Bash",)
_NUMBERED_LINE = re.compile(r"^\s*(\d+)\t", re.MULTILINE)
SEARCH_TOOLS = {
    "Grep": ("repository_search", "pattern"),
    "Glob": ("repository_search", "pattern"),
    "web_search": ("web_search", "query"),
    "WebSearch": ("web_search", "query"),
    "WebFetch": ("web_page", "url"),
}


def parse_mcp_tool(name: str, args: dict[str, Any]) -> tuple[str, str] | None:
    """``mcp__server__tool`` (Claude Code) or ``server.tool`` with ``arguments`` (Codex)."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return parts[1], "__".join(parts[2:])
        return None
    if "." in name and "arguments" in args:
        server, _, tool = name.partition(".")
        return server, tool
    return None


def _numbered_range(output: str) -> tuple[int, int] | None:
    """First and last line numbers of the Claude Code ``cat -n`` result layout."""
    numbers = [int(m.group(1)) for m in _NUMBERED_LINE.finditer(output)]
    if not numbers:
        return None
    start, end = numbers[0], numbers[-1]
    return (start, end) if start <= end else None


def _shell_read_range(read: ShellRead, line_count: int) -> tuple[int, int] | None:
    """The line range a shell read's output covers, when the start is knowable."""
    if line_count <= 0:
        return None
    if read.program == "cat":
        return (1, line_count)
    if read.program == "head" and read.line_start is not None and read.line_end is not None:
        return (1, min(read.line_end, line_count))
    if read.program == "sed" and read.line_start is not None:
        end = read.line_start + line_count - 1
        if read.line_end is not None:
            end = min(end, read.line_end)
        return (read.line_start, end)
    if read.program == "tail" and read.line_start is not None:
        return (read.line_start, read.line_start + line_count - 1)
    return None


def result_metadata(output: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Hash, size, row count, and schema of a tool result; the content is never kept."""
    rows: int | None = None
    schema: list[str] | None = None
    media = "text/plain"
    try:
        parsed = json.loads(output) if output.strip() else None
    except ValueError:
        parsed = None
    if parsed is not None:
        media = "application/json"
        data = parsed.get("rows") if isinstance(parsed, dict) else parsed
        if isinstance(data, list):
            rows = len(data)
            if data and isinstance(data[0], dict):
                schema = [str(k) for k in list(data[0])[:50]]
        if isinstance(parsed, dict) and isinstance(parsed.get("schema"), list):
            schema = [str(c) for c in parsed["schema"][:50]]
    truncated = payload.get("truncated")
    return {
        "hash": sha256_text(output),
        "bytes": len(output.encode()),
        "rows": rows,
        "schema": schema,
        "truncated": truncated if isinstance(truncated, bool) else None,
        "media_type": media,
    }


class _Normalizer:
    def __init__(self, session: Session, run: Run, clock: Clock, workdir: Path | None) -> None:
        self.session = session
        self.run = run
        self.scope = scope_for(run.project_id)
        self.now = clock.now()
        self.workdir = workdir
        self.aliases: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    def feedback(
        self,
        ev: Event,
        suffix: str,
        kind: str,
        text: str,
        *,
        source_key: str | None = None,
        suggested: str | None = None,
    ) -> None:
        key = f"ev:{ev.id}:{suffix}"
        exists = self.session.scalars(
            select(ContextFeedback.id).where(
                ContextFeedback.run_id == self.run.id, ContextFeedback.dedupe_key == key
            )
        ).first()
        if exists is not None:
            return
        self.session.add(
            ContextFeedback(
                run_id=self.run.id,
                project_id=self.run.project_id,
                dedupe_key=key,
                kind=kind,
                text=str(sanitize(text or kind))[:4000],
                source_key=source_key,
                event_id=ev.id,
                reported_by="agent-reported",
                suggested_classification=suggested if suggested in CLASSIFICATIONS else None,
                status="pending",
                created_at=ev.ts,
                updated_at=ev.ts,
            )
        )

    def common(self, ev: Event) -> dict[str, Any]:
        return {
            "run_id": self.run.id,
            "attempt_id": ev.attempt_id,
            "node_key": ev.node_key,
            "event_id": ev.id,
            "sequence": ev.sequence,
            "ts": ev.ts,
        }

    def source_key_for(self, agent_id: str) -> str:
        """The source key an agent-declared id was recorded under in this run (a
        repository file the agent called ``repo:x`` is the source ``file:x``)."""
        if agent_id not in self.aliases:
            self.aliases[agent_id] = agent_id
            for key, details in self.session.execute(
                select(ContextEvidenceEvent.source_key, ContextEvidenceEvent.details).where(
                    ContextEvidenceEvent.run_id == self.run.id,
                    ContextEvidenceEvent.quality == "agent-declared",
                    ContextEvidenceEvent.source_type == "repository_file",
                )
            ):
                alias = (details or {}).get("agent_source_id")
                if alias and key:
                    self.aliases[str(alias)] = key
        return self.aliases.get(agent_id, agent_id)

    # ------------------------------------------------------------------ handlers
    def context(self, ev: Event) -> None:
        p = dict(ev.payload or {})
        stage = str(p.get("stage") or "unknown")
        if stage == "correction":
            self.feedback(
                ev,
                "correction",
                "correction",
                str(p.get("text") or ev.summary),
                suggested=p.get("category"),
            )
            return
        if stage not in STAGES:
            stage = "unknown"
        src = p.get("source") if isinstance(p.get("source"), dict) else {}
        assert isinstance(src, dict)
        source_id = str(src.get("id") or src.get("name") or "unknown")
        source_type = str(src.get("type") or "unknown")
        live = src.get("live") if isinstance(src.get("live"), bool) else None
        version = str(src["version"]) if src.get("version") else None
        synced_at = str(src["synced_at"]) if src.get("synced_at") else None
        locator = safe_locator(src.get("locator"), self.workdir)
        key = source_id
        if source_type == "repository_file" and (locator or src.get("name")):
            # One repository file is one source, whatever id the agent gave it: the same
            # key as adapter-observed reads and the context pack (``file:<path>``).
            locator = locator or safe_locator(str(src["name"]), self.workdir)
            key = f"file:{locator}"
            self.aliases[source_id] = key
        source = upsert_source(
            self.session,
            scope=self.scope,
            key=key,
            source_type=source_type,
            name=str(src.get("name") or source_id),
            now=self.now,
            provider=str(src.get("provider") or ev.source),
            locator=locator,
            authority=str(src.get("authority") or "unknown"),
            live=live,
            version=version,
            synced_at=synced_at,
        )
        tool = p.get("tool") if isinstance(p.get("tool"), dict) else None
        result = p.get("result") if isinstance(p.get("result"), dict) else None
        add_evidence(
            self.session,
            **self.common(ev),
            dedupe_key=f"ev:{ev.id}",
            stage=stage,
            quality="agent-declared",
            source=source,
            live=live,
            version=version,
            synced_at=synced_at,
            query=str(sanitize(p["query"])) if p.get("query") else None,
            tool=sanitize(
                {
                    "server": tool.get("server"),
                    "name": tool.get("name"),
                    "args": tool.get("args"),
                    "status": tool.get("status") or ("error" if stage == "unavailable" else "ok"),
                }
            )
            if tool
            else None,
            result={
                k: result.get(k)
                for k in ("hash", "rows", "schema", "truncated", "media_type", "bytes")
                if k in result
            }
            if result
            else None,
            reason=str(sanitize(p["reason"])) if p.get("reason") else None,
            details={"agent_source_id": source_id[:300]} if key != source_id else None,
        )
        if stage in FEEDBACK_STAGES:
            text = str(p.get("reason") or f"{stage}: {source.name}")
            self.feedback(ev, stage, stage, text, source_key=source.source_key)

    def finding(self, ev: Event) -> None:
        # FindingsService.ingest_event is the one parser of a finding's identity (its key is
        # disambiguated on a re-used payload id, its text redacted); the legacy links below
        # take key, kind, and text from the row it returns.
        try:
            with self.session.begin_nested():
                row = FindingsService.ingest_event(self.session, self.run, ev, self.workdir)
        except Exception as exc:
            log.warning("findings: event %s could not be ingested", ev.id, exc_info=True)
            self.failed(ev, exc)
            return  # no Finding row, so no legacy link can name one
        subject, kind, text = row.finding_key, row.kind, row.text
        p = dict(ev.payload or {})
        links = p.get("links") if isinstance(p.get("links"), list) else []
        assert isinstance(links, list)
        for n, link in enumerate(links):
            if not isinstance(link, dict) or not link.get("source_id"):
                continue
            relation = str(link.get("relation") or "supports")
            if relation not in RELATIONS:
                relation = "supports"
            key = self.source_key_for(str(link["source_id"])[:300])
            source = upsert_source(
                self.session,
                scope=self.scope,
                key=key,
                source_type="unknown",
                name=key,
                now=self.now,
                provider=ev.source,
            )
            dup = self.session.scalars(
                select(EvidenceLink.id).where(
                    EvidenceLink.run_id == self.run.id,
                    EvidenceLink.subject_key == subject,
                    EvidenceLink.source_key == key,
                    EvidenceLink.relation == relation,
                )
            ).first()
            if dup is None:
                self.session.add(
                    EvidenceLink(
                        run_id=self.run.id,
                        subject_type=kind,
                        subject_key=subject,
                        subject_text=text,
                        source_id=source.id,
                        source_key=key,
                        relation=relation,
                        origin="agent-declared",
                        event_id=ev.id,
                        created_at=ev.ts,
                    )
                )
            add_evidence(
                self.session,
                **self.common(ev),
                dedupe_key=f"ev:{ev.id}:{n}",
                stage="gap" if relation == "missing" else "cited",
                quality="agent-declared",
                source=source,
                reason=f"{kind} {subject} ({relation})",
                details={"subject_key": subject, "subject_type": kind, "relation": relation},
            )

    def file_read(
        self, ev: Event, path: str | None = None, *, dedupe_suffix: int = 0, **extra: Any
    ) -> None:
        raw = path or (ev.payload or {}).get("path")
        if not raw:
            return
        rel = safe_locator(str(raw), self.workdir) or str(raw)
        source = upsert_source(
            self.session,
            scope=self.scope,
            key=f"file:{rel}",
            source_type="repository_file",
            name=rel,
            now=self.now,
            provider=ev.source,
            locator=rel,
            authority="authoritative",
            live=True,
        )
        dedupe_key = f"ev:{ev.id}" if dedupe_suffix == 0 else f"ev:{ev.id}:{dedupe_suffix}"
        add_evidence(
            self.session,
            **self.common(ev),
            dedupe_key=dedupe_key,
            stage=extra.pop("stage", "retrieved"),
            quality="adapter-observed",
            source=source,
            live=True,
            reason=extra.pop("reason", "Reported read by the adapter"),
            details={"file_path": rel, "bytes": (ev.payload or {}).get("bytes"), **extra},
        )

    def tool_result(self, ev: Event) -> None:
        p = dict(ev.payload or {})
        call = self._find_call(ev)
        if call is None:
            return
        cp = dict(call.payload or {})
        name = str(cp.get("tool") or "")
        args = cp.get("input") if isinstance(cp.get("input"), dict) else {}
        assert isinstance(args, dict)
        is_error = bool(p.get("is_error"))
        output = str(p.get("output") or "")
        stage = "unavailable" if is_error else "retrieved"
        status = "error"
        if is_error and "denied" in output.lower():
            status = "denied"
        elif not is_error:
            status = "ok"
        timing = {"started_at": call.ts.isoformat(), "ended_at": ev.ts.isoformat()}
        mcp = parse_mcp_tool(name, args)
        if mcp is not None:
            server, tool = mcp
            call_args = args.get("arguments") if "arguments" in args and len(args) == 1 else args
            source = upsert_source(
                self.session,
                scope=self.scope,
                key=f"mcp:{server}/{tool}",
                source_type="mcp_tool",
                name=f"{server} · {tool}",
                now=self.now,
                provider=server,
                locator=f"mcp://{server}/{tool}",
                live=True,
            )
            add_evidence(
                self.session,
                **self.common(ev),
                dedupe_key=f"ev:{ev.id}",
                stage=stage,
                quality="adapter-observed",
                source=source,
                live=True,
                tool={
                    "server": server,
                    "name": tool,
                    "args": sanitize(call_args),
                    "status": status,
                    **timing,
                },
                result=result_metadata(output, p),
                reason=None if not is_error else str(sanitize(output.strip()[:200])),
                details={"call_event_id": call.id},
            )
            return
        if name in FILE_TOOLS:
            path = args.get("file_path") or args.get("path")
            if path:
                offset = args.get("offset")
                limit = args.get("limit")
                requested: dict[str, int] = {}
                if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
                    requested["offset"] = offset
                if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1:
                    requested["limit"] = limit
                read_extra: dict[str, Any] = {"requested": requested, "whole_file": not requested}
                if not is_error:
                    numbered = _numbered_range(output)
                    if numbered is not None:
                        read_extra["returned_lines"] = {"start": numbered[0], "end": numbered[1]}
                    if p.get("output_truncated") is True:
                        read_extra["returned_truncated"] = True
                self.file_read(
                    ev,
                    str(path),
                    stage=stage,
                    reason="Read tool call reported by the adapter"
                    if not is_error
                    else "Read tool call failed",
                    call_event_id=call.id,
                    tool=name,
                    **read_extra,
                )
            return
        if name in SHELL_TOOLS:
            read = classify_shell_read(args.get("command"))
            if read is None:
                return
            shell_cwd = cp.get("cwd") if isinstance(cp.get("cwd"), str) else None
            line_count = len(output.splitlines()) if output else 0
            single_path = len(read.paths) == 1
            shell_lines = (
                _shell_read_range(read, line_count)
                if single_path and not is_error and p.get("output_truncated") is not True
                else None
            )
            for n, raw_path in enumerate(read.paths):
                if self.workdir is not None:
                    resolved = resolve_path(raw_path, self.workdir, shell_cwd)
                    if resolved is None:
                        rel = raw_path
                    elif resolved.inside:
                        rel = resolved.path
                    else:
                        rel = outside_label(resolved.path)
                else:
                    rel = raw_path
                shell_extra: dict[str, Any] = {"shell_read": read.as_details()}
                if shell_lines is not None:
                    shell_extra["returned_lines"] = {
                        "start": shell_lines[0],
                        "end": shell_lines[1],
                    }
                self.file_read(
                    ev,
                    rel,
                    stage=stage,
                    reason=f"Shell read ({read.program}) reported by the adapter"
                    if not is_error
                    else "Shell read failed",
                    call_event_id=call.id,
                    tool=name,
                    dedupe_suffix=n,
                    **shell_extra,
                )
            return
        if name in SEARCH_TOOLS:
            source_type, arg = SEARCH_TOOLS[name]
            query = str(args.get(arg) or "")
            source = upsert_source(
                self.session,
                scope=self.scope,
                key=f"search:{name}" if source_type != "web_page" else f"web:{safe_locator(query)}",
                source_type=source_type,
                name=name if source_type != "web_page" else str(safe_locator(query)),
                now=self.now,
                provider=ev.source,
                locator=safe_locator(query) if source_type == "web_page" else None,
                live=True,
            )
            add_evidence(
                self.session,
                **self.common(ev),
                dedupe_key=f"ev:{ev.id}",
                stage=stage,
                quality="adapter-observed",
                source=source,
                live=True,
                query=str(sanitize(query)) if source_type != "web_page" else None,
                tool={"name": name, "args": sanitize(args), "status": status, **timing},
                result=result_metadata(output, p),
                details={"call_event_id": call.id},
            )

    def _find_call(self, ev: Event) -> Event | None:
        """The ``tool_call`` a result answers (every built-in adapter links it)."""
        if not ev.parent_event_id:
            return None
        parent = self.session.get(Event, ev.parent_event_id)
        return parent if parent is not None and parent.type == EventType.TOOL_CALL.value else None

    # ------------------------------------------------------------------ dispatch
    def handle(self, ev: Event) -> None:
        if ev.type == EventType.CONTEXT.value:
            self.context(ev)
        elif ev.type == EventType.FINDING.value:
            self.finding(ev)
        elif ev.type == EventType.FILE_READ.value:
            self.file_read(ev)
        elif ev.type == EventType.TOOL_RESULT.value:
            self.tool_result(ev)

    def failed(self, ev: Event, exc: Exception) -> None:
        """Record that one event could not be normalized, so it cannot block the rest."""
        add_evidence(
            self.session,
            **self.common(ev),
            dedupe_key=f"ev:{ev.id}:error",
            stage="unknown",
            quality="adapter-observed",
            source=None,
            reason=f"AgentTape could not normalize this {ev.type} event "
            f"({type(exc).__name__}); it is still in the run timeline",
        )


def processed_through(session: Session, run_id: str) -> int:
    """Highest source-event sequence already normalized for the run (restart cursor)."""
    return int(
        session.scalar(
            select(func.max(ContextEvidenceEvent.sequence)).where(
                ContextEvidenceEvent.run_id == run_id, ContextEvidenceEvent.event_id.is_not(None)
            )
        )
        or 0
    )


def normalize_events(
    session: Session, clock: Clock, run: Run, events: list[Event], workdir: Path | None
) -> None:
    """Normalize ``events`` (ordered by sequence). Each event gets its own savepoint: one
    that fails is recorded as an ``unknown`` record and never blocks the rest."""
    norm = _Normalizer(session, run, clock, workdir)
    for ev in events:
        try:
            with session.begin_nested():
                norm.handle(ev)
        except Exception as exc:
            log.warning("context evidence: event %s could not be normalized", ev.id, exc_info=True)
            try:
                with session.begin_nested():
                    norm.failed(ev, exc)
            except Exception:
                log.exception("context evidence: could not record the failure of %s", ev.id)


def normalize_run(
    session: Session,
    clock: Clock,
    run_id: str,
    after_sequence: int = 0,
    workdir: Path | None = None,
) -> int:
    """Normalize every relevant event after ``after_sequence``; returns the new cursor."""
    run = session.get(Run, run_id)
    if run is None:
        return after_sequence
    cursor = after_sequence
    while True:
        events = list(
            session.scalars(
                select(Event)
                .where(
                    Event.run_id == run_id,
                    Event.sequence > cursor,
                    Event.type.in_(RELEVANT_TYPES),
                )
                .order_by(Event.sequence)
                .limit(BATCH)
            )
        )
        normalize_events(session, clock, run, events, workdir)
        if events:
            cursor = events[-1].sequence
        if len(events) < BATCH:
            return cursor
