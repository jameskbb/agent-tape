"""The trigger-source interface: how an inbound HTTP delivery is authenticated and read.

V1 ships one implementation, the generic authenticated webhook
(:mod:`agenttape.triggers.webhook`, kind ``webhook``). Future integrations - a Jira
automation webhook or a GitHub App - add a module to this package that subclasses
:class:`TriggerSource` and decorates it with :func:`register_trigger_source`; the delivery
pipeline in ``TriggerService.handle_delivery`` (size cap, content type, rate limit,
idempotency, input mapping, run creation, delivery log) stays the same. See
docs/TRIGGERS.md, "Adding a trigger source".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar

from agenttape.plugins import import_submodules

TRIGGERS_PACKAGE = "agenttape.triggers"


@dataclass(frozen=True)
class InboundRequest:
    """Transport-neutral view of one delivery (the API router builds it)."""

    headers: dict[str, str]  # lower-cased names
    query: dict[str, str]
    body: bytes
    client: str = "unknown"
    content_type: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class TriggerSource(ABC):
    kind: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""

    @abstractmethod
    def presented_token(self, request: InboundRequest) -> str | None:
        """The credential the sender presented (compared against the stored hash)."""

    def token_in_query(self, request: InboundRequest) -> bool:
        """True when the presented credential came from the URL (refused unless the
        trigger opted in with ``allow_query_token``)."""
        return False

    def delivery_id(self, request: InboundRequest) -> tuple[str, str] | None:
        """``(source, value)`` of a sender-assigned unique delivery id, if any."""
        return None

    def metadata(self, request: InboundRequest) -> dict[str, Any]:
        """Non-secret facts recorded with the delivery (event name, user agent...)."""
        return {}


_SOURCES: dict[str, type[TriggerSource]] = {}
S = TypeVar("S", bound=type[TriggerSource])


def register_trigger_source(cls: S) -> S:
    _SOURCES[cls.kind] = cls
    return cls


def trigger_sources() -> dict[str, TriggerSource]:
    import_submodules(TRIGGERS_PACKAGE)
    return {kind: cls() for kind, cls in sorted(_SOURCES.items())}


def get_trigger_source(kind: str) -> TriggerSource | None:
    return trigger_sources().get(kind)
