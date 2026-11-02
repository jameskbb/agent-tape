"""Runtime views: a run's Herdr references and the Herdr status / connection test.

Read-only towards Herdr except for the connection test's ping, agent list, and a
subscription that is closed right away. Never returns socket paths or session values.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.runtime import (
    AttachOut,
    DiagnosticOut,
    HerdrStatusOut,
    HerdrTestOut,
    IntegrationOut,
    RunRuntimeView,
    RuntimeCapabilityOut,
    RuntimeSessionOut,
)
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404
from agenttape.herdr import client as hc
from agenttape.herdr.capabilities import capabilities_for, table
from agenttape.herdr.client import HerdrClient, HerdrError
from agenttape.infrastructure.db.extra_models.runtime_sessions import RuntimeSession
from agenttape.infrastructure.db.models import Run, RunAttempt

DEFAULTS = {"workspace_policy": "new", "cleanup": "keep", "fallback": "none"}
_UNNAMED_SESSIONS = ("default", "custom socket")


def _caps(runtime: str, kind: str | None) -> list[RuntimeCapabilityOut]:
    return [RuntimeCapabilityOut(**c) for c in capabilities_for(runtime, kind or "")]


class RuntimeService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ run view

    def run_view(self, run_id: str) -> RunRuntimeView:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            rows = session.execute(
                select(RuntimeSession, RunAttempt.attempt_number)
                .join(RunAttempt, RunAttempt.id == RuntimeSession.attempt_id, isouter=True)
                .where(RuntimeSession.run_id == run_id)
                .order_by(RuntimeSession.created_at)
            ).all()
            sessions = [self._session_out(row, number) for row, number in rows]
            runtime = sessions[-1].runtime if sessions else "local"
            return RunRuntimeView(
                run_id=run.id,
                adapter_type=run.adapter_type,
                runtime=runtime,
                sessions=sessions,
            )

    @staticmethod
    def _session_out(row: RuntimeSession, number: int | None) -> RuntimeSessionOut:
        details = dict(row.details or {})
        target = row.agent_name or row.pane_id
        session_flag = row.session_name if row.session_name not in _UNNAMED_SESSIONS else None
        attach = (
            hc.attach_commands(session_flag, row.machine, target)
            if row.runtime == "herdr"
            else {"guidance": "This attempt ran locally (fallback); there is no Herdr session."}
        )
        return RuntimeSessionOut(
            attempt_id=row.attempt_id,
            attempt_number=number,
            runtime=row.runtime,
            machine=row.machine,
            session_name=row.session_name,
            herdr_version=row.herdr_version,
            protocol=row.protocol,
            workspace_id=row.workspace_id,
            tab_id=row.tab_id,
            pane_id=row.pane_id,
            agent_name=row.agent_name,
            agent_kind=row.agent_kind,
            native_session_ref=(
                f"reported ({row.native_session_kind})"
                if row.native_session_kind
                else "not reported"
            ),
            lifecycle_state=row.lifecycle_state,
            owned_workspace=row.owned_workspace,
            owned_agent=row.owned_agent,
            last_sync_at=row.last_sync_at,
            status=row.status,
            failure_category=row.failure_category,
            fallback_to=details.get("fallback_to"),
            attach=AttachOut(**attach),
            capabilities=_caps(
                row.runtime if row.runtime == "herdr" else "local",
                row.agent_kind
                if row.runtime == "herdr"
                else {"claude": "claude-code", "codex": "codex-cli"}.get(
                    row.agent_kind or "", row.agent_kind
                ),
            ),
        )

    # ------------------------------------------------------------------ Herdr status

    async def herdr_status(self) -> HerdrStatusOut:
        settings = self.ctx.settings
        exe = hc.cli_path(settings)
        cli_version, machines = (
            await asyncio.gather(
                asyncio.to_thread(hc.cli_version, exe), asyncio.to_thread(hc.machine_list, exe)
            )
            if exe
            else (None, None)
        )
        client = HerdrClient.from_settings(settings, timeout=3.0)
        out = HerdrStatusOut(
            installed=exe is not None,
            cli_version=cli_version,
            reachable=False,
            version=None,
            protocol=None,
            protocol_supported=None,
            message="",
            session=hc.session_label(settings),
            machine=settings.herdr_machine,
            runtime_capabilities={
                key: [RuntimeCapabilityOut(**c) for c in caps] for key, caps in table().items()
            },
            machines=machines,
            defaults=DEFAULTS,
        )
        try:
            info = await client.ping()
        except HerdrError as exc:
            out.message = f"Herdr server not reachable ({exc.category}): {exc.message}"
            return out
        out.reachable = True
        out.version, out.protocol = info.version, info.protocol
        out.protocol_supported = info.protocol_supported
        out.server_capabilities = info.capabilities
        out.message = f"Herdr {info.version} (protocol {info.protocol}) reachable"
        with contextlib.suppress(HerdrError):
            result = await client.call("integration.list", {})
            out.integrations = [
                IntegrationOut(
                    target=str(i.get("target")),
                    label=str(i.get("label") or i.get("target")),
                    available=bool(i.get("available")),
                    state=str(i.get("state") or "unknown"),
                )
                for i in result.get("integrations") or []
                if isinstance(i, dict)
            ]
        return out

    async def test_connection(self, *, actor: str) -> HerdrTestOut:
        settings = self.ctx.settings
        checks: list[DiagnosticOut] = []
        exe = hc.cli_path(settings)
        checks.append(
            DiagnosticOut(
                name="Herdr CLI installed",
                ok=exe is not None,
                detail="found on PATH" if exe else "the herdr command was not found",
                hint=None
                if exe
                else "Install Herdr (https://herdr.dev) or set AGENTTAPE_HERDR_EXECUTABLE.",
            )
        )
        path = hc.socket_path(settings)
        session = hc.session_label(settings)
        checks.append(
            DiagnosticOut(
                name="Session socket present",
                ok=path.exists(),
                detail=f"session '{session}'" + ("" if path.exists() else ": no socket"),
                hint=None
                if path.exists()
                else (
                    f"Start it: herdr --session {settings.herdr_session} server"
                    if settings.herdr_session
                    else "Start Herdr (run herdr), or set AGENTTAPE_HERDR_SESSION to a running session (herdr session list)."
                ),
            )
        )
        client = HerdrClient.from_settings(settings, timeout=3.0)
        try:
            info = await client.ping()
            checks.append(
                DiagnosticOut(name="Server answers ping", ok=True, detail=f"Herdr {info.version}")
            )
            checks.append(
                DiagnosticOut(
                    name="API protocol supported",
                    ok=info.protocol_supported,
                    detail=f"protocol {info.protocol} (tested with {hc.TESTED_PROTOCOL})",
                    hint=None if info.protocol_supported else "Update Herdr (herdr update).",
                )
            )
            agents = await client.call("agent.list", {})
            checks.append(
                DiagnosticOut(
                    name="Agent API",
                    ok=True,
                    detail=f"{len(agents.get('agents') or [])} live agent(s) visible",
                )
            )
            try:
                stream = client.subscribe([{"type": "workspace.created"}])
                first = await asyncio.wait_for(anext(stream), timeout=3.0)
                await stream.aclose()
                checks.append(
                    DiagnosticOut(
                        name="Event subscription",
                        ok=first.get("event") == "subscription_started",
                        detail="events.subscribe acknowledged",
                    )
                )
            except (HerdrError, TimeoutError, StopAsyncIteration) as exc:
                checks.append(
                    DiagnosticOut(
                        name="Event subscription",
                        ok=False,
                        detail=f"not available ({getattr(exc, 'category', 'timeout')}); AgentTape will poll",
                        hint="Runs still work with bounded polling.",
                    )
                )
        except HerdrError as exc:
            checks.append(
                DiagnosticOut(
                    name="Server answers ping",
                    ok=False,
                    detail=f"{exc.category}: {exc.message}",
                    hint="Check that the Herdr server for this session is running (herdr status).",
                )
            )
        ok = all(c.ok for c in checks if c.name != "Event subscription")
        with self.ctx.session() as db:
            record_audit(
                db,
                self.ctx.clock,
                action="runtime.herdr.test",
                resource_type="runtime",
                resource_id="herdr",
                actor=actor,
                details={"ok": ok, "failed": [c.name for c in checks if not c.ok]},
            )
        return HerdrTestOut(ok=ok, checks=checks)
