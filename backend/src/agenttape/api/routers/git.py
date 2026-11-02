"""Git isolation: a run's file changes and diff, approved commits, worktree cleanup."""

from __future__ import annotations

from fastapi import APIRouter

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.catalog import ApprovalOut
from agenttape.application.schemas.git import CleanupOut, CommitRequest, RunGitView
from agenttape.application.services.git import GitService

router = APIRouter(tags=["git"])


@router.get(
    "/runs/{run_id}/git",
    response_model=RunGitView,
    summary="Starting revision, worktree, changed files with parsed diff, validations, commits",
)
def run_git(run_id: str, ctx: Ctx) -> RunGitView:
    return GitService(ctx).view(run_id)


@router.post(
    "/runs/{run_id}/git/commit",
    response_model=ApprovalOut,
    status_code=201,
    summary="Request a local commit in the run's isolated worktree (needs an approval)",
)
def request_commit(run_id: str, body: CommitRequest, ctx: Ctx, actor: Actor) -> ApprovalOut:
    return GitService(ctx).request_commit(run_id, message=body.message, actor=actor)


@router.post(
    "/runs/{run_id}/git/cleanup",
    response_model=CleanupOut,
    summary="Remove the run's AgentTape-owned worktree if it has no uncommitted work",
)
def cleanup_worktree(run_id: str, ctx: Ctx, actor: Actor) -> CleanupOut:
    return GitService(ctx).cleanup(run_id, actor=actor)
