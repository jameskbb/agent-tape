"""Worker hooks for Git isolation: Git workdir preparation and post-attempt change capture.

Registered on import (the worker imports this through
``agenttape.worker.executors.git_workspace``):

* the workdir preparer records where every run executes, creates an AgentTape-owned
  worktree when the project enables ``git_isolation``, records the adapter version (for
  replay drift warnings), and emits the dirty-tree warning;
* the after-attempt hook captures changed files, the size-capped diff artifact,
  protected-path warnings, and runs the project's validation commands (argument arrays,
  judged by the V1 safety policy first) as ``command`` events plus ``validation_command``
  evaluations.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agenttape.adapters.base import (
    AdapterResult,
    CancellationToken,
    ExecutionContext,
    FileChangeDraft,
)
from agenttape.adapters.process import CommandSpec, run_process
from agenttape.application.context import AppContext
from agenttape.application.services.evaluations import EvaluationService
from agenttape.application.services.git import VALIDATION_EVALUATOR_TYPE, GitService
from agenttape.domain.enums import EvaluationStatus, EventType, FailureCategory, Severity
from agenttape.domain.policy import Verdict, check_command
from agenttape.evaluations.base import EvaluationOutcome, EvaluationSpec
from agenttape.git.changes import capture_changes
from agenttape.git.manager import IsolationError, WorktreeManager
from agenttape.git.protected import matching_patterns
from agenttape.worker import hooks

DEFAULT_VALIDATION_TIMEOUT_SECONDS = 300.0
MAX_VALIDATION_COMMANDS = 20


def _project(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    project = dict(snapshot.get("project") or {})
    return project, dict(project.get("settings") or {})


def _adapter_version(ctx: AppContext, adapter_type: str | None) -> str | None:
    if not adapter_type or not ctx.adapters.has(adapter_type):
        return None
    try:
        return ctx.adapters.get(adapter_type).discover().version
    except Exception:  # discovery must never break a run
        return None


async def prepare_git_workdir(ctx: AppContext, req: hooks.WorkdirRequest) -> hooks.PreparedWorkdir:
    service = GitService(ctx)
    existing = await asyncio.to_thread(service.workspace_dict, req.run_id)
    if existing is not None:
        path = Path(existing["path"]) if existing["path"] else None
        if existing["isolated"]:
            if existing["status"] != "active" or path is None or not path.is_dir():
                raise IsolationError(
                    "This run's isolated worktree no longer exists; start a replay instead"
                )
            return hooks.PreparedWorkdir(
                path=path, isolated=True, metadata={"branch": existing["branch"]}
            )
        if path is not None and path.is_dir():
            return hooks.PreparedWorkdir(path=path)
        return await hooks.default_workdir(ctx, req)

    _project_snap, settings = _project(req.snapshot)
    git_snap = dict(req.snapshot.get("git") or {})
    adapter_type = (req.snapshot.get("agent_profile") or {}).get("adapter_type")
    version = await asyncio.to_thread(_adapter_version, ctx, adapter_type)
    isolate = bool(settings.get("git_isolation"))
    project_path = req.project_path

    if project_path is None or not project_path.is_dir():
        if isolate:
            raise IsolationError("Git isolation is enabled but the project directory is missing")
        prepared = await hooks.default_workdir(ctx, req)
        await asyncio.to_thread(
            service.record_prepared,
            run_id=req.run_id,
            path=prepared.path,
            isolated=False,
            branch=None,
            base_commit=None,
            status="scratch",
            adapter_type=adapter_type,
            adapter_version=version,
            git_snapshot=git_snap,
        )
        return prepared

    if isolate:
        if not git_snap.get("is_repo"):
            raise IsolationError(
                f"Git isolation is enabled but {project_path} is not a Git repository"
            )
        manager = WorktreeManager(ctx.settings.workspaces_dir)
        workspace = await asyncio.to_thread(
            manager.prepare,
            run_id=req.run_id,
            project_path=project_path,
            isolate=True,
            base_commit=git_snap.get("head"),
        )
        await asyncio.to_thread(
            service.record_prepared,
            run_id=req.run_id,
            path=workspace.path,
            isolated=True,
            branch=workspace.branch,
            base_commit=workspace.base_commit,
            status="active",
            adapter_type=adapter_type,
            adapter_version=version,
            git_snapshot=git_snap,
        )
        return hooks.PreparedWorkdir(
            path=workspace.path,
            isolated=True,
            metadata={"branch": workspace.branch, "base_commit": workspace.base_commit},
        )

    await asyncio.to_thread(
        service.record_prepared,
        run_id=req.run_id,
        path=project_path,
        isolated=False,
        branch=git_snap.get("branch"),
        base_commit=git_snap.get("head"),
        status="shared",
        adapter_type=adapter_type,
        adapter_version=version,
        git_snapshot=git_snap,
    )
    return hooks.PreparedWorkdir(path=project_path)


# --------------------------------------------------------------------------- after attempt


async def capture_after_attempt(
    ctx: AppContext, ec: ExecutionContext, result: AdapterResult
) -> AdapterResult:
    if result.outcome == "needs_approval":
        return result  # paused mid-attempt; capture once it finishes
    service = GitService(ctx)
    workspace = await asyncio.to_thread(service.workspace_dict, ec.run.id)
    if workspace is None:
        return result
    _project_snap, settings = _project(ec.run.snapshot)
    patterns = [str(p) for p in settings.get("protected_paths") or [] if isinstance(p, str)]

    if await asyncio.to_thread(service.attempt_has_changes, ec.run.id, ec.attempt.id):
        # The adapter recorded its own changes (e.g. the Demo Agent in its own clone):
        # apply the project's protected-path policy to them.
        flagged = await asyncio.to_thread(
            service.flag_protected, ec.run.id, ec.attempt.id, patterns
        )
        await _protected_warning(ec, flagged)
        return result

    git_snap = dict(ec.run.snapshot.get("git") or {})
    if workspace["isolated"]:
        directory = Path(workspace["path"])
        base = workspace["base_commit"]
    else:
        if not git_snap.get("is_repo") or not workspace["path"]:
            return result
        directory = Path(workspace["path"])
        base = git_snap.get("head")
    if not base or not directory.is_dir():
        return result

    captured = await asyncio.to_thread(capture_changes, directory, base)
    if not captured.entries:
        return result
    pre_dirty = set() if workspace["isolated"] else set(git_snap.get("dirty_files") or [])
    diff_id = await ec.artifacts.write_text(
        "changes.diff",
        captured.diff_text,
        kind="diff",
        media_type="text/x-diff",
        metadata={
            "source": "git_capture",
            "base_commit": base,
            "truncated": captured.truncated,
            "skipped_untracked": captured.skipped_untracked[:50],
            "skipped_untracked_count": len(captured.skipped_untracked),
        },
    )
    drafts: list[FileChangeDraft] = []
    hits: list[dict[str, Any]] = []
    for e in captured.entries:
        matched = matching_patterns(e.path, patterns) + (
            matching_patterns(e.old_path, patterns) if e.old_path else []
        )
        if matched:
            hits.append({"path": e.path, "patterns": matched})
        drafts.append(
            FileChangeDraft(
                path=e.path,
                change_type=e.change_type,
                old_path=e.old_path,
                insertions=e.insertions,
                deletions=e.deletions,
                is_binary=e.is_binary,
                size_bytes=captured.sizes.get(e.path),
                is_protected=bool(matched),
                diff_artifact_id=diff_id,
                metadata={
                    "source": "git_capture",
                    "base_commit": base,
                    "protected_patterns": matched,
                    "pre_existing_dirty": e.path in pre_dirty,
                    **(
                        {"diff_omitted": captured.diff_omitted[e.path]}
                        if e.path in captured.diff_omitted
                        else {}
                    ),
                },
            )
        )
    await ec.files.record(drafts)
    await _protected_warning(ec, hits)
    await _run_validations(ctx, ec, directory, settings)
    return result


async def _protected_warning(ec: ExecutionContext, hits: list[dict[str, Any]]) -> None:
    if not hits:
        return
    paths = [h["path"] for h in hits]
    await ec.events.event(
        EventType.WARNING,
        f"Protected path{'s' if len(paths) != 1 else ''} changed: {', '.join(paths[:5])}"
        + (f" and {len(paths) - 5} more" if len(paths) > 5 else ""),
        {"kind": "protected_path", "files": hits[:200], "count": len(hits)},
        severity=Severity.WARNING,
    )


async def _run_validations(
    ctx: AppContext, ec: ExecutionContext, directory: Path, settings: dict[str, Any]
) -> None:
    commands = settings.get("validation_commands") or []
    if not isinstance(commands, list) or not commands or ec.cancel.cancelled:
        return
    timeout = float(
        settings.get("validation_timeout_seconds") or DEFAULT_VALIDATION_TIMEOUT_SECONDS
    )
    env = ec.build_env([str(n) for n in settings.get("env_allowlist") or []])
    raw_allowlist = settings.get("command_allowlist")
    allowlist = raw_allowlist if isinstance(raw_allowlist, list) else None
    evaluations = EvaluationService(ctx)
    for index, argv in enumerate(commands[:MAX_VALIDATION_COMMANDS]):
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            await ec.events.event(
                EventType.WARNING,
                f"Validation command {index + 1} skipped: it must be an argument array",
                {"kind": "validation_skipped", "command": argv},
                severity=Severity.WARNING,
            )
            continue
        spec = EvaluationSpec(
            type=VALIDATION_EVALUATOR_TYPE,
            name="Validation: " + " ".join(argv)[:180],
            config={"argv": argv, "cwd": "worktree" if "workspaces" in str(directory) else "repo"},
            required=False,
        )
        # The project's own validation commands are trusted for the allowlist (an exact
        # match), never for the V1 hard-disallow list: push, merge, destructive reset,
        # branch deletion, and options that run other programs are refused. They run
        # unattended after every attempt, so anything that would need an approval (a
        # commit, a fetch) is refused too.
        decision = check_command(argv, allowlist=allowlist, trusted_commands=[argv])
        if decision.verdict != Verdict.ALLOW:
            await ec.events.event(
                EventType.ERROR,
                f"Validation command {index + 1} refused by the V1 safety policy: "
                f"{' '.join(argv)[:160]}",
                {"kind": "validation_refused", "argv": argv, "policy": decision.as_dict()},
                severity=Severity.ERROR,
            )
            await asyncio.to_thread(
                evaluations.persist,
                run_id=ec.run.id,
                attempt_id=ec.attempt.id,
                step_run_id=None,
                spec=spec,
                outcome=EvaluationOutcome(
                    status=EvaluationStatus.ERROR,
                    explanation=f"Not run: {decision.reason}",
                    evidence={"argv": argv, "policy": decision.as_dict()},
                ),
                duration_ms=0,
                evaluator_version="1",
            )
            continue
        token = CancellationToken()
        vctx = dataclasses.replace(ec, cancel=token)

        async def deadline(t: CancellationToken = token) -> None:
            if not await t.sleep(timeout):
                t.cancel(FailureCategory.TIMEOUT, f"Validation command exceeded {timeout:g}s")

        timer = asyncio.create_task(deadline())
        try:
            proc = await run_process(
                CommandSpec(
                    argv=argv, cwd=directory, env=env, display="validate: " + " ".join(argv)
                ),
                vctx,
            )
        finally:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
        if proc.spawn_error:
            status, explanation = EvaluationStatus.ERROR, f"Could not start: {proc.spawn_error}"
        elif proc.stopped_reason is not None:
            status, explanation = EvaluationStatus.ERROR, f"Stopped after {timeout:g}s"
        elif proc.exit_code == 0:
            status, explanation = EvaluationStatus.PASS, "Exited with code 0"
        else:
            status, explanation = EvaluationStatus.FAIL, f"Exited with code {proc.exit_code}"
        await asyncio.to_thread(
            evaluations.persist,
            run_id=ec.run.id,
            attempt_id=ec.attempt.id,
            step_run_id=None,
            spec=spec,
            outcome=EvaluationOutcome(
                status=status,
                explanation=explanation,
                evidence={
                    "argv": argv,
                    "exit_code": proc.exit_code,
                    "duration_ms": proc.duration_ms,
                    "stdout_bytes": proc.stdout_bytes,
                    "stderr_bytes": proc.stderr_bytes,
                },
            ),
            duration_ms=proc.duration_ms,
            evaluator_version="1",
        )


hooks.register_workdir_preparer(prepare_git_workdir)
hooks.register_after_attempt(capture_after_attempt)
