"""Agent adapter protocol and the execution context handed to adapters.

An adapter is a thin translator between AgentTape and an agent runtime (a CLI, a script,
the built-in demo). It must:

* report availability without crashing (:meth:`AgentAdapter.discover`);
* validate a profile config before it is saved (:meth:`AgentAdapter.validate_profile`);
* execute one attempt (:meth:`AgentAdapter.execute`), streaming events through
  ``ctx.events`` and honouring ``ctx.cancel``;
* map raw adapter records to vendor-neutral events (:meth:`AgentAdapter.normalize`).

Adapters never touch the database directly; everything goes through the context.
See docs/EXTENDING.md ("Add an adapter").
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

from agenttape.domain.enums import (
    AdapterAvailability,
    EventType,
    FailureCategory,
    RiskLevel,
    Severity,
)
from agenttape.domain.events import EventDraft
from agenttape.domain.policy import forbidden_env_name

# --------------------------------------------------------------------------- discovery


@dataclass
class DiscoveryResult:
    adapter: str
    status: AdapterAvailability
    version: str | None = None
    executable: str | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProfileIssue:
    field: str
    message: str
    severity: Literal["error", "warning"] = "error"


# --------------------------------------------------------------------------- cancellation


class CancellationToken:
    """Cooperative cancellation shared by the executor, watchdogs, and the adapter."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: FailureCategory | None = None
        self.message: str | None = None

    def cancel(self, reason: FailureCategory, message: str) -> bool:
        """Request cancellation. Only the first reason wins; returns True if it was first."""
        if self._event.is_set():
            return False
        self.reason = reason
        self.message = message
        self._event.set()
        return True

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    async def sleep(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; return True if cancelled meanwhile."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=max(0.0, seconds))
        except TimeoutError:
            return False
        return True


# --------------------------------------------------------------------------- results


@dataclass
class ApprovalSpec:
    """An adapter's request to pause until a human approves (see ApprovalService)."""

    title: str
    rationale: str
    type: str = "run_continue"
    risk_level: RiskLevel = RiskLevel.MEDIUM
    requested_action: dict[str, Any] = field(default_factory=dict)
    expires_in_seconds: float | None = None


@dataclass
class AdapterResult:
    outcome: Literal["succeeded", "failed", "needs_approval"]
    exit_code: int | None = None
    failure_category: FailureCategory | None = None
    message: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    #: Only what the adapter actually reported. ``{"simulated": True}`` marks demo numbers.
    usage: dict[str, Any] | None = None
    cost: dict[str, Any] | None = None
    #: Opaque state stored on the attempt and handed back as ``ctx.resume_state``.
    checkpoint: dict[str, Any] | None = None
    approval: ApprovalSpec | None = None

    @classmethod
    def success(cls, **kwargs: Any) -> AdapterResult:
        return cls(outcome="succeeded", **kwargs)

    @classmethod
    def failure(cls, category: FailureCategory, message: str, **kwargs: Any) -> AdapterResult:
        return cls(outcome="failed", failure_category=category, message=message, **kwargs)


# --------------------------------------------------------------------------- context services


class ArtifactWriter(Protocol):
    async def write_text(
        self,
        name: str,
        text: str,
        *,
        kind: str = "file",
        media_type: str = "text/plain; charset=utf-8",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Redact, store, and record an artifact; returns the artifact id."""
        ...

    async def write_bytes(
        self,
        name: str,
        data: bytes,
        *,
        kind: str = "file",
        media_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> str: ...


@dataclass
class FileChangeDraft:
    path: str
    change_type: str
    old_path: str | None = None
    insertions: int | None = None
    deletions: int | None = None
    is_binary: bool = False
    size_bytes: int | None = None
    is_protected: bool = False
    diff_artifact_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class FileChangeRecorder(Protocol):
    async def record(self, changes: list[FileChangeDraft]) -> None: ...


@dataclass(frozen=True)
class RunInfo:
    id: str
    title: str
    kind: str
    source: str
    project_id: str | None
    workspace_id: str
    is_demo: bool
    snapshot: Mapping[str, Any]


@dataclass(frozen=True)
class AttemptInfo:
    id: str
    number: int
    max_attempts: int


class EventEmitter:
    """Convenience wrapper that stamps attempt/node ids and tracks activity for idle timeouts."""

    def __init__(
        self,
        emit: Callable[[EventDraft], Awaitable[None]],
        *,
        source: str,
        attempt_id: str | None,
        step_run_id: str | None = None,
        node_key: str | None = None,
        on_activity: Callable[[], None] | None = None,
    ) -> None:
        self._emit = emit
        self.source = source
        self.attempt_id = attempt_id
        self.step_run_id = step_run_id
        self.node_key = node_key
        self._on_activity = on_activity

    async def emit(self, draft: EventDraft) -> None:
        draft.attempt_id = draft.attempt_id or self.attempt_id
        draft.step_run_id = draft.step_run_id or self.step_run_id
        draft.node_key = draft.node_key or self.node_key
        if draft.source == "agenttape":
            draft.source = self.source
        if self._on_activity is not None:
            self._on_activity()
        await self._emit(draft)

    async def event(
        self,
        type: EventType | str,
        summary: str,
        payload: dict[str, Any] | None = None,
        *,
        severity: Severity | str = Severity.INFO,
        parent_event_id: str | None = None,
        usage: dict[str, Any] | None = None,
        id: str | None = None,
    ) -> None:
        await self.emit(
            EventDraft(
                type=type,
                summary=summary,
                payload=payload or {},
                severity=severity,
                parent_event_id=parent_event_id,
                usage=usage,
                id=id,
            )
        )

    def touch(self) -> None:
        if self._on_activity is not None:
            self._on_activity()


@dataclass
class ExecutionContext:
    """Everything an adapter needs for one attempt. Built by the worker's executor."""

    run: RunInfo
    attempt: AttemptInfo
    adapter_type: str
    #: The agent profile config from the run's immutable snapshot.
    profile: dict[str, Any]
    #: Rendered prompt as stored (secret references appear as ``[secret:NAME]``).
    prompt: str | None
    inputs: dict[str, Any]
    workdir: Path | None
    events: EventEmitter
    cancel: CancellationToken
    artifacts: ArtifactWriter
    files: FileChangeRecorder
    #: Checkpoint returned by a previous ``needs_approval`` result of this attempt.
    resume_state: dict[str, Any] | None
    data_dir: Path
    log_dir: Path
    #: Resolve a secret reference by name (value is auto-registered for redaction).
    resolve_secret: Callable[[str], str]
    #: Re-render the prompt with secret values. Never persist the result.
    resolved_prompt: Callable[[], str | None]
    #: Redact configured secret values and credential patterns from text.
    redact: Callable[[str], str]
    #: Settings snapshot for adapters that need caps/timeouts.
    limits: dict[str, Any] = field(default_factory=dict)
    #: The attempt's ``runtime_sessions`` row access (``herdr.sessions.RuntimeStore``), set
    #: by the worker; None outside a worker. Adapters never get a database handle otherwise.
    runtime_store: Any = None

    def build_env(
        self, allowlist: list[str] | None = None, extra: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Minimal child environment: a safe base + allowlisted names + explicit extras."""
        return build_child_env(allowlist or [], extra or {})


SAFE_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "USER", "LOGNAME")


def build_child_env(allowlist: list[str], extra: Mapping[str, str]) -> dict[str, str]:
    """Safe base + allowlisted host names + explicit extras.

    Allowlisted names that change what git or the dynamic loader runs without appearing
    in argv (``GIT_*``, ``LD_PRELOAD``...; ``domain.policy.forbidden_env_name``) are never
    passed, whatever a project or profile allowlist says. ``extra`` is AgentTape's own
    (adapter-set) values and is applied as given.
    """
    env: dict[str, str] = {}
    for name in (*SAFE_BASE_ENV, *(n for n in allowlist if not forbidden_env_name(n))):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env.update(extra)
    return env


# --------------------------------------------------------------------------- adapter protocol


#: How one run records findings (``AgentAdapter.findings_capture_for``).
FindingsCapture = Literal["mcp", "scripted", "unsupported"]
#: The adapter-wide value ``GET /adapters`` reports: a ``FindingsCapture``, or
#: ``"per_profile"`` when it depends on the profile's own agent.
AdapterFindingsCapture = Literal["mcp", "scripted", "unsupported", "per_profile"]


class AgentAdapter(ABC):
    """Base class for adapters. Subclass, set ``name``, and decorate with ``@register_adapter``."""

    name: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""
    #: Capability flags shown in the UI (e.g. "structured_events", "usage", "tools").
    capabilities: ClassVar[frozenset[str]] = frozenset()
    #: How this adapter's runs record findings: "mcp" through the AgentTape MCP server's
    #: record_finding tool, "scripted" as FINDING events a deterministic script records,
    #: "unsupported" when its runs cannot record findings, or "per_profile" when the answer
    #: depends on the profile (an adapter hosting several agents; see
    #: ``findings_capture_for``). Shown in the Findings tab's empty state so it never depends
    #: on a frontend lookup table of adapter names.
    findings_capture: ClassVar[AdapterFindingsCapture] = "unsupported"

    def findings_capture_for(self, config: Mapping[str, Any]) -> FindingsCapture:
        """How a run of a profile with this ``config`` records findings. Defaults to the
        adapter-wide ``findings_capture``; an adapter whose agents differ (``per_profile``)
        overrides this, since the adapter-wide value alone would misreport some runs."""
        if self.findings_capture == "per_profile":
            return "unsupported"
        return self.findings_capture

    @abstractmethod
    def discover(self) -> DiscoveryResult: ...

    @abstractmethod
    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]: ...

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> AdapterResult: ...

    def normalize(self, raw: dict[str, Any]) -> list[EventDraft]:
        """Map one raw adapter record to zero or more events (default: opaque passthrough)."""
        return [
            EventDraft(
                type=EventType.AGENT_MESSAGE,
                summary=str(raw.get("summary") or raw.get("type") or "adapter record")[:300],
                payload={"raw": raw},
                source=self.name,
            )
        ]

    def config_schema(self) -> dict[str, Any]:
        """Optional JSON Schema describing adapter-specific profile options (for forms)."""
        return {}

    def default_config(self) -> dict[str, Any]:
        return {}
