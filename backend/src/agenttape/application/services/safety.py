"""Typed approval requests for the V1 safety policy.

Other services call these instead of building approval rows by hand. Each request records a
``fingerprint`` of the exact action (a hash of its canonical JSON); before acting, the
caller verifies the approval with :meth:`SafetyApprovals.require_approved`, which checks
status, type, and fingerprint - so an approval for commit message A can never authorise
commit message B.

Typical use (Git track, committing after approval)::

    safety = SafetyApprovals(ctx)
    with ctx.session() as session:
        approval = safety.request_commit(
            session, run_id=run.id, repo_path=str(worktree), branch="agenttape/run-123",
            message="Fix greeting", files=["src/greeter.py"],
            resume_handler="git_commit",          # your handler, see register_approval_handler
        )
    ...
    # later, inside your "git_commit" approval handler or an API call:
    approval = safety.require_approved(
        session, approval_id, ApprovalType.GIT_COMMIT,
        action=commit_action(repo_path, branch, message, files),
    )

Hard-disallowed actions (push, merge, force push, destructive reset, deleting branches or
worktrees AgentTape did not create, host deletion, secret display) cannot be requested at
all: :meth:`SafetyApprovals.check_command` returns ``deny`` for them and
:meth:`SafetyApprovals.request_elevated_command` raises ``PolicyViolationError`` instead of
creating an approval.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.common import get_or_404, hash_json
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.domain.enums import ApprovalStatus, RiskLevel
from agenttape.domain.errors import PolicyViolationError
from agenttape.domain.policy import (
    DEFAULT_RISK,
    ApprovalType,
    PolicyDecision,
    Verdict,
    check_budget_change,
    check_command,
    check_protected_write,
)
from agenttape.infrastructure.db.models import ApprovalRequest, Run


def action_fingerprint(action: dict[str, Any]) -> str:
    """Stable hash of an action; the same action always has the same fingerprint."""
    return hash_json({k: v for k, v in action.items() if k != "fingerprint"})


def commit_action(
    repo_path: str, branch: str | None, message: str, files: Sequence[str]
) -> dict[str, Any]:
    return {
        "action": "git_commit",
        "repo_path": repo_path,
        "branch": branch,
        "message": message,
        "files": sorted(files),
    }


class SafetyApprovals:
    """The approval API for risky transitions. Joins the caller's transaction."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.approvals = ApprovalService(ctx)

    # ------------------------------------------------------------------ generic

    def request(
        self,
        session: Session,
        *,
        type: ApprovalType,
        title: str,
        rationale: str,
        action: dict[str, Any],
        run_id: str | None = None,
        attempt_id: str | None = None,
        step_run_id: str | None = None,
        node_key: str | None = None,
        risk_level: RiskLevel | None = None,
        resume_handler: str = "none",
        requester: str = "agenttape",
        expires_in_seconds: float | None = None,
    ) -> ApprovalRequest:
        workspace_id, is_demo = self._workspace(session, run_id)
        requested = {**action, "fingerprint": action_fingerprint(action)}
        return self.approvals.request(
            session,
            workspace_id=workspace_id,
            title=title,
            rationale=rationale,
            type=type.value,
            risk_level=risk_level or DEFAULT_RISK[type],
            requested_action=requested,
            run_id=run_id,
            attempt_id=attempt_id,
            step_run_id=step_run_id,
            node_key=node_key,
            resume_handler=resume_handler,
            requester=requester,
            expires_in_seconds=expires_in_seconds,
            is_demo=is_demo,
        )

    def _workspace(self, session: Session, run_id: str | None) -> tuple[str, bool]:
        if run_id:
            run = get_or_404(session, Run, run_id, "run")
            return run.workspace_id, run.is_demo
        return default_workspace_id(session, self.ctx.clock), False

    def require_approved(
        self,
        session: Session,
        approval_id: str,
        type: ApprovalType,
        *,
        action: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Return the approval if it is approved, of ``type``, and (when given) for exactly
        ``action``; raise :class:`PolicyViolationError` otherwise."""
        approval = get_or_404(session, ApprovalRequest, approval_id, "approval")
        if approval.type != type.value:
            raise PolicyViolationError(
                f"Approval {approval_id} is for '{approval.type}', not '{type.value}'"
            )
        if approval.status != ApprovalStatus.APPROVED.value:
            raise PolicyViolationError(
                f"Approval {approval_id} is {approval.status}, not approved",
                details={"status": approval.status},
            )
        if action is not None:
            expected = (approval.requested_action or {}).get("fingerprint")
            if expected != action_fingerprint(action):
                raise PolicyViolationError(
                    "The approved action does not match the action being performed",
                    details={"approval_id": approval_id},
                )
        return approval

    # ------------------------------------------------------------------ typed requests

    def request_commit(
        self,
        session: Session,
        *,
        run_id: str,
        repo_path: str,
        branch: str | None,
        message: str,
        files: Sequence[str],
        resume_handler: str = "none",
        expires_in_seconds: float | None = None,
        requester: str = "agenttape",
    ) -> ApprovalRequest:
        """Committing Git changes always needs approval (never pushes; V1)."""
        action = commit_action(repo_path, branch, message, files)
        return self.request(
            session,
            type=ApprovalType.GIT_COMMIT,
            title=f"Commit {len(files)} file(s): {message.splitlines()[0][:120] if message else ''}",
            rationale="Committing Git changes requires approval. AgentTape never pushes.",
            action=action,
            run_id=run_id,
            resume_handler=resume_handler,
            requester=requester,
            expires_in_seconds=expires_in_seconds,
        )

    def request_elevated_command(
        self,
        session: Session,
        *,
        argv: Sequence[str],
        reason: str,
        run_id: str | None = None,
        cwd: str | None = None,
        resume_handler: str = "none",
        expires_in_seconds: float | None = None,
    ) -> ApprovalRequest:
        decision = check_command(list(argv), elevated=True)
        self._refuse_denied(decision)
        return self.request(
            session,
            type=ApprovalType.ELEVATED_COMMAND,
            title=f"Run elevated command: {' '.join(argv)[:160]}",
            rationale=reason,
            action={"action": "command", "argv": list(argv), "cwd": cwd},
            run_id=run_id,
            resume_handler=resume_handler,
            expires_in_seconds=expires_in_seconds,
        )

    def request_protected_path_write(
        self,
        session: Session,
        *,
        paths: Iterable[str],
        patterns: Iterable[str],
        run_id: str | None = None,
        resume_handler: str = "none",
        expires_in_seconds: float | None = None,
    ) -> ApprovalRequest | None:
        """Request approval when ``paths`` touch protected patterns; ``None`` when not."""
        path_list = sorted(paths)
        decision = check_protected_write(path_list, patterns)
        if decision.verdict == Verdict.ALLOW:
            return None
        protected = decision.details.get("paths", [])
        return self.request(
            session,
            type=ApprovalType.PROTECTED_PATH,
            title=f"Write {len(protected)} protected path(s)",
            rationale=decision.reason,
            action={"action": "write_protected", "paths": protected},
            run_id=run_id,
            resume_handler=resume_handler,
            expires_in_seconds=expires_in_seconds,
        )

    def request_budget_increase(
        self,
        session: Session,
        *,
        run_id: str,
        current: dict[str, Any] | None,
        proposed: dict[str, Any],
        reason: str,
        resume_handler: str = "none",
        expires_in_seconds: float | None = None,
    ) -> ApprovalRequest | None:
        """Request approval when ``proposed`` raises an established limit; ``None`` when not."""
        decision = check_budget_change(current, proposed)
        if decision.verdict == Verdict.ALLOW:
            return None
        return self.request(
            session,
            type=ApprovalType.BUDGET_INCREASE,
            title="Increase the run budget",
            rationale=reason or decision.reason,
            action={"action": "budget_increase", "current": current, "proposed": proposed},
            run_id=run_id,
            resume_handler=resume_handler,
            expires_in_seconds=expires_in_seconds,
        )

    def request_evaluation_override(
        self,
        session: Session,
        *,
        run_id: str,
        failed: Sequence[str],
        reason: str,
        resume_handler: str = "none",
        expires_in_seconds: float | None = None,
    ) -> ApprovalRequest:
        return self.request(
            session,
            type=ApprovalType.EVALUATION_OVERRIDE,
            title="Continue after failed required evaluation: " + ", ".join(failed),
            rationale=reason,
            action={"action": "evaluation_override", "failed": sorted(failed)},
            run_id=run_id,
            resume_handler=resume_handler,
            expires_in_seconds=expires_in_seconds,
        )

    # ------------------------------------------------------------------ checks

    @staticmethod
    def check_command(argv: Sequence[str], **kwargs: Any) -> PolicyDecision:
        return check_command(list(argv), **kwargs)

    @staticmethod
    def _refuse_denied(decision: PolicyDecision) -> None:
        if decision.verdict == Verdict.DENY:
            raise PolicyViolationError(decision.reason, details={"policy": decision.as_dict()})
