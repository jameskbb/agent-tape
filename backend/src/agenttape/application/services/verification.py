"""``VerificationService``: the one application entry point for verification.

Verification runs on finished runs only (``TERMINAL_STATUSES``): there is no live or
incremental verification anywhere. Every verdict is written through
``FindingsService.record_verdict`` - never a second ``CitationVerdict`` writer - so a
re-check's history is always the same append-only chain the rest of the findings system
already relies on.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agenttape.application.schemas.verification import VerificationSummary
from agenttape.application.services.common import get_or_404, require_finished
from agenttape.application.services.findings import FindingsService, latest_verdict
from agenttape.domain.enums import TERMINAL_STATUS_VALUES, RunKind
from agenttape.findings.evidence import (
    event_view,
    reads_for_path,
    run_window,
    start_hash,
    writes_after,
)
from agenttape.findings.normalize import strip_cat_n
from agenttape.findings.retention import BlobUnavailable, load_read, retain_read
from agenttape.findings.verify import (
    METHOD,
    VERIFIER_VERSION,
    CitationView,
    ReadView,
    WriteView,
    decide_event_citation,
    decide_file_citation,
    inputs_digest,
)
from agenttape.infrastructure.db.extra_models.findings import Citation, CitationVerdict, Finding
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.models import Run

#: Verification triggers this service accepts. "automatic" is the worker's run-terminal
#: hook; "recheck" is every explicit re-check (the API, the seed's safety net).
#: Never a third value: an unknown trigger is a caller bug, not a silent no-op.
TRIGGERS = frozenset({"automatic", "recheck"})

if TYPE_CHECKING:
    from agenttape.application.context import AppContext
    from agenttape.findings.evidence import ReadSource
    from agenttape.findings.verify import VerdictDraft
    from agenttape.infrastructure.db.extra_models.verification import EvidenceRead

log = logging.getLogger(__name__)


class VerificationService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def _retain(self, session: Session, run: Run, read: ReadSource) -> EvidenceRead | None:
        """:func:`retain_read`, with an unexpected error logged and read as "nothing
        retained", except for a read with a retained output: its event text is only the
        first part of the read, so checking against it would blame the size cap for content
        that failed to load. That read is :class:`BlobUnavailable` instead."""
        try:
            return retain_read(self.ctx, session, run, read)
        except BlobUnavailable:
            raise
        except Exception:
            log.exception(
                "verification: could not retain the read of %s for run %s", read.path, run.id
            )
            if read.retained is not None:
                raise BlobUnavailable(
                    "the read's retained output could not be loaded",
                    blob_ref_value=f"sha256:{read.retained.sha256}",
                ) from None
            return None

    def _read_view(self, session: Session, run: Run, read: ReadSource) -> ReadView:
        """The ``ReadView`` for ``read``, backed by its retained content.

        A retention write or load failure never blocks a verdict: an unexpected error
        (disk full, over the artifact size limit) is logged and this read falls back to
        its event output with ``blob_ref`` ``None``; a :class:`BlobUnavailable` load
        failure (a pruned local file, a missing Git object, a hash mismatch, or a read's
        retained output that no longer loads) becomes content_state ``"unavailable"``. A
        read with a retained output never falls back to its capped event text.
        """
        base = ReadView(
            evidence_id=read.evidence_id,
            path=read.path,
            tool=read.tool,
            call_event_id=read.call_event_id,
            result_event_id=read.result_event_id,
            result_sequence=(
                read.result_sequence if read.result_sequence is not None else read.sequence
            ),
            numbered_lines=None,
            truncated=read.output_truncated,
            blob_ref=None,
            content_state="none",
            limit=read.limit,
        )
        if not read.output and read.retained is None:
            return base
        try:
            retained = self._retain(session, run, read)
            loaded = load_read(self.ctx, session, retained) if retained is not None else None
        except BlobUnavailable as exc:
            return dataclasses.replace(
                base,
                blob_ref=exc.blob_ref_value,
                content_state="unavailable",
                unavailable_detail=exc.detail,
            )
        if loaded is not None:
            numbered_lines = loaded.numbered_lines or None
            return dataclasses.replace(
                base,
                numbered_lines=numbered_lines,
                truncated=loaded.truncated,
                blob_ref=loaded.blob_ref,
                content_state="numbered" if numbered_lines else "unnumbered",
            )
        # No row could be retained (an unresolvable read, or an unexpected write error
        # already logged above): fall back to the event's own output, blob_ref None.
        numbered_lines = tuple(strip_cat_n(read.output or "")) or None
        return dataclasses.replace(
            base,
            numbered_lines=numbered_lines,
            content_state="numbered" if numbered_lines else "unnumbered",
        )

    def recheck_outdated_never_read(self) -> list[str]:
        """Re-verify every finished run read only through Claude Code hooks (an observed
        run, or a Herdr run hosting a session: ``observed_sessions.mode == "hosted"``)
        whose latest ``lexical_v1`` verdict on some citation is ``never_read`` from an
        older :data:`VERIFIER_VERSION`, and return their ids.

        Verification runs automatically only when a run finishes, so without this a run
        verified before the hook-coverage rule would keep an old ``never_read`` (decided
        with no coverage check) as its latest verdict. Only those verdicts can overclaim:
        a launched run's verdicts, and every other verdict, mean the same under the new
        rule and are left for an explicit re-check. ``agenttape serve`` runs this once at
        start (the ``observed-sessions`` background service); a run that fails to re-verify
        is logged and skipped.
        """
        hosted = select(ObservedSession.run_id).where(ObservedSession.mode == "hosted")
        with self.ctx.session() as session:
            rows = session.execute(
                select(Finding.run_id, CitationVerdict)
                .join(Citation, Citation.id == CitationVerdict.citation_id)
                .join(Finding, Finding.id == Citation.finding_id)
                .join(Run, Run.id == Finding.run_id)
                .where(
                    CitationVerdict.method == METHOD,
                    Run.status.in_(TERMINAL_STATUS_VALUES),
                    or_(Run.kind == RunKind.OBSERVED.value, Run.id.in_(hosted)),
                )
            ).all()
        by_citation: dict[str, tuple[str, list[CitationVerdict]]] = {}
        for run_id, verdict in rows:
            by_citation.setdefault(verdict.citation_id, (run_id, []))[1].append(verdict)
        outdated: set[str] = set()
        for run_id, verdicts in by_citation.values():
            latest = latest_verdict(verdicts)
            if (
                latest is not None
                and latest.verdict == "never_read"
                and latest.verifier_version != VERIFIER_VERSION
            ):
                outdated.add(run_id)
        rechecked: list[str] = []
        for run_id in sorted(outdated):
            try:
                self.verify_run(run_id, trigger="automatic")
            except Exception:
                log.exception("verification: could not re-check run %s", run_id)
                continue
            rechecked.append(run_id)
        return rechecked

    def verify_run(self, run_id: str, *, trigger: str = "recheck") -> VerificationSummary:
        """Decide and record a verdict for every citation on a finished run.

        ``trigger`` is ``"recheck"`` (the default: every explicit re-check always records,
        so history is kept) or ``"automatic"`` (the worker's run-terminal hook: a citation
        whose latest ``lexical_v1`` row already carries the same ``inputs_digest`` is
        skipped - counted ``unchanged``, nothing written). Any other value is a caller bug,
        never a silent no-op. Every citation - file or event - always gets a decided verdict
        (``decide_file_citation``/``decide_event_citation`` never return ``None``).
        """
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown verification trigger: {trigger!r}")
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            require_finished(
                run, f"Citations are verified once a run has finished; this run is {run.status}"
            )
            findings = list(
                session.scalars(
                    select(Finding).where(Finding.run_id == run_id).order_by(Finding.sequence)
                )
            )
            finding_by_id = {finding.id: finding for finding in findings}
            citations: list[Citation] = []
            if finding_by_id:
                citations = list(
                    session.scalars(
                        select(Citation)
                        .where(Citation.finding_id.in_(finding_by_id))
                        .order_by(Citation.finding_id, Citation.position)
                    )
                )
            # The latest lexical_v1 row per citation (``latest_verdict``, the same
            # chain-aware rule the findings read side uses), if any - what an automatic pass
            # compares its freshly-decided digest against. A citation whose latest row
            # overall is some other method (e.g. a seeded stand-in) is still compared
            # against its latest lexical_v1 row, so an unchanged engine result never
            # displaces it.
            latest_lexical: dict[str, CitationVerdict] = {}
            if citations:
                lexical_rows = list(
                    session.scalars(
                        select(CitationVerdict).where(
                            CitationVerdict.citation_id.in_([c.id for c in citations]),
                            CitationVerdict.method == METHOD,
                        )
                    )
                )
                by_citation: dict[str, list[CitationVerdict]] = {}
                for row in lexical_rows:
                    by_citation.setdefault(row.citation_id, []).append(row)
                for citation_id, rows in by_citation.items():
                    latest = latest_verdict(rows)
                    if latest is not None:
                        latest_lexical[citation_id] = latest

            window = run_window(session, run)
            #: ``None`` means "decided but unchanged" (an automatic pass skipping this
            #: citation) - distinct from never being decided at all (never happens: every
            #: citation always gets a draft from decide_file_citation/decide_event_citation).
            drafts: list[tuple[str, VerdictDraft | None]] = []
            start_hash_cache: dict[str, str | None] = {}
            for citation in citations:
                finding = finding_by_id[citation.finding_id]
                citation_view = CitationView(
                    kind=citation.kind,
                    path=citation.path,
                    inside_boundary=citation.inside_boundary,
                    line_start=citation.line_start,
                    line_end=citation.line_end,
                    quote=citation.quote,
                    quote_truncated=bool((citation.details or {}).get("quote_truncated")),
                    event_ref=citation.event_ref,
                    event_id=citation.event_id,
                )
                if citation.kind == "file" and citation.path:
                    reads = reads_for_path(
                        session, run_id, citation.path, before_sequence=finding.sequence
                    )
                    read_views: list[ReadView] = []
                    writes_by_read: dict[str, list[WriteView]] = {}
                    content_digests: dict[str, str | None] = {}
                    for read in reads:
                        read_view = self._read_view(session, run, read)
                        read_views.append(read_view)
                        content_digests[read_view.evidence_id] = read_view.blob_ref
                        if read_view.numbered_lines:
                            writes_by_read[read_view.evidence_id] = writes_after(
                                session, run_id, citation.path, read
                            )
                    if citation.path not in start_hash_cache:
                        start_hash_cache[citation.path] = start_hash(session, run_id, citation.path)
                    draft = decide_file_citation(
                        citation_view,
                        read_views,
                        writes_by_read=writes_by_read,
                        window=window,
                        start_hashes=start_hash_cache,
                    )
                    digest = inputs_digest(
                        citation_view,
                        reads=read_views,
                        writes_by_read=writes_by_read,
                        window=window,
                        content_digests=content_digests,
                    )
                else:
                    event = event_view(
                        session,
                        run_id,
                        citation.event_id,
                        artifact_store=self.ctx.artifact_store,
                    )
                    draft = decide_event_citation(citation_view, event, window=window)
                    digest = inputs_digest(citation_view, window=window, event=event)
                draft = dataclasses.replace(
                    draft, evidence={**draft.evidence, "inputs_digest": digest}
                )

                if trigger == "automatic":
                    existing = latest_lexical.get(citation.id)
                    if (
                        existing is not None
                        and (existing.evidence or {}).get("inputs_digest") == digest
                    ):
                        drafts.append((citation.id, None))
                        continue
                drafts.append((citation.id, draft))

        recorded = 0
        unchanged = 0
        verdict_counts: dict[str, int] = {}
        for citation_id, maybe_draft in drafts:
            if maybe_draft is None:
                unchanged += 1
                continue
            draft = maybe_draft
            FindingsService(self.ctx).record_verdict(
                citation_id,
                verdict=draft.verdict,
                method=METHOD,
                reason=draft.reason,
                evidence=draft.evidence,
                verifier_version=VERIFIER_VERSION,
            )
            recorded += 1
            verdict_counts[draft.verdict] = verdict_counts.get(draft.verdict, 0) + 1

        return VerificationSummary(
            run_id=run_id,
            citations=len(citations),
            recorded=recorded,
            unchanged=unchanged,
            verdicts=verdict_counts,
        )
