"""FindingReviewService: build an on-demand "Challenge findings" request and read a run's
challenge history.

:meth:`review_request` is the ONE request builder both trigger surfaces use - the on-demand
API/CLI and the ``finding_review`` workflow node; only
``parent_relation``/``source``/``parent_run_id`` differ between them, and the child run
itself is always created through :meth:`RunService.create_run` (the one run engine).
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from sqlalchemy import select

from agenttape.application.schemas.finding_review import (
    ChallengeCreate,
    ChallengeOut,
    ChallengeStartResult,
    ReviewOutcomeOut,
    UnresolvedReviewOut,
)
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.common import get_or_404, require_finished
from agenttape.application.services.findings import FindingsService, trusted_challenge_section
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import RunKind, RunSource, RunStatus
from agenttape.domain.errors import ConflictError, IdempotencyConflictError
from agenttape.findings.review import (
    MODEL_JUDGMENT,
    REVIEW_RELATION,
    REVIEW_SNAPSHOT_SECTION,
    ReviewPrompt,
    build_reviewer_prompt,
)
from agenttape.infrastructure.db.models import Run

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from agenttape.application.context import AppContext

#: ``REVIEW_RELATION`` is the on-demand trigger's ``parent_relation``. Distinct from the
#: workflow node's own required ``"workflow_node"`` value - the workflow engine's
#: wake-up hook only recognizes that literal string; a non-workflow parent is unaffected by
#: this one. It, ``RunSource.REVIEW``, and the ``finding_review`` snapshot section are
#: reserved: only this service can set them, through ``RunService.create_run``'s
#: internal-only ``finding_review`` argument (docs/SECURITY.md T27).

_DID_NOT_COMPLETE = frozenset(
    {
        RunStatus.FAILED.value,
        RunStatus.TIMED_OUT.value,
        RunStatus.CANCELLED.value,
        RunStatus.SKIPPED.value,
    }
)


class FindingReviewService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.runs = RunService(ctx)

    # ================================================================== build

    def review_request(
        self,
        session: Session,
        target: Run,
        *,
        agent_profile_id: str,
        agent_profile_version_id: str | None = None,
        title: str | None = None,
    ) -> tuple[RunCreate, ReviewPrompt]:
        require_finished(target, "Challenge findings needs a finished run")
        findings = FindingsService.findings_for_run(session, target.id)
        if not findings:
            raise ConflictError("This run recorded no findings to challenge")
        nonce = secrets.token_hex(8)
        prompt = build_reviewer_prompt(target_run_id=target.id, findings=findings, nonce=nonce)
        req = RunCreate(
            kind=RunKind.AGENT.value,
            title=(title or f"Challenge: {target.title}")[:300],
            project_id=target.project_id,
            agent_profile_id=agent_profile_id,
            agent_profile_version_id=agent_profile_version_id,
            prompt=prompt.text,
            is_demo=target.is_demo or None,
            tags=["finding_review"],
        )
        # The snapshot section (prompt.snapshot_section()) is never put on the RunCreate:
        # callers pass it to RunService.create_run's internal-only finding_review argument.
        return req, prompt

    # ================================================================== on-demand trigger

    def start_challenge(
        self, target_run_id: str, body: ChallengeCreate, *, actor: str = "local user"
    ) -> ChallengeStartResult:
        with self.ctx.session() as session:
            target = get_or_404(session, Run, target_run_id, "run")
            req, prompt = self.review_request(
                session,
                target,
                agent_profile_id=body.agent_profile_id,
                agent_profile_version_id=body.agent_profile_version_id,
                title=body.title,
            )
        req = req.model_copy(
            update={
                "source": RunSource.REVIEW,
                "source_ref": target_run_id,
                "parent_run_id": target_run_id,
                "parent_relation": REVIEW_RELATION,
            }
        )
        result = self.runs.create_run(req, actor=actor, finding_review=prompt.snapshot_section())
        return ChallengeStartResult(
            run=result.run,
            findings_reviewed=list(prompt.finding_keys),
            omitted=prompt.omitted,
            neutralized=[{"finding_key": key, "field": field} for key, field in prompt.neutralized],
        )

    # ================================================================== workflow node

    def start_for_workflow_node(
        self,
        *,
        workflow_run_id: str,
        workflow_id: str | None,
        workflow_version_id: str | None,
        workflow_name: str,
        node_key: str,
        node_label: str,
        attempt: int,
        target_run_id: str,
        agent_profile_id: str,
        agent_profile_version_id: str | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
        is_demo: bool = False,
        actor: str = "local user",
    ) -> tuple[str, bool]:
        """The ``finding_review`` workflow node's entry to :meth:`review_request`.

        The child's ``parent_relation`` is exactly ``"workflow_node"`` - the workflow
        engine's wake-up hook (``worker/executors/workflow.py``) only resumes the workflow
        for that literal value. A restarted node start reuses the existing child for the
        same ``(workflow_run_id, node_key, attempt)`` instead of rebuilding a request - the
        reviewer prompt carries a fresh random nonce each time, so a rebuilt request would
        conflict with the stored fingerprint under the same idempotency key.
        """
        from agenttape.workflows.nodes.agent_run import child_idempotency_key

        key = child_idempotency_key(workflow_run_id, node_key, attempt)
        existing = self._workflow_child_by_key(key)
        if existing is not None:
            return existing, False
        with self.ctx.session() as session:
            target = get_or_404(session, Run, target_run_id, "run")
            req, prompt = self.review_request(
                session,
                target,
                agent_profile_id=agent_profile_id,
                agent_profile_version_id=agent_profile_version_id,
            )
        req = req.model_copy(
            update={
                "source": RunSource.WORKFLOW,
                "source_ref": f"{workflow_run_id}:{node_key}",
                "idempotency_key": key,
                "parent_run_id": workflow_run_id,
                "parent_relation": "workflow_node",
                "parent_node_key": node_key,
                "workflow_id": workflow_id,
                "workflow_version_id": workflow_version_id,
                "title": f"{workflow_name}: {node_label}"[:300],
                "timeout_seconds": timeout_seconds,
                "max_attempts": max_attempts if max_attempts and max_attempts > 1 else None,
                "is_demo": is_demo or target.is_demo or None,
                "tags": ["workflow", "finding_review"],
            }
        )
        try:
            result = self.runs.create_run(
                req, actor=actor, finding_review=prompt.snapshot_section()
            )
        except IdempotencyConflictError:
            existing = self._workflow_child_by_key(key)
            if existing is not None:
                return existing, False
            raise
        return result.run.id, result.created

    def _workflow_child_by_key(self, key: str) -> str | None:
        with self.ctx.session() as session:
            run = session.scalars(
                select(Run).where(
                    Run.source == RunSource.WORKFLOW.value, Run.idempotency_key == key
                )
            ).first()
            return run.id if run is not None else None

    # ================================================================== read

    def challenges(self, target_run_id: str) -> list[ChallengeOut]:
        with self.ctx.session() as session:
            target = get_or_404(session, Run, target_run_id, "run")
            triggers: dict[str, str] = {}
            # Both routes list a run only with its provenance (trusted_challenge_section)
            # naming this target, so they apply one trust rule.
            candidates: list[Run] = [
                run
                for run in session.scalars(
                    select(Run).where(
                        Run.parent_run_id == target_run_id, Run.parent_relation == REVIEW_RELATION
                    )
                )
                if (trusted_challenge_section(session, run) or {}).get("target_run_id")
                == target_run_id
            ]
            for run in candidates:
                triggers[run.id] = "on_demand"
            if target.parent_relation == "workflow_node" and target.parent_run_id:
                siblings = session.scalars(
                    select(Run).where(
                        Run.parent_run_id == target.parent_run_id,
                        Run.parent_relation == "workflow_node",
                    )
                ).all()
                for sibling in siblings:
                    section = trusted_challenge_section(session, sibling) or {}
                    if section.get("target_run_id") == target_run_id:
                        candidates.append(sibling)
                        triggers[sibling.id] = "workflow_node"
            candidates.sort(key=lambda r: r.created_at)
            return [
                self._challenge_out(session, target_run_id, child, triggers[child.id])
                for child in candidates
            ]

    def _challenge_out(
        self, session: Session, target_run_id: str, child: Run, trigger: str
    ) -> ChallengeOut:
        section = (child.snapshot or {}).get(REVIEW_SNAPSHOT_SECTION) or {}
        if child.status == RunStatus.SUCCEEDED.value:
            state = "completed"
        elif child.status in _DID_NOT_COMPLETE:
            state = "did_not_complete"
        else:
            state = "pending"
        outcomes: list[ReviewOutcomeOut] = []
        unresolved: list[UnresolvedReviewOut] = []
        for finding in FindingsService.findings_for_run(session, child.id):
            review = (finding.get("details") or {}).get("review")
            if review is None:
                continue
            if review.get("resolved") and review.get("target_run_id") == target_run_id:
                outcomes.append(
                    ReviewOutcomeOut(
                        target_finding_key=review["target_finding_key"],
                        target_finding_id=review["target_finding_id"],
                        outcome=review["outcome"],
                        review_finding_id=finding["id"],
                        review_finding_key=finding["key"],
                        text=finding["text"],
                        citations=len(finding.get("citations") or []),
                    )
                )
            else:
                unresolved.append(
                    UnresolvedReviewOut(
                        review_finding_id=finding["id"],
                        review_finding_key=finding["key"],
                        reason=review.get("reason"),
                    )
                )
        return ChallengeOut(
            run=self.runs.get_run(child.id),
            trigger=trigger,  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            findings_reviewed=list(section.get("finding_keys") or []),
            omitted=int(section.get("omitted") or 0),
            outcomes=outcomes,
            unresolved=unresolved,
            judgment=MODEL_JUDGMENT,
            counts_as_proof=False,
        )
