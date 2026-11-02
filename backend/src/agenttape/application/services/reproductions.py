"""``ReproductionService``: the only writer of ``citation_reproductions``.

Re-running a citation's recorded argv never touches ``citation_verdicts``: a reproduction
result lives only here, on its own record, so it can never overwrite or be confused with
the citation's own lexical verdict. ``request`` and ``request_automatic`` only record a
queued row and enqueue a worker job (``kind="run"``, ``run_id=None`` so run concurrency
limits and run-queue reconciliation never treat it as a run, payload
``run_kind="citation_reproduction"``) - the actual re-run always happens later, in
:meth:`ReproductionService.execute`, inside a worker job, never synchronously in the call
that queued it.

``request_automatic`` is the one entry point :mod:`agenttape.findings.hooks` calls when a
run finishes: every event citation whose recorded command is a read-only search
and has no reproduction yet - of any trigger - gets exactly one automatic, queued re-run,
ever. It shares the same row-creation and enqueue path as a user's own request, so there is
one place that ever writes a ``citation_reproductions`` row.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agenttape.application.schemas.verification import CitationReproductionOut
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, require_finished
from agenttape.domain.errors import ConflictError, NotFoundError, ValidationError
from agenttape.domain.policy import Verdict, check_command
from agenttape.evaluations.builtin._common import resolve_inside, run_argv
from agenttape.file_evidence.ledger import WRITE_OPS
from agenttape.findings.normalize import normalize
from agenttape.findings.reproduce import (
    COMMAND_TIMEOUT_SECONDS,
    OUTPUT_EXCERPT_CHARS,
    REPRODUCTION_KIND,
    SEARCH_TIMEOUT_SECONDS,
    RecordedCommand,
    classify,
    comparable,
    compare,
    git_refusal,
    recorded_command,
    unsafe_argument,
)
from agenttape.git.invoke import run_git_hardened
from agenttape.git.manager import IsolationError, WorktreeManager
from agenttape.infrastructure.db.extra_models.file_evidence import (
    FileEvidenceEvent,
    FileEvidenceSnapshot,
)
from agenttape.infrastructure.db.extra_models.findings import Citation, Finding
from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace
from agenttape.infrastructure.db.extra_models.verification import CitationReproduction
from agenttape.infrastructure.db.models import Event, Run
from agenttape.security.executables import REPLAY_PATH, resolve_replay_program
from agenttape.security.redaction import DEFAULT_REDACTOR

#: A queued or running reproduction blocks a second request for the same citation.
_PENDING_STATUSES = ("queued", "running")

#: The reason text for the pending-request 409, kept as one constant so the API and any
#: future caller agree on the exact wording.
_PENDING_CONFLICT = "A re-run for this citation is already in progress"

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

log = logging.getLogger(__name__)


class _CouldNotRun(Exception):
    """Nothing ran (or the result cannot be compared) - the message is the stored reason."""


@dataclass
class _Outcome:
    result: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    exit_code: int | None = None
    recorded_exit_code: int | None = None
    pinned_commit: str | None = None
    cwd: str | None = None
    output_excerpt: str | None = None
    output_sha256: str | None = None


class ReproductionService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ command extraction

    @staticmethod
    def _extract_recorded_command(
        session: Session, run_id: str, event: Event
    ) -> RecordedCommand | str:
        """:func:`recorded_command`, paired with the sibling event a tool-call citation
        needs: a TOOL_RESULT's parent TOOL_CALL (via ``parent_event_id``), or a TOOL_CALL's
        TOOL_RESULT child (the event whose ``parent_event_id`` is this one). A ``command``
        event needs no sibling; the pairing queries are skipped for it.
        """
        call_payload: dict[str, Any] | None = None
        result_payload: dict[str, Any] | None = None
        if event.type == "tool_call":
            result_event = (
                session.execute(
                    select(Event).where(
                        Event.parent_event_id == event.id,
                        Event.run_id == run_id,
                        Event.type == "tool_result",
                    )
                )
                .scalars()
                .first()
            )
            if result_event is not None:
                result_payload = result_event.payload or {}
        elif event.type == "tool_result" and event.parent_event_id:
            call_event = session.get(Event, event.parent_event_id)
            if call_event is not None and call_event.run_id == run_id:
                call_payload = call_event.payload or {}
        return recorded_command(
            event.type,
            event.payload or {},
            call_payload=call_payload,
            result_payload=result_payload,
        )

    # ------------------------------------------------------------------ request

    def request(
        self, citation_id: str, *, trigger: str, actor: str | None = None
    ) -> CitationReproductionOut:
        """Record a queued reproduction and enqueue the worker job that will run it."""
        with self.ctx.session() as session:
            citation = get_or_404(session, Citation, citation_id, "citation")
            if citation.kind != "event" or not citation.event_id:
                raise ValidationError("Only a citation of a recorded event can be re-run")
            finding = session.get(Finding, citation.finding_id)
            if finding is None:
                raise NotFoundError("citation", citation_id)
            run = get_or_404(session, Run, finding.run_id, "run")
            require_finished(
                run, f"A citation can be re-run once its run has finished; this run is {run.status}"
            )
            pending = session.scalars(
                select(CitationReproduction).where(
                    CitationReproduction.citation_id == citation_id,
                    CitationReproduction.status.in_(_PENDING_STATUSES),
                )
            ).first()
            if pending is not None:
                raise ConflictError(_PENDING_CONFLICT)
            event = session.execute(
                select(Event).where(Event.id == citation.event_id, Event.run_id == run.id)
            ).scalar_one_or_none()
            argv: list[str] = []
            mode = "command"
            if event is not None:
                extracted = self._extract_recorded_command(session, run.id, event)
                if not isinstance(extracted, str):
                    argv = list(extracted.argv)
                    mode = classify(extracted.argv)
            row = self._create_and_enqueue(
                session,
                citation_id=citation_id,
                run_id=run.id,
                source_event_id=citation.event_id,
                trigger=trigger,
                mode=mode,
                argv=argv,
                requested_by=actor,
                priority=0,
            )
            if trigger == "user":
                record_audit(
                    session,
                    self.ctx.clock,
                    action="citation.reproduce",
                    resource_type="citation",
                    resource_id=citation_id,
                    actor=actor or "local user",
                    details={"reproduction_id": row.id, "mode": mode, "argv": argv[:20]},
                    run_id=run.id,
                )
            return CitationReproductionOut.model_validate(row)

    def request_automatic(self, run_id: str) -> int:
        """Queue an automatic re-run for every cited search with no reproduction yet.

        Called once, from :mod:`agenttape.findings.hooks`, when ``run_id`` reaches a
        terminal status. A citation with any existing reproduction row - automatic or
        user-requested - is skipped, so calling this twice for the same run (a retried
        hook, an observed-run reconciliation) queues nothing the second time. Unsafe
        searches are still queued: the job itself records ``could_not_run`` with the
        reason, which is the honest rung to show, rather than silently skipping them here.
        No audit entry - this is a system action, and the row's own ``trigger`` says so.

        Returns the number of reproductions queued.
        """
        queued = 0
        with self.ctx.session() as session:
            citation_rows = session.execute(
                select(Citation.id, Citation.event_id)
                .join(Finding, Finding.id == Citation.finding_id)
                .where(
                    Finding.run_id == run_id,
                    Citation.kind == "event",
                    Citation.event_id.isnot(None),
                )
            ).all()
            if not citation_rows:
                return 0
            citation_ids = [row.id for row in citation_rows]
            already_has_reproduction = set(
                session.scalars(
                    select(CitationReproduction.citation_id).where(
                        CitationReproduction.citation_id.in_(citation_ids)
                    )
                )
            )
            event_ids = {row.event_id for row in citation_rows if row.event_id}
            events_by_id = {
                event.id: event
                for event in session.scalars(
                    select(Event).where(Event.id.in_(event_ids), Event.run_id == run_id)
                )
            }
            for citation_id, event_id in citation_rows:
                if citation_id in already_has_reproduction or event_id is None:
                    continue
                event = events_by_id.get(event_id)
                if event is None:
                    continue
                extracted = self._extract_recorded_command(session, run_id, event)
                if isinstance(extracted, str) or classify(extracted.argv) != "search":
                    continue
                try:
                    self._create_and_enqueue(
                        session,
                        citation_id=citation_id,
                        run_id=run_id,
                        source_event_id=event_id,
                        trigger="automatic",
                        mode="search",
                        argv=list(extracted.argv),
                        requested_by=None,
                        priority=-1,
                    )
                except ConflictError:
                    # A person's own request for this citation landed first; it already
                    # has a pending re-run, so an automatic one is not added beside it.
                    continue
                queued += 1
            return queued

    def _create_and_enqueue(
        self,
        session: Session,
        *,
        citation_id: str,
        run_id: str,
        source_event_id: str | None,
        trigger: str,
        mode: str,
        argv: list[str],
        requested_by: str | None,
        priority: int,
    ) -> CitationReproduction:
        """The one place a ``citation_reproductions`` row is created and enqueued.

        ``priority`` lower runs first: an automatic search re-run (``-1``) is queued ahead
        of a user's own request (``0``). Raises :class:`ConflictError` when the database's
        one-pending-per-citation index refuses the row: a concurrent request for the same
        citation inserted its own queued row after this one's check but before this insert.
        """
        row = CitationReproduction(
            citation_id=citation_id,
            run_id=run_id,
            source_event_id=source_event_id,
            trigger=trigger,
            mode=mode,
            requested_by=requested_by,
            status="queued",
            argv=argv,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            pending = session.scalars(
                select(CitationReproduction.id).where(
                    CitationReproduction.citation_id == citation_id,
                    CitationReproduction.status.in_(_PENDING_STATUSES),
                )
            ).first()
            if pending is None:
                raise
            raise ConflictError(_PENDING_CONFLICT) from None
        self.ctx.queue.enqueue(
            session,
            dedupe_key=f"reproduction:{row.id}",
            run_id=None,
            kind="run",
            priority=priority,
            payload={"run_kind": REPRODUCTION_KIND, "reproduction_id": row.id},
        )
        return row

    # ------------------------------------------------------------------ execute

    async def execute(self, reproduction_id: str) -> None:
        """Run a queued reproduction and record its result. Never raises.

        Any exception after the row is marked "running" is caught here and recorded as
        ``could_not_run`` with a generic reason and the error type (the exception text goes
        only to the log) - the row never stays "running" because of a bug in this code.
        """
        with self.ctx.session() as session:
            row = session.get(CitationReproduction, reproduction_id)
            if row is None or row.status == "done":
                return
            row.status = "running"
            row.started_at = self.ctx.clock.now()
            session.flush()
            run_id = row.run_id
            source_event_id = row.source_event_id
            mode = row.mode

        try:
            outcome = await self._run(reproduction_id, run_id, source_event_id, mode)
        except Exception as exc:  # this row must never stay "running" because of a bug here
            log.exception("reproduction %s: unexpected error", reproduction_id)
            outcome = _Outcome(
                result="could_not_run",
                reason=(
                    "AgentTape hit an internal error while re-running this command; "
                    "the server log has the details."
                ),
                evidence={"internal_error": type(exc).__name__},
            )

        with self.ctx.session() as session:
            row = session.get(CitationReproduction, reproduction_id)
            if row is None:
                return
            row.status = "done"
            row.result = outcome.result
            row.reason = outcome.reason
            row.exit_code = outcome.exit_code
            row.recorded_exit_code = outcome.recorded_exit_code
            row.pinned_commit = outcome.pinned_commit
            row.cwd = outcome.cwd
            row.output_excerpt = outcome.output_excerpt
            row.output_sha256 = outcome.output_sha256
            row.evidence = outcome.evidence
            row.finished_at = self.ctx.clock.now()

    async def _run(
        self, reproduction_id: str, run_id: str, source_event_id: str | None, mode: str
    ) -> _Outcome:
        """Gather this run's context, decide and (maybe) run, then finalize every outcome.

        ``changed_before_command``, ``start_dirty``, and ``cwd_assumed`` are added to
        *every* result's evidence - reproduced, not_reproduced, or could_not_run alike -
        since they describe what the snapshot could and could not know, not the comparison
        outcome itself.
        """
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            event = (
                session.execute(
                    select(Event).where(Event.id == source_event_id, Event.run_id == run_id)
                ).scalar_one_or_none()
                if source_event_id
                else None
            )
            workspace = session.scalars(
                select(RunWorkspace).where(RunWorkspace.run_id == run_id)
            ).first()
            settings = dict(dict(run.snapshot or {}).get("project") or {}).get("settings") or {}
            repo_path = workspace.repo_path if workspace else None
            base_commit = workspace.base_commit if workspace else None
            extracted = (
                self._extract_recorded_command(session, run_id, event)
                if event is not None
                else "the cited event no longer exists"
            )
            changed_before_command = 0
            start_dirty: bool | None = None
            if event is not None:
                changed_before_command = int(
                    session.scalar(
                        select(func.count())
                        .select_from(FileEvidenceEvent)
                        .where(
                            FileEvidenceEvent.run_id == run_id,
                            FileEvidenceEvent.operation.in_(WRITE_OPS),
                            FileEvidenceEvent.ts < event.ts,
                        )
                    )
                    or 0
                )
                start_snapshot = session.scalars(
                    select(FileEvidenceSnapshot)
                    .where(
                        FileEvidenceSnapshot.run_id == run_id,
                        FileEvidenceSnapshot.kind == "start",
                    )
                    .order_by(FileEvidenceSnapshot.taken_at)
                    .limit(1)
                ).first()
                start_dirty = start_snapshot.dirty if start_snapshot is not None else None

        outcome = await self._run_core(
            reproduction_id, mode, extracted, settings, repo_path, base_commit
        )
        outcome.evidence["changed_before_command"] = changed_before_command
        outcome.evidence["start_dirty"] = start_dirty
        outcome.evidence["cwd_assumed"] = (
            True if isinstance(extracted, str) else not extracted.cwd_known
        )
        if changed_before_command > 0:
            outcome.reason += (
                f" The run had changed {changed_before_command} file(s) before this "
                "command; the re-run used the base commit without those changes."
            )
        return outcome

    async def _run_core(
        self,
        reproduction_id: str,
        mode: str,
        extracted: RecordedCommand | str,
        settings: dict[str, Any],
        repo_path: str | None,
        base_commit: str | None,
    ) -> _Outcome:
        if isinstance(extracted, str):
            return _Outcome(result="could_not_run", reason=extracted)

        argv = list(extracted.argv)
        # A git invocation outside the read-only replay set is refused here, before the
        # command policy is ever consulted, and the refusal's code is kept with the result.
        refusal = git_refusal(argv, mode)
        if refusal is not None:
            return _Outcome(
                result="could_not_run",
                reason=refusal.reason,
                evidence={"refusal": refusal.code},
            )
        unsafe_reason = unsafe_argument(argv, mode)
        if unsafe_reason is not None:
            return _Outcome(result="could_not_run", reason=unsafe_reason)
        not_comparable_reason = comparable(mode, extracted)
        if not_comparable_reason is not None:
            return _Outcome(result="could_not_run", reason=not_comparable_reason)

        allowlist = settings.get("command_allowlist")
        trusted = [v for v in settings.get("validation_commands") or [] if v]
        decision = check_command(
            argv,
            allowlist=allowlist if isinstance(allowlist, list) else None,
            trusted_commands=trusted,
        )
        evidence: dict[str, Any] = {"policy": decision.as_dict()}
        if decision.verdict == Verdict.REQUIRE_APPROVAL:
            return _Outcome(
                result="could_not_run",
                reason=(
                    "The command policy requires approval for this command "
                    f"({decision.reason}); a re-run never asks for approval."
                ),
                evidence=evidence,
            )
        if decision.verdict == Verdict.DENY:
            return _Outcome(
                result="could_not_run",
                reason=f"The command policy refuses this command: {decision.reason}",
                evidence=evidence,
            )
        # Only Verdict.ALLOW ever reaches a snapshot and an actual re-run.
        assert decision.verdict == Verdict.ALLOW

        # git replays through AgentTape's own hardened git (run_git_hardened); any other
        # program is looked up only in the system directories, never the inherited PATH
        # a run's own agent could have planted a program on.
        exe_name = PurePosixPath(argv[0]).name
        program: str | None = None
        if exe_name != "git":
            program = resolve_replay_program(argv[0])
            if program is None:
                return _Outcome(
                    result="could_not_run",
                    reason=(
                        f"{argv[0]} was not found in the system directories AgentTape re-runs "
                        f"programs from ({', '.join(REPLAY_PATH.split(':'))}), or could have "
                        "been written by this user account; nothing was run."
                    ),
                    evidence=evidence,
                )

        if not repo_path or not base_commit:
            return _Outcome(
                result="could_not_run",
                reason="This run has no recorded Git commit to re-run at.",
                evidence=evidence,
            )

        manager = WorktreeManager(self.ctx.settings.workspaces_dir)
        timeout = (
            float(SEARCH_TIMEOUT_SECONDS)
            if mode == "search"
            else min(
                float(settings.get("timeout_seconds") or COMMAND_TIMEOUT_SECONDS),
                float(COMMAND_TIMEOUT_SECONDS),
            )
        )
        snapshot_path: Path | None = None
        try:
            try:
                snapshot_path = await asyncio.to_thread(
                    manager.prepare_snapshot,
                    key=reproduction_id,
                    repo_path=Path(repo_path),
                    commit=base_commit,
                )
            except IsolationError as exc:
                raise _CouldNotRun(f"The snapshot could not be prepared: {exc}") from exc

            cwd_path = snapshot_path
            recorded_cwd = extracted.cwd
            if extracted.cwd_known and recorded_cwd not in (None, ".", ""):
                resolved = resolve_inside(snapshot_path, str(recorded_cwd))
                if resolved is None or not resolved.is_dir():
                    raise _CouldNotRun(
                        f"The recorded working directory {recorded_cwd!r} is not usable "
                        "for the re-run."
                    )
                cwd_path = resolved

            timed_out = False
            if program is None:
                proc = await asyncio.to_thread(
                    run_git_hardened, cwd_path, *argv[1:], timeout=timeout
                )
                exit_code: int | None = proc.returncode
                combined_output = (proc.stdout or "") + (proc.stderr or "")
            else:
                env_allowlist = list(settings.get("env_allowlist") or [])
                run_result = await run_argv(
                    [program, *argv[1:]],
                    cwd=cwd_path,
                    timeout=timeout,
                    env_allowlist=env_allowlist,
                    extra_env={"PATH": REPLAY_PATH},
                )
                if run_result.error:
                    raise _CouldNotRun(f"Could not start {argv[0]}: {run_result.error}")
                exit_code = run_result.exit_code
                combined_output = run_result.stdout + run_result.stderr
                timed_out = run_result.timed_out

            if timed_out:
                raise _CouldNotRun(f"The re-run did not finish within {timeout:.0f}s.")

            redacted_output = DEFAULT_REDACTOR.redact_text(combined_output)
            result, reason, compare_details = compare(
                mode, extracted, exit_code=exit_code, output=redacted_output
            )
            if not extracted.cwd_known:
                reason += (
                    " The shell's working directory was not recorded; AgentTape ran the "
                    "command at the repository root."
                )
            evidence.update(
                {
                    "compared": compare_details,
                    "recorded": {
                        "exit_code": extracted.exit_code,
                        "failed": extracted.failed,
                    },
                    "rerun": {
                        "exit_code": exit_code,
                        "timed_out": timed_out,
                    },
                }
            )
            return _Outcome(
                result=result,
                reason=reason,
                evidence=evidence,
                exit_code=exit_code,
                recorded_exit_code=extracted.exit_code,
                pinned_commit=base_commit,
                cwd=str(cwd_path),
                output_excerpt=redacted_output[-OUTPUT_EXCERPT_CHARS:],
                output_sha256=hashlib.sha256(
                    normalize(redacted_output).encode("utf-8")
                ).hexdigest(),
            )
        except _CouldNotRun as exc:
            return _Outcome(
                result="could_not_run",
                reason=str(exc),
                evidence=evidence,
                pinned_commit=base_commit,
            )
        finally:
            if snapshot_path is not None:
                message = await asyncio.to_thread(
                    manager.remove_snapshot, repo_path=Path(repo_path), path=snapshot_path
                )
                evidence["cleanup"] = message
