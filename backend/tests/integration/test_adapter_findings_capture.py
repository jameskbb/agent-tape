"""Per-adapter findings capability: GET /api/v1/adapters carries
how each adapter's runs record findings, and a subclass that does not declare it defaults
to "unsupported" rather than silently omitting the field. An adapter hosting several agents
(Herdr) reports "per_profile" and answers per run instead, so a Codex or demo
run inside Herdr is never described by the Claude Code pane's capability."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    DiscoveryResult,
    ExecutionContext,
    ProfileIssue,
)
from agenttape.adapters.builtin.herdr import KINDS, HerdrAdapter
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import AdapterAvailability

HERDR_CAPTURE = {"claude": "mcp", "codex": "unsupported", "demo": "scripted"}


def test_get_adapters_reports_findings_capability_per_adapter(client: TestClient) -> None:
    adapters = {a["name"]: a for a in client.get("/api/v1/adapters").json()}
    assert adapters["claude-code"]["findings"] == "mcp"
    assert adapters["herdr"]["findings"] == "per_profile"
    assert adapters["demo"]["findings"] == "scripted"
    assert adapters["codex-cli"]["findings"] == "unsupported"
    assert adapters["generic-command"]["findings"] == "unsupported"
    # Every adapter still carries its existing capabilities list unchanged.
    assert isinstance(adapters["demo"]["capabilities"], list) and adapters["demo"]["capabilities"]


def test_adapter_subclass_without_findings_capture_defaults_to_unsupported() -> None:
    class _MinimalAdapter(AgentAdapter):
        name = "minimal-test-adapter"
        display_name = "Minimal Test Adapter"

        def discover(self) -> DiscoveryResult:
            return DiscoveryResult(adapter=self.name, status=AdapterAvailability.READY)

        def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
            return []

        async def execute(self, ctx: ExecutionContext) -> AdapterResult:
            return AdapterResult.success()

    assert _MinimalAdapter().findings_capture == "unsupported"


def test_herdr_capture_map_covers_every_agent_kind() -> None:
    assert set(HERDR_CAPTURE) == set(KINDS)


@pytest.mark.parametrize("kind", KINDS)
def test_herdr_findings_capture_is_resolved_per_agent_kind(kind: str) -> None:
    assert HerdrAdapter().findings_capture_for({"agent_kind": kind}) == HERDR_CAPTURE[kind]


def test_herdr_findings_capture_defaults_to_the_demo_agent_like_execute() -> None:
    assert HerdrAdapter().findings_capture_for({}) == "scripted"


@pytest.mark.parametrize("kind", KINDS)
def test_a_herdr_run_reports_its_own_agents_findings_capture(
    ctx: AppContext, client: TestClient, kind: str
) -> None:
    profile = AgentProfileService(ctx).create(
        AgentProfileCreate(name=f"herdr-{kind}", adapter_type="herdr", config={"agent_kind": kind})
    )
    run_id = (
        RunService(ctx)
        .create_run(RunCreate(agent_profile_id=profile.id, prompt="Review the change"))
        .run.id
    )
    run = client.get(f"/api/v1/runs/{run_id}").json()
    run = run.get("run", run)
    assert run["adapter_type"] == "herdr"
    assert run["findings_capture"] == HERDR_CAPTURE[kind]


def test_a_single_adapter_run_reports_the_adapter_value(
    ctx: AppContext, client: TestClient
) -> None:
    profile = AgentProfileService(ctx).create(
        AgentProfileCreate(name="demo-local", adapter_type="demo", config={"scenario": "success"})
    )
    run_id = (
        RunService(ctx)
        .create_run(RunCreate(agent_profile_id=profile.id, prompt="Review the change"))
        .run.id
    )
    listed = client.get("/api/v1/runs").json()["items"]
    [row] = [r for r in listed if r["id"] == run_id]
    assert row["findings_capture"] == "scripted"


def test_a_per_profile_adapter_without_an_override_never_claims_capture() -> None:
    class _VariesAdapter(AgentAdapter):
        name = "varies-test-adapter"
        display_name = "Varies Test Adapter"
        findings_capture = "per_profile"

        def discover(self) -> DiscoveryResult:
            return DiscoveryResult(adapter=self.name, status=AdapterAvailability.READY)

        def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
            return []

        async def execute(self, ctx: ExecutionContext) -> AdapterResult:
            return AdapterResult.success()

    assert _VariesAdapter().findings_capture_for({}) == "unsupported"
