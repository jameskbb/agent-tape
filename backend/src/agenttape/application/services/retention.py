"""Data retention: settings, a dry-run preview, and cleanup of AgentTape-owned data only.

``preview`` lists the finished runs older than the configured age (never active runs,
runs tagged with the keep tag, or runs with a pending approval), the bytes their
artifacts, logs, and workspaces use, and orphaned directories inside the data dir that no
run references. ``execute`` deletes only what the caller previewed *and* is still
eligible when re-checked, and never anything outside ``<data_dir>``: artifact, log, and
workspace directories are resolved with traversal-safe joins, and isolated worktrees go
through :class:`WorktreeManager`, which refuses to lose uncommitted work.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select

from agenttape.application.context import AppContext
from agenttape.application.schemas.git import (
    RetentionExecute,
    RetentionOrphan,
    RetentionPreview,
    RetentionResult,
    RetentionRunItem,
    RetentionSettings,
)
from agenttape.application.services.audit import record_audit
from agenttape.application.services.workspaces import get_app_setting, set_app_setting
from agenttape.domain.enums import TERMINAL_STATUS_VALUES, ApprovalStatus
from agenttape.domain.errors import PolicyViolationError, ValidationError
from agenttape.git.changes import is_clean
from agenttape.git.manager import WorktreeManager
from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace
from agenttape.infrastructure.db.models import ApprovalRequest, Artifact, Run
from agenttape.security.paths import is_within, safe_join

SETTINGS_KEY = "retention"
RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_PREVIEW_RUNS = 2000


def dir_size(path: Path, *, limit_entries: int = 200_000) -> int:
    """Bytes used by regular files under ``path`` (symlinks are not followed)."""
    total = 0
    seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen > limit_entries:
                        return total
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
    return total


class RetentionService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.manager = WorktreeManager(ctx.settings.workspaces_dir)

    # ================================================================== settings

    def get_settings(self) -> RetentionSettings:
        with self.ctx.session() as session:
            stored = get_app_setting(session, SETTINGS_KEY)
        return RetentionSettings.model_validate(stored or {})

    def update_settings(self, value: RetentionSettings, *, actor: str) -> RetentionSettings:
        bad = [s for s in value.statuses if s not in TERMINAL_STATUS_VALUES]
        if bad:
            raise ValidationError(
                "Only finished statuses can be cleaned up", details={"invalid": bad}
            )
        with self.ctx.session() as session:
            set_app_setting(session, self.ctx.clock, SETTINGS_KEY, value.model_dump())
            record_audit(
                session,
                self.ctx.clock,
                action="retention.settings.update",
                resource_type="settings",
                resource_id=SETTINGS_KEY,
                actor=actor,
                details=value.model_dump(),
            )
        return value

    # ================================================================== paths

    def _run_dirs(self, run_id: str) -> dict[str, Path]:
        if not RUN_ID_RE.match(run_id):
            raise PolicyViolationError("Unexpected run id format", details={"run_id": run_id})
        ws = self.settings.workspaces_dir
        return {
            "artifacts": safe_join(self.settings.artifacts_dir, f"runs/{run_id}"),
            "logs": safe_join(self.settings.logs_dir, run_id),
            "demo_clone": safe_join(ws, f"demo/{run_id}"),
            "scratch": safe_join(ws, f"runs/{run_id}"),
        }

    def _orphan_candidates(self) -> list[tuple[str, Path]]:
        s = self.settings
        roots = [
            ("artifacts", s.artifacts_dir / "runs"),
            ("logs", s.logs_dir),
            ("demo_clone", s.workspaces_dir / "demo"),
            ("scratch", s.workspaces_dir / "runs"),
            ("worktree", self.manager.root),
        ]
        out: list[tuple[str, Path]] = []
        for kind, root in roots:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir() and not child.is_symlink() and RUN_ID_RE.match(child.name):
                    out.append((kind, child))
        return out

    # ================================================================== preview

    def preview(self, settings: RetentionSettings | None = None) -> RetentionPreview:
        cfg = settings or self.get_settings()
        now = self.ctx.clock.now()
        cutoff = now - timedelta(days=cfg.max_age_days)
        statuses = [s for s in cfg.statuses if s in TERMINAL_STATUS_VALUES]
        items: list[RetentionRunItem] = []
        with self.ctx.session() as session:
            stmt = select(Run).where(
                Run.status.in_(statuses),
                or_(
                    Run.finished_at < cutoff,
                    (Run.finished_at.is_(None)) & (Run.created_at < cutoff),
                ),
            )
            if not cfg.include_demo:
                stmt = stmt.where(Run.is_demo.is_(False))
            runs = session.scalars(stmt.order_by(Run.created_at).limit(MAX_PREVIEW_RUNS)).all()
            pending = set(
                session.scalars(
                    select(ApprovalRequest.run_id).where(
                        ApprovalRequest.status == ApprovalStatus.PENDING.value,
                        ApprovalRequest.run_id.is_not(None),
                    )
                )
            )
            art_bytes: dict[str, int] = {
                str(run_id): int(total or 0)
                for run_id, total in session.execute(
                    select(Artifact.run_id, func.coalesce(func.sum(Artifact.size_bytes), 0))
                    .where(Artifact.run_id.in_([r.id for r in runs]))
                    .group_by(Artifact.run_id)
                ).all()
            }
            workspaces = {
                w.run_id: w
                for w in session.scalars(
                    select(RunWorkspace).where(RunWorkspace.run_id.in_([r.id for r in runs]))
                )
            }
            for run in runs:
                if cfg.keep_tag and cfg.keep_tag in (run.tags or []):
                    continue
                dirs = self._run_dirs(run.id)
                log_bytes = dir_size(dirs["logs"])
                work_bytes = dir_size(dirs["demo_clone"]) + dir_size(dirs["scratch"])
                blocked: str | None = None
                worktree_path: str | None = None
                ws = workspaces.get(run.id)
                if ws is not None and ws.isolated and ws.owned and ws.status == "active":
                    worktree_path = ws.path
                    path = Path(ws.path or "")
                    if path.is_dir():
                        work_bytes += dir_size(path)
                        if not cfg.remove_worktrees:
                            blocked = "keeps an isolated worktree (worktree removal is off)"
                        else:
                            try:
                                clean, lines = is_clean(path)
                            except RuntimeError:
                                clean, lines = False, []
                            if not clean:
                                blocked = (
                                    f"its worktree has {len(lines)} uncommitted change(s); "
                                    "commit or copy them first"
                                )
                if run.id in pending:
                    blocked = "has a pending approval"
                # The run's evidence directory (retained reads, and a read's output kept
                # beyond the event cap) has no Artifact rows, but execute deletes it too.
                artifact_bytes = int(art_bytes.get(run.id, 0) or 0) + dir_size(
                    dirs["artifacts"] / "evidence"
                )
                items.append(
                    RetentionRunItem(
                        run_id=run.id,
                        title=run.title,
                        status=run.status,
                        finished_at=run.finished_at,
                        is_demo=run.is_demo,
                        artifact_bytes=artifact_bytes,
                        log_bytes=log_bytes,
                        worktree_bytes=work_bytes,
                        total_bytes=artifact_bytes + log_bytes + work_bytes,
                        worktree_path=worktree_path,
                        blocked=blocked,
                    )
                )
            known_runs = set(session.scalars(select(Run.id)))
            active_paths = {
                w.path
                for w in session.scalars(
                    select(RunWorkspace).where(RunWorkspace.status == "active")
                )
                if w.path
            }
        orphans: list[RetentionOrphan] = []
        if cfg.include_orphans:
            for kind, path in self._orphan_candidates():
                if kind == "worktree":
                    if str(path) in active_paths:
                        continue
                elif path.name in known_runs:
                    continue
                blocked = None
                if kind == "worktree":
                    try:
                        clean, lines = is_clean(path)
                        if not clean:
                            blocked = (
                                f"{len(lines)} uncommitted change(s) in this orphaned worktree"
                            )
                    except (RuntimeError, OSError):
                        blocked = "not a readable Git worktree"
                orphans.append(
                    RetentionOrphan(
                        kind=kind, path=str(path), bytes=dir_size(path), blocked=blocked
                    )
                )
        eligible = [i for i in items if not i.blocked]
        return RetentionPreview(
            settings=cfg,
            cutoff=cutoff,
            generated_at=now,
            runs=items,
            orphans=orphans,
            total_runs=len(eligible),
            total_bytes=sum(i.total_bytes for i in eligible)
            + sum(o.bytes for o in orphans if not o.blocked),
            blocked_runs=len(items) - len(eligible),
        )

    # ================================================================== execute

    def execute(self, body: RetentionExecute, *, actor: str) -> RetentionResult:
        plan = self.preview()
        eligible = {i.run_id: i for i in plan.runs if not i.blocked}
        skipped: list[dict[str, Any]] = []
        for run_id in body.run_ids:
            if run_id not in eligible:
                blocked = next((i.blocked for i in plan.runs if i.run_id == run_id), None)
                skipped.append({"run_id": run_id, "reason": blocked or "no longer eligible"})
        removed_paths: list[str] = []
        freed = 0
        to_delete: list[str] = []
        with self.ctx.session() as session:
            rows = {
                w.run_id: (w.repo_path, w.branch, w.base_commit)
                for w in session.scalars(
                    select(RunWorkspace).where(RunWorkspace.run_id.in_(list(eligible)))
                )
            }
        for run_id in body.run_ids:
            item = eligible.get(run_id)
            if item is None:
                continue
            if item.worktree_path:
                repo, branch, base = rows.get(run_id, (None, None, None))
                result = self.manager.cleanup_worktree(
                    repo_path=Path(repo) if repo else None,
                    path=Path(item.worktree_path),
                    branch=branch,
                    base_commit=base,
                )
                if not result.removed:
                    skipped.append({"run_id": run_id, "reason": result.message})
                    continue
                removed_paths.append(item.worktree_path)
            for path in self._run_dirs(run_id).values():
                if path.exists() and self._inside_data_dir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    removed_paths.append(str(path))
            freed += item.total_bytes
            to_delete.append(run_id)
        if to_delete:
            with self.ctx.session() as session:
                for start in range(0, len(to_delete), 500):
                    chunk = to_delete[start : start + 500]
                    session.execute(delete(Run).where(Run.id.in_(chunk)))
        orphans = {o.path: o for o in plan.orphans if not o.blocked}
        for raw in body.orphan_paths:
            orphan = orphans.get(raw)
            if orphan is None:
                skipped.append({"path": raw, "reason": "not an eligible orphan"})
                continue
            path = Path(orphan.path)
            if orphan.kind == "worktree":
                result = self.manager.cleanup_worktree(
                    repo_path=None, path=path, branch=None, base_commit=None
                )
                if not result.removed:
                    skipped.append({"path": raw, "reason": result.message})
                    continue
            elif self._inside_data_dir(path):
                shutil.rmtree(path, ignore_errors=True)
            removed_paths.append(orphan.path)
            freed += orphan.bytes
        with self.ctx.session() as session:
            record_audit(
                session,
                self.ctx.clock,
                action="retention.cleanup",
                resource_type="retention",
                resource_id=None,
                actor=actor,
                details={
                    "deleted_runs": len(to_delete),
                    "run_ids": to_delete[:200],
                    "removed_paths": len(removed_paths),
                    "skipped": skipped[:100],
                    "freed_bytes": freed,
                },
            )
        return RetentionResult(
            deleted_runs=to_delete, skipped=skipped, removed_paths=removed_paths, freed_bytes=freed
        )

    def _inside_data_dir(self, path: Path) -> bool:
        data = self.settings.data_dir.resolve()
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return is_within(resolved, data) and resolved != data
