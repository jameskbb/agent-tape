"""Runtime (Herdr) API shapes. Socket paths and native session values never appear here."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RuntimeCapabilityOut(BaseModel):
    name: str
    description: str
    support: str
    note: str


class AttachOut(BaseModel):
    command: str | None = None
    guidance: str


class RuntimeSessionOut(BaseModel):
    attempt_id: str
    attempt_number: int | None
    runtime: str
    machine: str
    session_name: str | None
    herdr_version: str | None
    protocol: int | None
    workspace_id: str | None
    tab_id: str | None
    pane_id: str | None
    agent_name: str | None
    agent_kind: str | None
    #: "reported" (the kind only: id or path) or "not reported"; the value is never stored.
    native_session_ref: str
    lifecycle_state: str | None
    owned_workspace: bool
    owned_agent: bool
    last_sync_at: datetime | None
    status: str
    failure_category: str | None
    fallback_to: str | None = None
    attach: AttachOut
    capabilities: list[RuntimeCapabilityOut] = Field(default_factory=list)


class RunRuntimeView(BaseModel):
    run_id: str
    adapter_type: str | None
    runtime: str
    sessions: list[RuntimeSessionOut]


class IntegrationOut(BaseModel):
    target: str
    label: str
    available: bool
    state: str


class HerdrStatusOut(BaseModel):
    installed: bool
    cli_version: str | None
    reachable: bool
    version: str | None
    protocol: int | None
    protocol_supported: bool | None
    message: str
    session: str
    machine: str
    server_capabilities: dict[str, Any] = Field(default_factory=dict)
    #: ``"<runtime>:<kind>"`` -> capability list, for Herdr and the local fallbacks.
    runtime_capabilities: dict[str, list[RuntimeCapabilityOut]] = Field(default_factory=dict)
    #: Saved Herdr machines (read-only); None when the CLI is missing or failed.
    machines: list[dict[str, Any]] | None = None
    #: Herdr agent integrations; None when Herdr is not reachable.
    integrations: list[IntegrationOut] | None = None
    defaults: dict[str, str] = Field(default_factory=dict)


class DiagnosticOut(BaseModel):
    name: str
    ok: bool
    detail: str
    hint: str | None = None


class HerdrTestOut(BaseModel):
    ok: bool
    checks: list[DiagnosticOut]
