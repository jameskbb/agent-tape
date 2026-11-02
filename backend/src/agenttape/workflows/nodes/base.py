"""Node executor protocol and registry (one module per node type in this package)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar

from agenttape.adapters.base import CancellationToken, EventEmitter
from agenttape.domain.enums import FailureCategory, RiskLevel
from agenttape.domain.workflows import NodeDef, NodeType, WorkflowDefinition
from agenttape.plugins import import_submodules

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

NODES_PACKAGE = "agenttape.workflows.nodes"

OutcomeStatus = Literal["succeeded", "failed", "timed_out", "waiting", "running", "cancelled"]


@dataclass
class RunData:
    id: str
    title: str
    workspace_id: str
    project_id: str | None
    is_demo: bool
    inputs: dict[str, Any]
    snapshot: dict[str, Any]
    workflow_id: str | None
    workflow_version_id: str | None
    status: str
    started_at_iso: str | None = None

    @property
    def workflow_name(self) -> str:
        return str((self.snapshot.get("workflow") or {}).get("name") or self.title)

    @property
    def project_settings(self) -> dict[str, Any]:
        return dict((self.snapshot.get("project") or {}).get("settings") or {})

    @property
    def project_path(self) -> str | None:
        path = (self.snapshot.get("project") or {}).get("repo_path")
        return str(path) if path else None


@dataclass
class StepData:
    id: str
    key: str
    status: str
    attempt_count: int
    state: dict[str, Any]
    child_run_id: str | None
    approval_request_id: str | None
    inputs: dict[str, Any] | None
    outputs: dict[str, Any] | None
    failure_category: str | None
    failure_message: str | None


@dataclass
class ApprovalDraft:
    title: str
    rationale: str | None
    type: str
    risk_level: RiskLevel | str = RiskLevel.MEDIUM
    requested_action: dict[str, Any] = field(default_factory=dict)
    expires_in_seconds: float | None = None


@dataclass
class NodeOutcome:
    """What a node executor reports back to the engine."""

    status: OutcomeStatus
    outputs: dict[str, Any] | None = None
    failure_category: FailureCategory | None = None
    message: str | None = None
    child_run_id: str | None = None
    approval: ApprovalDraft | None = None
    #: Merged into ``step_runs.state``.
    state: dict[str, Any] = field(default_factory=dict)
    #: Whether a failed outcome may be retried (subject to the node's max_attempts).
    retryable: bool = True

    @classmethod
    def ok(cls, outputs: dict[str, Any] | None = None, **kw: Any) -> NodeOutcome:
        return cls(status="succeeded", outputs=outputs or {}, **kw)

    @classmethod
    def fail(
        cls, category: FailureCategory, message: str, *, retryable: bool = True, **kw: Any
    ) -> NodeOutcome:
        return cls(
            status="failed", failure_category=category, message=message, retryable=retryable, **kw
        )


@dataclass
class NodeContext:
    """Everything a node executor needs for one start of one step."""

    ctx: AppContext
    run: RunData
    definition: WorkflowDefinition
    node: NodeDef
    step: StepData
    #: The template context (``inputs``, ``nodes``, ``workflow``, ``node``).
    template_context: dict[str, Any]
    events: EventEmitter
    cancel: CancellationToken
    log_dir: Path
    worker_id: str

    @property
    def config(self) -> dict[str, Any]:
        return self.node.config


class NodeExecutor(ABC):
    type: ClassVar[NodeType]
    #: Inline nodes finish inside the engine's tick; deferred ones (agent runs, approvals)
    #: are observed on later ticks.
    inline: ClassVar[bool] = True

    @abstractmethod
    async def start(self, nc: NodeContext) -> NodeOutcome:
        """Begin (and for inline nodes, finish) the node."""

    def on_approval(self, step: StepData, approved: bool, decision: dict[str, Any]) -> NodeOutcome:
        """A pending approval requested by this node was decided.

        Default: approved -> run the node again with the approval recorded in its state;
        rejected -> fail.
        """
        if approved:
            return NodeOutcome(status="running", state={"approval_granted": decision})
        return NodeOutcome.fail(
            FailureCategory.APPROVAL_EXPIRED
            if decision.get("status") == "expired"
            else FailureCategory.APPROVAL_REJECTED,
            f"Approval {decision.get('status')}"
            + (f" by {decision.get('reviewer')}" if decision.get("reviewer") else "")
            + (f": {decision.get('note')}" if decision.get("note") else ""),
            retryable=False,
        )


_REGISTERED: dict[NodeType, type[NodeExecutor]] = {}
N = TypeVar("N", bound=type[NodeExecutor])


def register_node(cls: N) -> N:
    _REGISTERED[cls.type] = cls
    return cls


def load_node_executors() -> dict[NodeType, NodeExecutor]:
    import_submodules(NODES_PACKAGE)
    return {t: cls() for t, cls in _REGISTERED.items()}
