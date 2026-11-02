"""Retaining the content a verdict is computed from.

The one writer of ``evidence_blobs`` and ``evidence_reads`` (``ReproductionService`` is the
only writer of ``citation_reproductions``). Retained content is canonical once
captured: the first check captures it from the ``tool_result`` event's own output; every
later check loads the retained bytes, never re-reads the event. This is what makes "pruned
blob" a real, reachable ``not_checkable`` reason instead of dead vocabulary.

An unredacted read from line 1 that could be the whole file
(:func:`~agenttape.findings.verify.could_be_whole_file`) and whose exact lines exist as a Git
blob in the run's repository is stored as a Git blob reference (``git_blob`` set,
``storage_path`` None, no copy on disk) - only ``cat-file -e``/``cat-file blob`` through the policy-checked
``GuardedGit`` (never a raw process launch, never a write). Every other read is
redacted before it is written (mirrors ``ArtifactService.store``) and capped at
``retained_output.MAX_RETAINED_BYTES``, at a line boundary. The content is the read's
retained output (``findings/retained_output.py``) when the recorder kept one, so a read
over the 16 KiB event text cap is checked up to the retained-content cap; otherwise it is
the ``tool_result`` event's own output. Local copy blob files live at
``runs/<run_id>/evidence/<sha256>`` inside the run's artifact directory - the same directory
``RetentionService`` and ``DemoService.reset`` already remove with the run, so a run's
retained content is deleted with no changes to retention code.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.findings import retained_output
from agenttape.findings.evidence import ReadSource
from agenttape.findings.normalize import has_redaction, strip_cat_n
from agenttape.findings.verify import blob_id_candidates, could_be_whole_file
from agenttape.git.runner import GuardedGit
from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace
from agenttape.infrastructure.db.extra_models.verification import EvidenceBlob, EvidenceRead
from agenttape.infrastructure.db.models import Project, Run
from agenttape.security.redaction import DEFAULT_REDACTOR

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

log = logging.getLogger(__name__)


class BlobUnavailable(Exception):
    """Retained content for a read can no longer be loaded.

    ``blob_ref_value`` is the reference (``git:<id>`` or ``sha256:<hex>``) to name in the
    ``not_checkable`` verdict, so the citation names what it could not check.
    """

    def __init__(self, detail: str, *, blob_ref_value: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.blob_ref_value = blob_ref_value


@dataclass(frozen=True)
class LoadedRead:
    numbered_lines: tuple[tuple[int, str], ...]
    truncated: bool
    redacted: bool
    blob_ref: str


def blob_ref(blob: EvidenceBlob) -> str:
    """The reference shown to users and stored in verdict evidence for ``blob``."""
    if blob.git_blob:
        return f"git:{blob.git_blob}"
    return f"sha256:{blob.sha256}"


def _existing_blob(session: Session, run_id: str, sha256: str) -> EvidenceBlob | None:
    return session.scalars(
        select(EvidenceBlob).where(EvidenceBlob.run_id == run_id, EvidenceBlob.sha256 == sha256)
    ).first()


def _repo_for_run(session: Session, run_id: str) -> str | None:
    """The Git repository this run worked in, or ``None`` when it is not recorded.

    ``RunWorkspace.repo_path`` first, then the run's project as a fallback - the same
    precedence the rest of the run's Git views use.
    """
    ws = session.scalars(select(RunWorkspace).where(RunWorkspace.run_id == run_id)).first()
    if ws is not None and ws.repo_path:
        return ws.repo_path
    run = session.get(Run, run_id)
    if run is not None and run.project_id:
        project = session.get(Project, run.project_id)
        if project is not None and project.repo_path:
            return project.repo_path
    return None


def _git_blob_reference(
    session: Session, run_id: str, numbered_lines: list[tuple[int, str]]
) -> tuple[str, str] | None:
    """The (blob id, exact text) of the first Git blob candidate that exists in this run's
    repository, or ``None`` when there is no repository, no git executable, or no match.

    A read is a Git reference candidate only when it is unredacted and
    :func:`could_be_whole_file` (checked by the caller). The reference is content-addressed:
    the blob holds exactly the lines the read returned, so what is retained is right even if
    the read turns out not to have been the whole file. Git errors (no git executable, not a
    repository, a policy error) mean "no Git reference": never raised, only reported as no
    match, so a storage problem never blocks a verdict.
    """
    repo = _repo_for_run(session, run_id)
    if not repo:
        return None
    with_lf, without_lf = blob_id_candidates(numbered_lines)
    joined = "\n".join(text for _, text in numbered_lines)
    git = GuardedGit(Path(repo))
    for candidate, text in ((with_lf, joined + "\n"), (without_lf, joined)):
        try:
            result = git.run("cat-file", "-e", candidate)
        except Exception:  # any Git failure means "no reference", never a crash
            log.warning("evidence retention: could not check Git blob %s in %s", candidate, repo)
            return None
        if result.returncode == 0:
            return candidate, text
    return None


def retain_read(
    ctx: AppContext, session: Session, run: Run, source: ReadSource
) -> EvidenceRead | None:
    """Retain ``source``'s output as a content-addressed blob, once per read.

    ``None`` when the read has no recorded output (nothing to retain). Returns the existing
    row for ``source.result_event_id`` when one was already retained - never a second row
    for the same read. The output is the read's retained output when the recorder kept one
    (its ``truncated`` flag says whether the retained-content cap cut it); otherwise the
    event's own, possibly capped, output. A retained output that no longer loads raises
    :class:`BlobUnavailable` and retains nothing, so a later check can still use it. An
    unredacted read that could
    be the whole file and whose exact lines are a Git blob in the run's repository is
    stored as a zero-copy Git reference; every other read is a redacted, size-capped local
    copy.
    """
    if not source.result_event_id or not (source.output or source.retained):
        return None
    existing = session.scalars(
        select(EvidenceRead).where(EvidenceRead.result_event_id == source.result_event_id)
    ).first()
    if existing is not None:
        return existing

    output = source.output
    truncated_by_source = bool(source.output_truncated)
    if source.retained is not None:
        full = retained_output.load_output(ctx.artifact_store, run.id, source.retained)
        if full is None:
            # Never fall back to the capped event text: that would be retained for good,
            # and every later check would blame the size cap for a copy that was only
            # unreadable. Nothing is written, so a re-check retries the copy.
            raise BlobUnavailable(
                "the read's retained output is missing, unreadable, or no longer matches its hash",
                blob_ref_value=f"sha256:{source.retained.sha256}",
            )
        output, truncated_by_source = full, source.retained.truncated
    if not output:
        return None

    text = DEFAULT_REDACTOR.redact_text(output)
    redacted = has_redaction(text)
    numbered = strip_cat_n(text)
    whole_file_candidate = could_be_whole_file(
        numbered, truncated=truncated_by_source, limit=source.limit
    )

    git_reference: tuple[str, str] | None = None
    if whole_file_candidate and not redacted:
        git_reference = _git_blob_reference(session, run.id, numbered)

    if git_reference is not None:
        git_blob_id, git_text = git_reference
        data = git_text.encode("utf-8")
        sha256 = hashlib.sha256(data).hexdigest()
        blob = _existing_blob(session, run.id, sha256)
        if blob is None:
            blob = EvidenceBlob(
                run_id=run.id,
                sha256=sha256,
                git_blob=git_blob_id,
                storage_path=None,
                size_bytes=len(data),
                truncated=False,
                redacted=False,
            )
            session.add(blob)
            session.flush()
        read = EvidenceRead(
            run_id=run.id,
            result_event_id=source.result_event_id,
            call_event_id=source.call_event_id,
            path=source.path,
            line_first=numbered[0][0],
            line_last=numbered[-1][0],
            truncated=False,
            redacted=False,
            read_hash=git_blob_id,
            blob_id=blob.id,
        )
        session.add(read)
        session.flush()
        return read

    data = text.encode("utf-8")
    data, cut = retained_output.cut_at_line_boundary(data, retained_output.MAX_RETAINED_BYTES)
    truncated = truncated_by_source or cut
    sha256 = hashlib.sha256(data).hexdigest()

    blob = _existing_blob(session, run.id, sha256)
    if blob is None:
        stored = ctx.artifact_store.put_bytes(retained_output.evidence_key(run.id, sha256), data)
        blob = EvidenceBlob(
            run_id=run.id,
            sha256=stored.sha256,
            storage_path=stored.storage_path,
            size_bytes=stored.size_bytes,
            truncated=truncated,
            redacted=redacted,
        )
        session.add(blob)
        session.flush()

    retained_text = data.decode("utf-8", errors="replace")
    retained_numbered = strip_cat_n(retained_text)
    line_first = retained_numbered[0][0] if retained_numbered else None
    line_last = retained_numbered[-1][0] if retained_numbered else None
    # A read that could be the whole file still gets a read_hash (the LF-terminated
    # candidate) even when it matched no Git blob - a file the run created, a file outside
    # any repository, or a read whose recorded content was redacted. It is the Git blob id
    # of what the read returned; a read cut by its own limit never gets one.
    read_hash = blob_id_candidates(numbered)[0] if whole_file_candidate else None
    read = EvidenceRead(
        run_id=run.id,
        result_event_id=source.result_event_id,
        call_event_id=source.call_event_id,
        path=source.path,
        line_first=line_first,
        line_last=line_last,
        truncated=truncated,
        redacted=redacted,
        read_hash=read_hash,
        blob_id=blob.id,
    )
    session.add(read)
    session.flush()
    return read


def _load_git_reference(session: Session, blob: EvidenceBlob) -> LoadedRead:
    repo = _repo_for_run(session, blob.run_id)
    if not repo:
        raise BlobUnavailable("the run's repository is not recorded", blob_ref_value=blob_ref(blob))
    assert blob.git_blob is not None
    git = GuardedGit(Path(repo))
    try:
        result = git.run("cat-file", "blob", blob.git_blob)
    except Exception:  # a Git failure here is a pruned/unreachable blob
        raise BlobUnavailable(
            f"Git object {blob.git_blob} is no longer in {repo}", blob_ref_value=blob_ref(blob)
        ) from None
    if result.returncode != 0:
        raise BlobUnavailable(
            f"Git object {blob.git_blob} is no longer in {repo}", blob_ref_value=blob_ref(blob)
        )
    data = result.stdout.encode("utf-8", errors="replace")
    if hashlib.sha256(data).hexdigest() != blob.sha256:
        raise BlobUnavailable(
            "retained content no longer matches its hash", blob_ref_value=blob_ref(blob)
        )
    # Defense in depth: a no-op when the content that matched the redacted recording held
    # nothing redactable (why it became a Git reference at all). ``redacted`` reports what
    # this pass actually did, so a redaction rule added since the blob was stored is
    # reflected here rather than silently reported as "not redacted".
    text = DEFAULT_REDACTOR.redact_text(result.stdout)
    numbered = tuple(enumerate(text.splitlines(), start=1))
    return LoadedRead(
        numbered_lines=numbered,
        truncated=False,
        redacted=text != result.stdout,
        blob_ref=blob_ref(blob),
    )


def load_read(ctx: AppContext, session: Session, read: EvidenceRead) -> LoadedRead:
    """Load ``read``'s retained content, or raise :class:`BlobUnavailable`."""
    blob = session.get(EvidenceBlob, read.blob_id) if read.blob_id else None
    if blob is None:
        raise BlobUnavailable("no retained content", blob_ref_value=f"evidence_read:{read.id}")
    if blob.git_blob:
        return _load_git_reference(session, blob)
    if blob.storage_path is None:
        raise BlobUnavailable("no retained content", blob_ref_value=blob_ref(blob))
    try:
        with ctx.artifact_store.open(blob.storage_path) as fh:
            data = fh.read()
    except OSError:
        raise BlobUnavailable(
            f"the retained file for {blob_ref(blob)} is missing", blob_ref_value=blob_ref(blob)
        ) from None
    if hashlib.sha256(data).hexdigest() != blob.sha256:
        raise BlobUnavailable(
            "retained content no longer matches its hash", blob_ref_value=blob_ref(blob)
        )
    text = data.decode("utf-8", errors="replace")
    numbered = tuple(strip_cat_n(text))
    return LoadedRead(
        numbered_lines=numbered,
        truncated=blob.truncated,
        redacted=blob.redacted,
        blob_ref=blob_ref(blob),
    )
