"""Git isolation records, the file-change view, approved local commits, worktree cleanup.

Commits need an approval under the V1 safety policy: ``request_commit`` only
creates an :class:`ApprovalRequest` of type ``commit``. The ``git_commit`` approval
handler performs the commit when, and only when, that request is approved, and only as a
local commit on the run's AgentTape-owned branch inside its isolated worktree. Every
step is audited.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.catalog import ApprovalOut
from agenttape.application.schemas.git import (
    ChangedFileOut,
    ChangeSummary,
    CleanupOut,
    CommitState,
    DiffHunkOut,
    DiffLineOut,
    RunGitView,
    StartingRevision,
    WorkspaceOut,
)
from agenttape.application.schemas.runs import EvaluationResultOut
from agenttape.application.services.approvals import ApprovalService, register_approval_handler
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404
from agenttape.domain.enums import ApprovalStatus, EventType, RiskLevel, Severity
from agenttape.domain.errors import ConflictError, ValidationError
from agenttape.git.changes import is_clean
from agenttape.git.diffparse import TRUNCATION_MARKER, parse_unified_diff
from agenttape.git.manager import WorktreeManager
from agenttape.git.protected import matching_patterns
from agenttape.git.runner import GuardedGit, is_owned_branch
from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace
from agenttape.infrastructure.db.models import (
    ApprovalRequest,
    Artifact,
    EvaluationResult,
    FileChange,
    Run,
    RunAttempt,
)

COMMIT_APPROVAL_TYPE = "commit"
COMMIT_HANDLER = "git_commit"
VALIDATION_EVALUATOR_TYPE = "validation_command"
MAX_VIEW_DIFF_BYTES = 1024 * 1024
FALLBACK_AUTHOR = {"name": "AgentTape", "email": "agenttape@localhost"}


def _diff_omitted(value: object) -> Literal["size_cap", "count_cap"] | None:
    """The recorded reason an untracked file has no hunk, when it is one AgentTape wrote."""
    if value == "size_cap":
        return "size_cap"
    if value == "count_cap":
        return "count_cap"
    return None


def workspace_fingerprint(path: Path) -> str:
    """Hash of the worktree's uncommitted state, to detect edits after a commit request."""
    git = GuardedGit(path)
    status = git.run("status", "--porcelain=v1", "--untracked-files=all").stdout
    diff = git.run("diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD").stdout
    digest = hashlib.sha256()
    digest.update(status.encode("utf-8"))
    digest.update(b"\0")
    digest.update(diff.encode("utf-8"))
    for line in status.splitlines():
        if line.startswith("?? "):
            target = path / line[3:]
            try:
                if target.is_file() and target.stat().st_size <= 5 * 1024 * 1024:
                    digest.update(target.read_bytes())
            except OSError:
                continue
    return digest.hexdigest()


def _author_env(git: GuardedGit) -> dict[str, str]:
    """Use the repository's configured identity; fall back to a neutral AgentTape one."""
    env: dict[str, str] = {}
    name = git.run("config", "--get", "user.name").stdout.strip()
    email = git.run("config", "--get", "user.email").stdout.strip()
    if not name:
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = FALLBACK_AUTHOR["name"]
    if not email:
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = FALLBACK_AUTHOR["email"]
    return env


