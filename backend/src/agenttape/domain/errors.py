"""Domain and application error types.

The API maps these onto the error envelope ``{"error": {"code", "message", "details"}}``;
the CLI prints ``message`` and exits non-zero. ``code`` values are stable API contract.
"""

from __future__ import annotations

from typing import Any


class AgentTapeError(Exception):
    """Base class. ``code`` is a stable machine-readable identifier."""

    code = "error"
    http_status = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AgentTapeError):
    code = "not_found"
    http_status = 404

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            f"{resource} '{identifier}' was not found",
            details={"resource": resource, "id": identifier},
        )


class ValidationError(AgentTapeError):
    code = "validation_error"
    http_status = 422


class ConflictError(AgentTapeError):
    code = "conflict"
    http_status = 409


class IdempotencyConflictError(ConflictError):
    """The idempotency key was already used for a *different* request."""

    code = "idempotency_key_conflict"


class RunNotFinishedError(ConflictError):
    """The action needs a finished run (a terminal status) and this one has not ended."""

    code = "run_not_finished"


class PolicyViolationError(AgentTapeError):
    code = "policy_violation"
    http_status = 403


class InvalidTransitionError(ConflictError):
    code = "invalid_transition"

    def __init__(self, kind: str, current: str, target: str) -> None:
        super().__init__(
            f"Cannot move {kind} from '{current}' to '{target}'",
            details={"kind": kind, "from": str(current), "to": str(target)},
        )
        self.kind = kind
        self.current = current
        self.target = target
