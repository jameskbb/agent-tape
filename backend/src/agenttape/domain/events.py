"""The vendor-neutral event envelope (see docs/EVENT-SCHEMA.md).

Adapters and services create :class:`EventDraft` objects; the worker's event sink assigns
``id``, ``sequence``, ``ts`` and ``monotonic_ms``, redacts, caps sizes, and persists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agenttape.domain.enums import EventType, Severity

EVENT_SCHEMA_VERSION = 1


@dataclass
class EventDraft:
    type: EventType | str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    severity: Severity | str = Severity.INFO
    source: str = "agenttape"
    attempt_id: str | None = None
    step_run_id: str | None = None
    node_key: str | None = None
    parent_event_id: str | None = None
    usage: dict[str, Any] | None = None
    cost: dict[str, Any] | None = None
    #: Optional caller-chosen id (so a later event can name this one as its parent).
    id: str | None = None
    #: Milliseconds since the attempt started (set by the worker's sink); None = derive.
    monotonic_ms: float | None = None

    def __post_init__(self) -> None:
        self.type = EventType(self.type)
        self.severity = Severity(self.severity)