class GitService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.events = EventRecorder(ctx.settings, ctx.clock)
        self.manager = WorktreeManager(ctx.settings.workspaces_dir)

    # ================================================================== records

    @staticmethod
    def row(session: Session, run_id: str) -> RunWorkspace | None:
        return session.scalars(select(RunWorkspace).where(RunWorkspace.run_id == run_id)).first()

    def workspace_dict(self, run_id: str) -> dict[str, Any] | None:
        with self.ctx.session() as session:
            row = self.row(session, run_id)
            if row is None:
                return None
            return {
                "path": row.path,
                "isolated": row.isolated,
                "owned": row.owned,
                "status": row.status,
                "branch": row.branch,
                "base_commit": row.base_commit,
                "repo_path": row.repo_path,
            }

    def record_prepared(
        self,
        *,
        run_id: str,
        path: Path | None,
        isolated: bool,
        branch: str | None,
        base_commit: str | None,
        status: str,
        adapter_type: str | None,
        adapter_version: str | None,
        git_snapshot: dict[str, Any],
    ) -> None:
        """Insert the run's workspace record and emit the start-of-run Git events."""
        with self.ctx.session() as session:
            run = session.get(Run, run_id)
            if run is None or self.row(session, run_id) is not None:
                return
            project = (run.snapshot or {}).get("project") or {}
            now = self.ctx.clock.now()
            session.add(
                RunWorkspace(
                    run_id=run_id,
                    project_id=run.project_id,
                    repo_path=project.get("repo_path"),
                    path=str(path) if path else None,
                    isolated=isolated,
                    owned=isolated,
                    branch=branch,
                    base_commit=base_commit,
                    status=status,
                    adapter_type=adapter_type,
                    adapter_version=adapter_version,
                    details={},
                    is_demo=run.is_demo,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            if git_snapshot.get("is_repo") and git_snapshot.get("dirty"):
                count = int(git_snapshot.get("dirty_count") or 0)
                where = (
                    "The isolated worktree starts from the recorded commit without them."
                    if isolated
                    else "The agent works on top of them; the captured diff marks them "
                    "as pre-existing."
                )
                self.events.record(
                    session,
                    run_id,
                    EventType.WARNING,
                    f"Repository had {count} uncommitted file{'s' if count != 1 else ''} "
                    "when the run started",
                    {
                        "kind": "dirty_worktree",
                        "head": git_snapshot.get("head"),
                        "branch": git_snapshot.get("branch"),
                        "dirty_count": count,
                        "dirty_files": list(git_snapshot.get("dirty_files") or [])[:50],
                        "isolated": isolated,
                        "message": where,
                    },
                    severity=Severity.WARNING,
                )
            if isolated:
                self.events.record(
                    session,
                    run_id,
                    EventType.GIT_CHANGE,
                    f"Isolated worktree on {branch} from {str(base_commit)[:10]}",
                    {
                        "action": "worktree_created",
                        "path": str(path),
                        "branch": branch,
                        "base_commit": base_commit,
                        "files": [],
                    },
                )
            record_audit(
                session,
                self.ctx.clock,
                action="git.worktree.create" if isolated else "git.workdir.record",
                resource_type="run_workspace",
                resource_id=run_id,
                actor="agenttape",
                run_id=run_id,
                workspace_id=run.workspace_id,
                details={"path": str(path) if path else None, "branch": branch, "status": status},
            )

    def attempt_has_changes(self, run_id: str, attempt_id: str) -> bool:
        with self.ctx.session() as session:
            return (
                session.scalars(
                    select(FileChange.id).where(
                        FileChange.run_id == run_id, FileChange.attempt_id == attempt_id
                    )
                ).first()
                is not None
            )

    def flag_protected(
        self, run_id: str, attempt_id: str, patterns: list[str]
    ) -> list[dict[str, Any]]:
        """Mark adapter-recorded changes that touch protected paths. Returns the hits."""
        hits: list[dict[str, Any]] = []
        if not patterns:
            return hits
        with self.ctx.session() as session:
            rows = session.scalars(
                select(FileChange).where(
                    FileChange.run_id == run_id, FileChange.attempt_id == attempt_id
                )
            ).all()
            for fc in rows:
                matched = matching_patterns(fc.path, patterns) + (
                    matching_patterns(fc.old_path, patterns) if fc.old_path else []
                )
                if matched:
                    fc.is_protected = True
                    fc.metadata_ = {**(fc.metadata_ or {}), "protected_patterns": matched}
                    hits.append({"path": fc.path, "patterns": matched})
        return hits

    # ================================================================== view

    def _workspace_out(self, row: RunWorkspace) -> WorkspaceOut:
        out = WorkspaceOut(
            run_id=row.run_id,
            isolated=row.isolated,
            owned=row.owned,
            status=row.status,
            path=row.path,
            repo_path=row.repo_path,
            branch=row.branch,
            base_commit=row.base_commit,
            adapter_type=row.adapter_type,
            adapter_version=row.adapter_version,
            last_commit_sha=row.last_commit_sha,
            commits=list((row.details or {}).get("commits") or []),
            created_at=row.created_at,
            removed_at=row.removed_at,
        )
        if row.isolated and row.status == "active" and row.path:
            path = Path(row.path)
            out.exists = path.is_dir()
            if out.exists:
                try:
                    _clean, lines = is_clean(path)
                    out.uncommitted_count = len(lines)
                except (RuntimeError, OSError):
                    out.uncommitted_count = None
        return out

    def view(self, run_id: str) -> RunGitView:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            snap = run.snapshot or {}
            project = snap.get("project") or {}
            psettings = project.get("settings") or {}
            git_snap = snap.get("git")
            row = self.row(session, run_id)
            changes = session.scalars(
                select(FileChange).where(FileChange.run_id == run_id).order_by(FileChange.path)
            ).all()
            attempts = {
                a.id: a.attempt_number
                for a in session.scalars(select(RunAttempt).where(RunAttempt.run_id == run_id))
            }
            latest_attempt: int | None = None
            if changes:
                latest_attempt = max(attempts.get(c.attempt_id or "", 0) for c in changes)
                changes = [
                    c for c in changes if attempts.get(c.attempt_id or "", 0) == latest_attempt
                ]
            diff_id = next((c.diff_artifact_id for c in changes if c.diff_artifact_id), None)
            diff_text = ""
            if diff_id:
                art = session.get(Artifact, diff_id)
                if art is not None:
                    try:
                        with self.ctx.artifact_store.open(art.storage_path) as fh:
                            diff_text = fh.read(MAX_VIEW_DIFF_BYTES).decode(
                                "utf-8", errors="replace"
                            )
                    except OSError:
                        diff_text = ""
            parsed = {f.path: f for f in parse_unified_diff(diff_text)}
            files: list[ChangedFileOut] = []
            for c in changes:
                meta = c.metadata_ or {}
                pf = parsed.get(c.path) or (parsed.get(c.old_path) if c.old_path else None)
                files.append(
                    ChangedFileOut(
                        id=c.id,
                        path=c.path,
                        old_path=c.old_path,
                        change_type=c.change_type,
                        insertions=c.insertions,
                        deletions=c.deletions,
                        is_binary=c.is_binary,
                        size_bytes=c.size_bytes,
                        is_protected=c.is_protected,
                        protected_patterns=list(meta.get("protected_patterns") or []),
                        pre_existing_dirty=bool(meta.get("pre_existing_dirty")),
                        attempt_id=c.attempt_id,
                        hunks=[
                            DiffHunkOut(
                                header=h.header,
                                section=h.section,
                                lines=[
                                    DiffLineOut(kind=ln.kind, text=ln.text, old=ln.old, new=ln.new)
                                    for ln in h.lines
                                ],
                            )
                            for h in (pf.hunks if pf and not c.is_binary else [])
                        ],
                        diff_available=bool(pf and pf.hunks and not c.is_binary),
                        diff_truncated=bool(pf and pf.truncated),
                        diff_omitted=_diff_omitted(meta.get("diff_omitted")),
                    )
                )
            summary = ChangeSummary(
                files=len(files),
                insertions=sum(f.insertions or 0 for f in files),
                deletions=sum(f.deletions or 0 for f in files),
                binary_files=sum(1 for f in files if f.is_binary),
                protected_files=sum(1 for f in files if f.is_protected),
                added=sum(1 for f in files if f.change_type == "added"),
                modified=sum(1 for f in files if f.change_type == "modified"),
                deleted=sum(1 for f in files if f.change_type == "deleted"),
                renamed=sum(1 for f in files if f.change_type == "renamed"),
            )
            validations = session.scalars(
                select(EvaluationResult)
                .where(
                    EvaluationResult.run_id == run_id,
                    EvaluationResult.evaluator_type == VALIDATION_EVALUATOR_TYPE,
                )
                .order_by(EvaluationResult.created_at)
            ).all()
            if latest_attempt is not None:
                attempt_ids = {k for k, v in attempts.items() if v == latest_attempt}
                latest_validations = [v for v in validations if v.attempt_id in attempt_ids]
                validations = latest_validations or validations
            approvals = session.scalars(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.run_id == run_id,
                    ApprovalRequest.type == COMMIT_APPROVAL_TYPE,
                )
                .order_by(ApprovalRequest.created_at.desc())
            ).all()
            workspace = self._workspace_out(row) if row is not None else None
            available, reason = self._commit_availability(run, workspace, summary.files)
            pending = next((a for a in approvals if a.status == ApprovalStatus.PENDING.value), None)
            return RunGitView(
                run_id=run.id,
                project_name=project.get("name"),
                repo_path=project.get("repo_path"),
                git_isolation=bool(psettings.get("git_isolation")),
                protected_patterns=[str(p) for p in psettings.get("protected_paths") or []],
                validation_commands=[
                    [str(a) for a in cmd]
                    for cmd in psettings.get("validation_commands") or []
                    if isinstance(cmd, list)
                ],
                starting=StartingRevision.model_validate(git_snap) if git_snap else None,
                workspace=workspace,
                summary=summary,
                files=files,
                attempt_number=latest_attempt,
                diff_artifact_id=diff_id,
                diff_truncated=TRUNCATION_MARKER in diff_text,
                validations=[EvaluationResultOut.model_validate(v) for v in validations],
                commit=CommitState(
                    available=available and pending is None,
                    reason=reason
                    if not available
                    else ("A commit request is waiting for approval" if pending else None),
                    suggested_message=self._suggest_message(run),
                    pending=ApprovalOut.model_validate(pending) if pending else None,
                    history=[ApprovalOut.model_validate(a) for a in approvals],
                ),
            )

    @staticmethod
    def _suggest_message(run: Run) -> str:
        title = (run.title or "AgentTape run").strip()
        return f"{title}\n\nRecorded by AgentTape run {run.id}."

    @staticmethod
    def _commit_availability(
        run: Run, workspace: WorkspaceOut | None, file_count: int
    ) -> tuple[bool, str | None]:
        if workspace is None or not workspace.isolated:
            return False, (
                "Commits are only made inside an isolated worktree; enable Git isolation "
                "for the project and start a new run"
            )
        if workspace.status != "active" or not workspace.exists:
            return False, "The run's worktree has been removed"
        if run.status not in ("succeeded", "failed", "timed_out", "cancelled"):
            return False, "Wait until the run has finished"
        if not workspace.uncommitted_count:
            return False, "There are no uncommitted changes in the worktree"
        return True, None

    # ================================================================== commit

    def request_commit(self, run_id: str, *, message: str, actor: str) -> ApprovalOut:
        message = message.strip()
        if not message:
            raise ValidationError("The commit message must not be empty")
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            row = self.row(session, run_id)
            workspace = self._workspace_out(row) if row is not None else None
            files = session.scalars(select(FileChange).where(FileChange.run_id == run_id)).all()
            available, reason = self._commit_availability(run, workspace, len(files))
            if not available:
                raise ConflictError(reason or "A commit is not possible for this run")
            assert row is not None and row.path and row.branch
            pending = session.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.run_id == run_id,
                    ApprovalRequest.type == COMMIT_APPROVAL_TYPE,
                    ApprovalRequest.status == ApprovalStatus.PENDING.value,
                )
            ).first()
            if pending is not None:
                raise ConflictError(
                    "A commit request for this run is already waiting for approval",
                    details={"approval_id": pending.id},
                )
            path = Path(row.path)
            _clean, lines = is_clean(path)
            protected = sorted({f.path for f in files if f.is_protected})
            approval = ApprovalService(self.ctx).request(
                session,
                workspace_id=run.workspace_id,
                title=f"Commit {len(lines)} change(s) on {row.branch}",
                rationale=(
                    "Create a local commit in this run's isolated worktree. Nothing is pushed "
                    "or merged."
                    + (f" Protected paths changed: {', '.join(protected)}." if protected else "")
                ),
                type=COMMIT_APPROVAL_TYPE,
                risk_level=RiskLevel.HIGH if protected else RiskLevel.MEDIUM,
                requested_action={
                    "action": "git_commit",
                    "message": message,
                    "branch": row.branch,
                    "worktree": row.path,
                    "base_commit": row.base_commit,
                    "files": [line[3:] for line in lines[:200]],
                    "protected_files": protected,
                    "fingerprint": workspace_fingerprint(path),
                },
                run_id=run_id,
                resume_handler=COMMIT_HANDLER,
                requester=actor,
                is_demo=run.is_demo,
            )
            record_audit(
                session,
                self.ctx.clock,
                action="git.commit.request",
                resource_type="run",
                resource_id=run_id,
                actor=actor,
                run_id=run_id,
                workspace_id=run.workspace_id,
                details={"approval_id": approval.id, "branch": row.branch, "files": len(lines)},
            )
            return ApprovalOut.model_validate(approval)

    def perform_commit(self, session: Session, approval: ApprovalRequest, approved: bool) -> None:
        """Approval handler body (runs inside the decision's transaction). Never raises."""
        if approval.run_id is None:
            return
        run = session.get(Run, approval.run_id)
        if run is None:
            return
        action = dict(approval.requested_action or {})
        if not approved:
            record_audit(
                session,
                self.ctx.clock,
                action=f"git.commit.{approval.status}",
                resource_type="run",
                resource_id=run.id,
                actor=approval.reviewer or "agenttape",
                run_id=run.id,
                workspace_id=run.workspace_id,
                details={"approval_id": approval.id, "committed": False},
            )
            return
        try:
            sha, files = self._commit(session, run, approval, action)
        except (RuntimeError, OSError, ValueError, ConflictError) as exc:
            message = getattr(exc, "message", None) or str(exc)
            self.events.record(
                session,
                run.id,
                EventType.ERROR,
                f"Approved commit was not created: {message}"[:500],
                {"approval_id": approval.id, "error": message[:2000], "action": "commit_failed"},
                severity=Severity.ERROR,
            )
            record_audit(
                session,
                self.ctx.clock,
                action="git.commit.failed",
                resource_type="run",
                resource_id=run.id,
                actor=approval.reviewer or "agenttape",
                run_id=run.id,
                workspace_id=run.workspace_id,
                details={"approval_id": approval.id, "error": message[:1000]},
            )
            return
        first_line = str(action.get("message", "")).splitlines()[0][:120]
        self.events.record(
            session,
            run.id,
            EventType.GIT_CHANGE,
            f"Committed {sha[:10]} on {action.get('branch')}: {first_line}",
            {
                "action": "commit",
                "sha": sha,
                "branch": action.get("branch"),
                "message": action.get("message"),
                "approval_id": approval.id,
                "files": files[:200],
                "total_files": len(files),
            },
            severity=Severity.NOTICE,
        )
        record_audit(
            session,
            self.ctx.clock,
            action="git.commit.create",
            resource_type="run",
            resource_id=run.id,
            actor=approval.reviewer or "agenttape",
            run_id=run.id,
            workspace_id=run.workspace_id,
            details={
                "approval_id": approval.id,
                "sha": sha,
                "branch": action.get("branch"),
                "files": len(files),
            },
        )

    def _commit(
        self, session: Session, run: Run, approval: ApprovalRequest, action: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        row = self.row(session, run.id)
        if row is None or not row.isolated or not row.owned or row.status != "active":
            raise ConflictError("the run has no active AgentTape-owned worktree")
        if not row.path or not row.branch or not is_owned_branch(row.branch):
            raise ConflictError("the worktree record is incomplete")
        if action.get("branch") != row.branch or action.get("worktree") != row.path:
            raise ConflictError("the approved request does not match this run's worktree")
        path = Path(row.path)
        if not self.manager.owns_path(path) or not path.is_dir():
            raise ConflictError("the worktree is missing or outside the AgentTape directory")
        git = GuardedGit(path)
        current = git.out("symbolic-ref", "--quiet", "--short", "HEAD").strip()
        if current != row.branch:
            raise ConflictError(f"the worktree is on {current}, not {row.branch}")
        if action.get("fingerprint") and workspace_fingerprint(path) != action["fingerprint"]:
            raise ConflictError(
                "the worktree changed after the commit was requested; request a new commit "
                "so the approval covers what is committed"
            )
        message = str(action.get("message") or "").strip()
        if not message:
            raise ValueError("empty commit message")
        added = git.run("add", "--all", "--", ".")
        if added.returncode != 0:
            raise RuntimeError("git add failed: " + added.stderr.strip()[:300])
        staged = git.out("diff", "--cached", "--name-status", "--no-renames").splitlines()
        if not staged:
            raise ConflictError("there is nothing to commit")
        commit = git.run(
            "commit", "--quiet", "--file", "-", env=_author_env(git), input_text=message + "\n"
        )
        if commit.returncode != 0:
            raise RuntimeError(
                "git commit failed: " + (commit.stderr or commit.stdout).strip()[:300]
            )
        sha = git.out("rev-parse", "HEAD").strip()
        kinds = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}
        files = [
            {"path": line.split("\t", 1)[-1], "change_type": kinds.get(line[:1], "modified")}
            for line in staged
            if "\t" in line
        ]
        now = self.ctx.clock.now()
        details = dict(row.details or {})
        commits = list(details.get("commits") or [])
        commits.append(
            {
                "sha": sha,
                "message": message,
                "approval_id": approval.id,
                "reviewer": approval.reviewer,
                "files": len(files),
                "created_at": now.isoformat(),
            }
        )
        details["commits"] = commits
        row.details = details
        row.last_commit_sha = sha
        row.updated_at = now
        return sha, files

    # ================================================================== cleanup

    def cleanup(self, run_id: str, *, actor: str) -> CleanupOut:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            row = self.row(session, run_id)
            if row is None or not row.isolated or not row.owned:
                raise ConflictError("This run did not use an AgentTape-owned worktree")
            if row.status == "removed":
                raise ConflictError("The worktree was already removed")
            if run.status in ("queued", "preparing", "running", "cancelling"):
                raise ConflictError("The run is still active; wait until it finishes")
            pending = session.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.run_id == run_id,
                    ApprovalRequest.type == COMMIT_APPROVAL_TYPE,
                    ApprovalRequest.status == ApprovalStatus.PENDING.value,
                )
            ).first()
            if pending is not None:
                raise ConflictError("A commit request is waiting for approval; decide it first")
            result = self.manager.cleanup_worktree(
                repo_path=Path(row.repo_path) if row.repo_path else None,
                path=Path(row.path or ""),
                branch=row.branch,
                base_commit=row.base_commit,
            )
            out = CleanupOut(**result.__dict__)
            now = self.ctx.clock.now()
            details = dict(row.details or {})
            history = list(details.get("cleanup") or [])
            history.append({"at": now.isoformat(), "actor": actor, **out.model_dump()})
            details["cleanup"] = history[-20:]
            row.details = details
            if result.removed:
                row.status = "removed"
                row.removed_at = now
            row.updated_at = now
            self.events.record(
                session,
                run_id,
                EventType.GIT_CHANGE if result.removed else EventType.WARNING,
                (
                    f"Worktree removed ({'branch deleted' if result.branch_deleted else 'branch ' + (result.branch_kept_reason or 'kept')})"
                    if result.removed
                    else f"Worktree cleanup refused: {result.message}"
                )[:300],
                {"action": "worktree_cleanup", **out.model_dump(), "files": []},
                severity=Severity.INFO if result.removed else Severity.WARNING,
            )
            record_audit(
                session,
                self.ctx.clock,
                action="git.worktree.remove" if result.removed else "git.worktree.remove_refused",
                resource_type="run_workspace",
                resource_id=run_id,
                actor=actor,
                run_id=run_id,
                workspace_id=run.workspace_id,
                details=out.model_dump(),
            )
            return out


def _commit_handler(
    ctx: AppContext, session: Session, approval: ApprovalRequest, approved: bool
) -> None:
    GitService(ctx).perform_commit(session, approval, approved)


register_approval_handler(COMMIT_HANDLER, _commit_handler)
