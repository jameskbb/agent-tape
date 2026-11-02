"""Enumerations shared by every layer. Pure: no I/O, no framework imports."""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


# Attempts and steps reuse the run status vocabulary; each has its own transition table.
AttemptStatus = RunStatus
StepStatus = RunStatus

TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.CANCELLED,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.TIMED_OUT,
        RunStatus.SKIPPED,
    }
)
ACTIVE_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.PREPARING, RunStatus.RUNNING, RunStatus.CANCELLING}
)
# The same sets as plain strings, for comparing a stored ``status`` column.
TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in TERMINAL_STATUSES)
ACTIVE_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in ACTIVE_STATUSES)


class FailureCategory(StrEnum):
    """Structured reason a run, attempt, or step did not succeed."""

    ADAPTER_ERROR = "adapter_error"
    TIMEOUT = "timeout"
    IDLE_TIMEOUT = "idle_timeout"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    EVALUATION_FAILED = "evaluation_failed"
    VALIDATION_FAILED = "validation_failed"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    INTERRUPTED = "interrupted"
    POLICY_VIOLATION = "policy_violation"
    CONFIG_ERROR = "config_error"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    DEPENDENCY_FAILED = "dependency_failed"
    INTERNAL_ERROR = "internal_error"


#: Categories retried automatically (subject to ``max_attempts``) unless a profile overrides.
DEFAULT_RETRYABLE: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.ADAPTER_ERROR,
        FailureCategory.INTERRUPTED,
        FailureCategory.IDLE_TIMEOUT,
        FailureCategory.INTERNAL_ERROR,
    }
)


class RunSource(StrEnum):
    """Where a run came from. All sources share one execution path."""

    UI = "ui"
    API = "api"
    CLI = "cli"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    WORKFLOW = "workflow"
    REPLAY = "replay"
    FORK = "fork"
    DEMO = "demo"
    #: A Claude Code ``http`` hook created or touched this run.
    HOOK = "hook"
    #: The AgentTape MCP server created or touched this run.
    MCP = "mcp"
    #: Challenge findings, on demand.
    REVIEW = "review"


class RunKind(StrEnum):
    """What executes a run: a single adapter, a workflow DAG, or an observed session
    AgentTape did not (necessarily) start. Existing code using the string literals
    ("agent"/"workflow") is left as is; this enum is additive."""

    AGENT = "agent"
    WORKFLOW = "workflow"
    OBSERVED = "observed"


class EventType(StrEnum):
    LIFECYCLE = "lifecycle"
    AGENT_MESSAGE = "agent_message"
    REASONING_SUMMARY = "reasoning_summary"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    COMMAND = "command"
    STDOUT = "stdout"
    STDERR = "stderr"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    GIT_CHANGE = "git_change"
    ARTIFACT = "artifact"
    USAGE = "usage"
    EVALUATION = "evaluation"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    RETRY = "retry"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    ERROR = "error"
    WARNING = "warning"
    SCHEDULER = "scheduler"
    TRIGGER = "trigger"
    STEP = "step"
    #: Runtime (e.g. Herdr) actions AgentTape took: start, prompt, wait, read, interrupt, reconnect.
    RUNTIME = "runtime"
    #: A file evidence finding (protected path, outside boundary, collapsed noise, gaps).
    FILE_EVIDENCE = "file_evidence"
    #: Context provenance: a source made available, selected, retrieved, injected, cited, ...
    CONTEXT = "context"
    #: A structured finding, recommendation, or decision the agent reported.
    FINDING = "finding"


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvaluationStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - evaluation status, not a password
    FAIL = "fail"
    WARNING = "warning"
    ERROR = "error"
    SKIPPED = "skipped"


class QueueItemStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    DONE = "done"
    CANCELLED = "cancelled"


class AdapterAvailability(StrEnum):
    INSTALLED = "installed"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"
    READY = "ready"


class VariableType(StrEnum):
    STRING = "string"
    MULTILINE = "multiline"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    PATH = "path"
    SECRET = "secret"  # noqa: S105 - variable type name
