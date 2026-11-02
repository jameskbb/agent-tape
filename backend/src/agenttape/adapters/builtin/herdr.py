"""The Herdr runtime adapter (``herdr``): agents live in Herdr, AgentTape records and governs.

Profile config:

* ``agent_kind``       - ``claude`` | ``codex`` (a real agent started with ``agent.start``)
                         or ``demo`` (the evidence demo agent run as a command in a pane,
                         in the same directory and with the same arguments as the Demo
                         Agent's local ``full_evidence`` scenario; ``variant`` and
                         ``step_delay_ms`` mean what they mean there);
* ``workspace_policy`` - ``new`` (default: an AgentTape-owned workspace per attempt) or
                         ``reuse`` (``reuse_workspace_id``: a new tab in that workspace;
                         ``reuse_agent``: prompt an existing idle claude/codex agent);
* ``cleanup``          - ``keep`` (default) or ``close_owned`` (close only the workspace or
                         tab AgentTape created, when the attempt finishes);
* ``fallback``         - ``none`` (default) or ``local``: when Herdr is unavailable *before*
                         the attempt starts, run the equivalent local adapter instead
                         (``local_config`` overrides its config). The fallback is recorded as a
                         runtime event. Nothing ever falls back after a Herdr start.

Every start, prompt, wait, read, interrupt, and reconnect is an ``EventType.RUNTIME`` event
(payloads carry Herdr ids and states only, never socket paths or prompt text). The
attempt's Herdr identity is kept in ``runtime_sessions`` (``herdr/sessions.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    DiscoveryResult,
    ExecutionContext,
    FindingsCapture,
    ProfileIssue,
)
from agenttape.adapters.builtin.demo import (
    VARIANTS,
    DemoAdapter,
    _Stopped,
    demo_run,
    evidence_approval,
)
from agenttape.adapters.registry import AdapterRegistry, register_adapter
from agenttape.config import Settings, load_settings
from agenttape.domain.enums import (
    AdapterAvailability,
    EventType,
    FailureCategory,
    Severity,
)
from agenttape.herdr import client as hc
from agenttape.herdr.capabilities import capabilities_for
from agenttape.herdr.client import HerdrClient, HerdrError
from agenttape.herdr.sessions import RuntimeStore

KINDS = ("claude", "codex", "demo")
WORKSPACE_POLICIES = ("new", "reuse")
CLEANUP_POLICIES = ("keep", "close_owned")
FALLBACK_POLICIES = ("none", "local")
LOCAL_ADAPTER = {"claude": "claude-code", "codex": "codex-cli", "demo": "demo"}
DEFAULT_LOCAL_CONFIG: dict[str, dict[str, Any]] = {
    "claude": {},
    "codex": {},
    "demo": {"scenario": "full_evidence"},
}
REPORT_SOURCE = "agenttape"
DEMO_AGENT_LABEL = "agenttape-demo"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
WORKSPACE_RE = re.compile(r"^w[0-9]+$")

STARTUP_TIMEOUT_MS = 60_000
TICK_SECONDS = 0.25
POLL_SECONDS = 2.0  # agent.get cadence while the event stream is down (bounded fallback)
SUBSCRIBED_POLL_SECONDS = 10.0  # safety net while subscribed
UNREACHABLE_GRACE_SECONDS = 30.0
PROMPT_STALL_SECONDS = 60.0
OUTPUT_LINES = 400
_ID_NAMESPACE = uuid.UUID("5b0f6c1e-8a55-4c55-9a3c-6a7e1d0c2a11")


def _failure_category(runtime_category: str) -> FailureCategory:
    if runtime_category == hc.UNAVAILABLE:
        return FailureCategory.ADAPTER_UNAVAILABLE
    if runtime_category == hc.PROTOCOL_MISMATCH:
        return FailureCategory.CONFIG_ERROR
    return FailureCategory.ADAPTER_ERROR


@register_adapter
class HerdrAdapter(AgentAdapter):
    name = "herdr"
    display_name = "Herdr runtime"
    description = (
        "Runs Claude Code, Codex, or the evidence demo agent inside Herdr (the terminal "
        "runtime for coding agents). AgentTape starts, prompts, observes, and reattaches "
        "through Herdr's socket API; you can attach to the live agent at any time."
    )
    capabilities = frozenset({"structured_events", "approvals", "reconnect", "interactive_attach"})
    #: Depends on the profile's ``agent_kind``: see ``findings_capture_for``.
    findings_capture = "per_profile"

    def findings_capture_for(self, config: Mapping[str, Any]) -> FindingsCapture:
        """Hosted Claude Code sessions record findings through the registered AgentTape MCP
        server; the evidence demo agent writes scripted FINDING records to its
        telemetry file, which ``tail_demo`` normalizes exactly like a local demo run; Codex
        has no way to record a finding. ``agent_kind`` defaults to ``demo`` as ``execute``
        does."""
        kind = config.get("agent_kind", "demo")
        if kind == "claude":
            return "mcp"
        if kind == "demo":
            return "scripted"
        return "unsupported"

    # ------------------------------------------------------------------ discovery / validation

    def discover(self) -> DiscoveryResult:
        settings = load_settings()
        exe = hc.cli_path(settings)
        details: dict[str, Any] = {
            "session": hc.session_label(settings),
            "machine": settings.herdr_machine,
            "cli_installed": exe is not None,
            "runtime_capabilities": {k: capabilities_for("herdr", k) for k in KINDS},
        }
        try:
            info = hc.ping_sync(hc.socket_path(settings))
        except HerdrError as exc:
            return DiscoveryResult(
                adapter=self.name,
                status=AdapterAvailability.UNAVAILABLE,
                executable="herdr" if exe else None,
                message=f"Herdr server not reachable ({exc.category}): {exc.message}",
                details=details,
            )
        details.update(protocol=info.protocol, server_capabilities=info.capabilities)
        if not info.protocol_supported:
            return DiscoveryResult(
                adapter=self.name,
                status=AdapterAvailability.MISCONFIGURED,
                version=info.version,
                executable="herdr" if exe else None,
                message=f"Herdr protocol {info.protocol} is older than the supported {hc.MIN_PROTOCOL}; update Herdr",
                details=details,
            )
        return DiscoveryResult(
            adapter=self.name,
            status=AdapterAvailability.READY,
            version=info.version,
            executable="herdr" if exe else None,
            message=f"Herdr {info.version} (protocol {info.protocol}) reachable",
            details=details,
        )

    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
        issues: list[ProfileIssue] = []
        kind = config.get("agent_kind")
        if kind not in KINDS:
            issues.append(ProfileIssue("agent_kind", f"must be one of: {', '.join(KINDS)}"))
        for key, allowed in (
            ("workspace_policy", WORKSPACE_POLICIES),
            ("cleanup", CLEANUP_POLICIES),
            ("fallback", FALLBACK_POLICIES),
        ):
            value = config.get(key)
            if value is not None and value not in allowed:
                issues.append(ProfileIssue(key, f"must be one of: {', '.join(allowed)}"))
        if config.get("workspace_policy") == "reuse":
            ws = config.get("reuse_workspace_id")
            agent = config.get("reuse_agent")
            if bool(ws) == bool(agent):
                issues.append(
                    ProfileIssue(
                        "workspace_policy",
                        "reuse needs exactly one of reuse_workspace_id or reuse_agent",
                    )
                )
            if ws and not (isinstance(ws, str) and WORKSPACE_RE.match(ws)):
                issues.append(
                    ProfileIssue("reuse_workspace_id", "must be a Herdr workspace id such as w3")
                )
            if agent and not (isinstance(agent, str) and NAME_RE.match(agent)):
                issues.append(ProfileIssue("reuse_agent", "must be a live Herdr agent name"))
            if agent and kind == "demo":
                issues.append(
                    ProfileIssue("reuse_agent", "only claude or codex agents can be reused")
                )
        variant = config.get("variant")
        if variant is not None and variant not in VARIANTS:
            issues.append(ProfileIssue("variant", f"must be one of: {', '.join(VARIANTS)}"))
        delay = config.get("step_delay_ms")
        if delay is not None and (not isinstance(delay, int) or not 0 <= delay <= 10_000):
            issues.append(ProfileIssue("step_delay_ms", "must be an integer between 0 and 10000"))
        local_cfg = config.get("local_config")
        if local_cfg is not None and not isinstance(local_cfg, dict):
            issues.append(ProfileIssue("local_config", "must be an object"))
        if config.get("fallback") == "local":
            issues.append(
                ProfileIssue(
                    "fallback",
                    "if Herdr is unavailable before an attempt starts, it runs locally "
                    f"with the {LOCAL_ADAPTER.get(str(kind), 'local')} adapter (recorded on the run)",
                    severity="warning",
                )
            )
        return issues

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["agent_kind"],
            "properties": {
                "agent_kind": {"type": "string", "enum": list(KINDS)},
                "workspace_policy": {
                    "type": "string",
                    "enum": list(WORKSPACE_POLICIES),
                    "default": "new",
                },
                "reuse_workspace_id": {"type": "string"},
                "reuse_agent": {"type": "string"},
                "cleanup": {"type": "string", "enum": list(CLEANUP_POLICIES), "default": "keep"},
                "fallback": {"type": "string", "enum": list(FALLBACK_POLICIES), "default": "none"},
                "local_config": {"type": "object"},
                "variant": {"type": "string", "enum": list(VARIANTS), "default": "a"},
                "step_delay_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
            },
        }

    def default_config(self) -> dict[str, Any]:
        return {
            "agent_kind": "demo",
            "workspace_policy": "new",
            "cleanup": "keep",
            "fallback": "none",
        }

    # ------------------------------------------------------------------ execution

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        settings = load_settings()
        store = ctx.runtime_store if isinstance(ctx.runtime_store, RuntimeStore) else None
        try:
            return await _HerdrRun(
                ctx, HerdrClient.from_settings(settings), store, settings
            ).execute()
        except _Stopped:  # cancelled while preparing the demo fixture clone
            return AdapterResult.failure(
                ctx.cancel.reason or FailureCategory.CANCELLED, ctx.cancel.message or "Stopped"
            )


class _Failed(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


class _HerdrRun:
    def __init__(
        self,
        ctx: ExecutionContext,
        client: HerdrClient,
        store: RuntimeStore | None,
        settings: Settings,
    ) -> None:
        self.ctx = ctx
        self.client = client
        self.store = store
        self.settings = settings
        self.cfg = ctx.profile
        self.kind = str(self.cfg.get("agent_kind", "demo"))
        self.row: dict[str, Any] = {"details": {}}
        self.state: str | None = None
        self.subscribed = False
        self.dedupe = False
        #: Agent-side tool_use id -> deterministic event id of that record's first draft
        #: (mirrors the local demo run's ``_DemoRun.tool_events``), so a later tool_result
        #: can be linked to its call via ``parent_event_id``.
        self.tool_events: dict[str, str] = {}
        self.usage: dict[str, Any] | None = None
        self.demo = demo_run(ctx) if self.kind == "demo" else None
        self.demo_checkpoint: dict[str, Any] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._sub: asyncio.Task[None] | None = None

    # -- recording helpers ---------------------------------------------------------------

    async def event(
        self, action: str, summary: str, *, severity: Severity = Severity.INFO, **payload: Any
    ) -> None:
        await self.ctx.events.event(
            EventType.RUNTIME,
            summary,
            {"runtime": "herdr", "action": action, **payload},
            severity=severity,
        )

    async def persist(self, **fields: Any) -> None:
        if "details" in fields:
            fields["details"] = {**self.row.get("details", {}), **fields["details"]}
        self.row.update(fields)
        if self.store is not None:
            await asyncio.to_thread(self.store.save, **fields)

    def attach(self) -> dict[str, Any]:
        target = self.row.get("agent_name") or self.row.get("pane_id")
        return hc.attach_commands(self.settings.herdr_session, self.settings.herdr_machine, target)

    async def fail(self, category: str, message: str) -> AdapterResult:
        await self.persist(status="failed", failure_category=category, last_sync_at=self._now())
        await self.event("failure", message, severity=Severity.ERROR, category=category)
        return AdapterResult.failure(_failure_category(category), f"Herdr {category}: {message}")

    def _now(self) -> Any:
        return self.store.now() if self.store is not None else None

    # -- entry -------------------------------------------------------------------------------

    async def execute(self) -> AdapterResult:
        resume = self.ctx.resume_state or {}
        try:
            if "herdr_reattach" in resume or resume.get("runtime") == "herdr":
                return await self.resume(resume)
            try:
                info = await self.client.ping()
                if not info.protocol_supported:
                    raise HerdrError(
                        hc.PROTOCOL_MISMATCH,
                        f"Herdr protocol {info.protocol} is older than {hc.MIN_PROTOCOL}",
                    )
            except HerdrError as exc:
                return await self.prestart_unavailable(exc)
            if not info.protocol_tested:
                await self.event(
                    "protocol",
                    f"Herdr protocol {info.protocol} is newer than the tested {hc.TESTED_PROTOCOL}; continuing",
                    severity=Severity.WARNING,
                    protocol=info.protocol,
                )
            await self.persist(
                runtime="herdr",
                status="active",
                machine=self.settings.herdr_machine,
                session_name=hc.session_label(self.settings),
                herdr_version=info.version,
                protocol=info.protocol,
                agent_kind=self.kind,
                lifecycle_state="starting",
                last_sync_at=self._now(),
            )
            if self.ctx.workdir is None:
                return await self.fail(hc.API_ERROR, "the run has no working directory")
            await self.open_pane()
            if self.kind == "demo":
                return await self.run_demo("start", 0)
            return await self.run_agent()
        except _Failed as exc:
            return await self.fail(exc.category, exc.message)
        except HerdrError as exc:
            return await self.fail(exc.category, exc.message)
        finally:
            if self._sub is not None:
                self._sub.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._sub

    def ensure_subscription(self) -> asyncio.Queue[str]:
        """Start (once) the status subscription for the pane; statuses arrive on the queue."""
        if self._sub is None:
            self._sub = asyncio.create_task(self.subscribe_loop(self._queue))
        return self._queue

    async def prestart_unavailable(self, exc: HerdrError) -> AdapterResult:
        """Before anything started in Herdr: fail, or run locally if the profile says so."""
        if self.cfg.get("fallback") != "local":
            await self.persist(
                runtime="herdr",
                status="failed",
                failure_category=exc.category,
                machine=self.settings.herdr_machine,
                session_name=hc.session_label(self.settings),
                agent_kind=self.kind,
            )
            await self.event(
                "unavailable",
                f"Herdr is not available ({exc.category}): {exc.message}",
                severity=Severity.ERROR,
                category=exc.category,
            )
            return AdapterResult.failure(
                _failure_category(exc.category),
                f"Herdr {exc.category}: {exc.message}. Start Herdr, or set the profile's fallback to local.",
            )
        local_name = LOCAL_ADAPTER[self.kind]
        await self.persist(
            runtime="local",
            status="fallback",
            failure_category=exc.category,
            machine="local",
            agent_kind=self.kind,
            details={"fallback_to": local_name, "reason": exc.category},
        )
        await self.event(
            "fallback",
            f"Herdr unavailable ({exc.category}) before the attempt started; running locally with "
            f"the {local_name} adapter as the profile's fallback policy allows",
            severity=Severity.WARNING,
            category=exc.category,
            fallback_to=local_name,
        )
        local = AdapterRegistry.load().get(local_name)
        # From here on this is a local run. Relabel the attempt's shared context in place so
        # its events (emitter source) and the evidence monitors, which read the same
        # context's ``adapter_type``, record the runtime that actually ran.
        self.ctx.adapter_type = local_name
        self.ctx.profile = dict(self.cfg.get("local_config") or DEFAULT_LOCAL_CONFIG[self.kind])
        self.ctx.events.source = local_name
        return await local.execute(self.ctx)

    # -- workspace / pane --------------------------------------------------------------------

    async def open_pane(self) -> None:
        ctx = self.ctx
        label = f"agenttape {ctx.run.id[:8]} #{ctx.attempt.number}"
        cwd = str(ctx.workdir)
        if self.cfg.get("workspace_policy") == "reuse" and self.cfg.get("reuse_agent"):
            name = str(self.cfg["reuse_agent"])
            agent = await self.client.agent_get(name)
            if agent.get("agent_status") not in ("idle", "done"):
                raise _Failed(
                    hc.BLOCKED if agent.get("agent_status") == "blocked" else hc.API_ERROR,
                    f"agent '{name}' is {agent.get('agent_status')}; AgentTape only reuses an idle agent",
                )
            await self.persist(
                workspace_id=agent.get("workspace_id"),
                tab_id=agent.get("tab_id"),
                pane_id=agent.get("pane_id"),
                agent_name=name,
                owned_workspace=False,
                owned_agent=False,
                details={"terminal_id": agent.get("terminal_id"), "reused_agent": True},
            )
            await self.event(
                "attach", f"Reusing the existing Herdr agent '{name}' (not owned)", agent=name
            )
            return
        if self.cfg.get("workspace_policy") == "reuse":
            ws = str(self.cfg["reuse_workspace_id"])
            result = await self.client.call(
                "tab.create", {"workspace_id": ws, "cwd": cwd, "label": label, "focus": False}
            )
            tab, pane = result.get("tab") or {}, result.get("root_pane") or {}
            await self.persist(
                workspace_id=ws,
                tab_id=tab.get("tab_id"),
                pane_id=pane.get("pane_id"),
                owned_workspace=False,
                details={"terminal_id": pane.get("terminal_id"), "owned_tab": True},
            )
            await self.event(
                "start",
                f"Created tab {tab.get('tab_id')} in the existing workspace {ws}",
                workspace_id=ws,
                tab_id=tab.get("tab_id"),
                pane_id=pane.get("pane_id"),
            )
            return
        result = await self.client.call(
            "workspace.create", {"cwd": cwd, "label": label, "focus": False}
        )
        ws_info = result.get("workspace") or {}
        tab, pane = result.get("tab") or {}, result.get("root_pane") or {}
        if not pane.get("pane_id"):
            raise _Failed(hc.PROTOCOL_MISMATCH, "workspace.create returned no pane")
        await self.persist(
            workspace_id=ws_info.get("workspace_id"),
            tab_id=tab.get("tab_id"),
            pane_id=pane.get("pane_id"),
            owned_workspace=True,
            details={"terminal_id": pane.get("terminal_id")},
        )
        await self.event(
            "start",
            f"Created AgentTape-owned Herdr workspace {ws_info.get('workspace_id')} (pane {pane.get('pane_id')})",
            workspace_id=ws_info.get("workspace_id"),
            tab_id=tab.get("tab_id"),
            pane_id=pane.get("pane_id"),
        )

    async def check_identity(self, info: dict[str, Any]) -> None:
        """Raise when the pane no longer hosts the agent this attempt started."""
        expected = DEMO_AGENT_LABEL if self.kind == "demo" else self.row.get("agent_name")
        observed = info.get("agent") if self.kind == "demo" else info.get("name")
        terminal = self.row.get("details", {}).get("terminal_id")
        if (expected and observed != expected) or (
            terminal and info.get("terminal_id") and info.get("terminal_id") != terminal
        ):
            raise _Failed(
                hc.AGENT_REPLACED,
                f"pane {self.row.get('pane_id')} no longer hosts the agent AgentTape started",
            )
        session = info.get("agent_session")
        if (
            isinstance(session, dict)
            and session.get("kind")
            and not self.row.get("native_session_kind")
        ):
            await self.persist(native_session_kind=str(session["kind"]))

    async def probe(self) -> dict[str, Any]:
        """``agent.get`` by pane id, mapping a missing agent to replaced/stale."""
        pane_id = str(self.row.get("pane_id"))
        try:
            info = await self.client.agent_get(pane_id)
        except HerdrError as exc:
            if exc.category != hc.STALE_ID:
                raise
            try:
                await self.client.pane_get(pane_id)
            except HerdrError as pane_exc:
                if pane_exc.category == hc.STALE_ID:
                    raise _Failed(
                        hc.STALE_ID,
                        f"Herdr pane {pane_id} no longer exists (closed, or Herdr restarted)",
                    ) from None
                raise
            raise _Failed(
                hc.AGENT_REPLACED, f"pane {pane_id} no longer hosts the agent AgentTape started"
            ) from None
        await self.check_identity(info)
        return info

    # -- lifecycle observation ---------------------------------------------------------------

    async def on_state(self, state: str) -> None:
        if state not in hc.AGENT_STATES:
            state = "unknown"
        if state == self.state:
            return
        self.state = state
        await self.persist(lifecycle_state=state, last_sync_at=self._now())
        if state == "blocked":
            attach = self.attach()
            await self.event(
                "blocked",
                "The agent is waiting for input in Herdr. AgentTape leaves it untouched; resolve it in Herdr"
                + (f": {attach['command']}" if attach.get("command") else "."),
                severity=Severity.WARNING,
                state=state,
                attach_command=attach.get("command"),
            )
            return
        await self.event("wait", f"Herdr reports the agent {state}", state=state)

    async def subscribe_loop(self, queue: asyncio.Queue[str]) -> None:
        pane_id = str(self.row.get("pane_id"))
        backoff = 0.5
        lost = False
        while True:
            try:
                async for message in self.client.subscribe(
                    [{"type": "pane.agent_status_changed", "pane_id": pane_id}]
                ):
                    if message.get("event") == "subscription_started":
                        self.subscribed = True
                        backoff = 0.5
                        if lost:
                            lost = False
                            await self.event("resubscribe", "Herdr event stream restored")
                        continue
                    data = message.get("data") or {}
                    if data.get("pane_id") == pane_id and data.get("agent_status"):
                        queue.put_nowait(str(data["agent_status"]))
            except HerdrError as exc:
                if exc.code == "invalid_request":
                    self.subscribed = False
                    await self.event(
                        "degraded",
                        "This Herdr does not support event subscriptions; polling instead",
                        severity=Severity.WARNING,
                    )
                    return
                if self.subscribed and not lost:
                    lost = True
                    await self.event(
                        "resubscribe",
                        f"Herdr event stream lost ({exc.category}); polling until it returns",
                        severity=Severity.WARNING,
                        category=exc.category,
                    )
                self.subscribed = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    async def observe(
        self, *, until_done: bool, saw_activity: bool, target: str
    ) -> dict[str, Any] | None:
        """Watch the agent. Returns ``{"cancelled": True}`` on cancel, None when finished.

        ``until_done`` false: return once the agent is idle/done (startup readiness).
        """
        queue = self.ensure_subscription()
        loop = asyncio.get_running_loop()
        next_poll = 0.0
        last_ok = loop.time()
        stall_at = loop.time() + PROMPT_STALL_SECONDS
        while True:
            if self.ctx.cancel.cancelled:
                return {"cancelled": True}
            state: str | None
            try:
                state = await asyncio.wait_for(queue.get(), timeout=TICK_SECONDS * 2)
                last_ok = loop.time()
            except TimeoutError:
                state = None
            if state is None and loop.time() >= next_poll:
                next_poll = loop.time() + (
                    SUBSCRIBED_POLL_SECONDS if self.subscribed else POLL_SECONDS
                )
                try:
                    info = await self.probe()
                except HerdrError as exc:
                    if exc.category in (hc.UNAVAILABLE, hc.SOCKET_DISCONNECT, hc.TIMEOUT):
                        if loop.time() - last_ok > UNREACHABLE_GRACE_SECONDS:
                            raise
                        continue
                    raise
                last_ok = loop.time()
                state = str(info.get("agent_status") or "unknown")
                await self.persist(last_sync_at=self._now())
                seq, base = info.get("state_change_seq"), self.row["details"].get("prompt_seq")
                if isinstance(seq, int) and isinstance(base, int) and seq >= base + 2:
                    # A whole turn (active, then settled) happened between two polls.
                    saw_activity = True
            if state is not None:
                await self.on_state(state)
            if self.state in ("working", "blocked"):
                # Herdr confirms the agent is alive (or waiting on a person): not idle.
                self.ctx.events.touch()
                if not saw_activity:
                    saw_activity = True
                    await self.persist(details={"saw_activity": True})
            if self.state in ("idle", "done") and (saw_activity or not until_done):
                return None
            if until_done and not saw_activity and loop.time() > stall_at:
                raise _Failed(
                    hc.TIMEOUT,
                    f"agent '{target}' showed no activity within {PROMPT_STALL_SECONDS:g}s of the prompt",
                )

    # -- claude / codex --------------------------------------------------------------------

    async def run_agent(self) -> AdapterResult:
        ctx = self.ctx
        if not self.row.get("agent_name"):
            name = f"at-{ctx.run.id[:8]}-a{ctx.attempt.number}"
            try:
                await self.client.call(
                    "agent.start",
                    {
                        "name": name,
                        "kind": self.kind,
                        "pane_id": self.row["pane_id"],
                        "timeout_ms": STARTUP_TIMEOUT_MS,
                    },
                    timeout=STARTUP_TIMEOUT_MS / 1000 + 5,
                )
                ready = True
            except HerdrError as exc:
                if exc.code != "agent_not_ready":
                    raise
                ready = False
            await self.persist(agent_name=name, owned_agent=True)
            await self.event(
                "start",
                f"Started {self.kind} agent '{name}' in Herdr pane {self.row['pane_id']}",
                agent=name,
                kind=self.kind,
                pane_id=self.row["pane_id"],
            )
            if not ready:
                outcome = await self.observe(until_done=False, saw_activity=False, target=name)
                if outcome is not None:
                    return await self.interrupt(name)
        name = str(self.row["agent_name"])
        text = ctx.resolved_prompt() or ctx.prompt or ""
        if not text.strip():
            raise _Failed(hc.API_ERROR, "the run has no prompt to send")
        self.ensure_subscription()  # catch the working transition of a fast turn
        before = await self.client.agent_get(name)
        await self.check_identity(before)  # also records the native session ref kind
        # "prompting" is saved before the call: a restart in between must not send the
        # prompt twice (resume() treats a moved state_change_seq as delivered).
        await self.persist(
            details={"prompt_seq": before.get("state_change_seq"), "prompting": True}
        )
        await self.client.call("agent.prompt", {"target": name, "text": text})
        await self.persist(details={"prompted": True})
        await self.event(
            "prompt",
            f"Sent the resolved prompt to '{name}' through agent.prompt",
            agent=name,
            characters=len(text),
        )
        return await self.finish_agent(name, saw_activity=False)

    async def finish_agent(self, name: str, *, saw_activity: bool) -> AdapterResult:
        outcome = await self.observe(until_done=True, saw_activity=saw_activity, target=name)
        if outcome is not None:
            return await self.interrupt(name)
        await self.probe()  # final sync: identity, native session ref
        await self.read_output(name)
        await self.persist(status="completed", last_sync_at=self._now())
        await self.cleanup()
        return AdapterResult.success(
            outputs={"herdr_agent": name, "lifecycle_state": self.state},
        )

    async def read_output(self, target: str) -> None:
        """Save a claude/codex agent's recent visible output. (The demo kind's output is its
        telemetry file; its pane only shows the command line AgentTape typed.)"""
        try:
            result = await self.client.call(
                "agent.read",
                {"target": target, "source": "recent_unwrapped", "lines": OUTPUT_LINES},
            )
        except HerdrError as exc:
            await self.event(
                "read",
                f"Could not read recent output from Herdr ({exc.category})",
                severity=Severity.WARNING,
                category=exc.category,
            )
            return
        read = result.get("read") or {}
        text = str(read.get("text") or "")
        await self.ctx.artifacts.write_text(
            "herdr-recent-output.txt",
            text,
            kind="log",
            metadata={
                "source": "herdr",
                "lines_requested": OUTPUT_LINES,
                "truncated": bool(read.get("truncated")),
            },
        )
        await self.event(
            "read",
            f"Saved the agent's recent visible output from Herdr ({len(text.splitlines())} lines)",
            lines=len(text.splitlines()),
            truncated=bool(read.get("truncated")),
        )

    async def interrupt(self, target: str) -> AdapterResult:
        ctx = self.ctx
        reason = ctx.cancel.reason or FailureCategory.CANCELLED
        if self.state == "blocked":
            await self.event(
                "interrupt",
                "Not interrupting: the agent is blocked on a question in Herdr and is left untouched",
                severity=Severity.WARNING,
                sent=False,
            )
        else:
            try:
                if self.kind == "demo":
                    await self.client.call(
                        "pane.send_keys", {"pane_id": target, "keys": ["ctrl+c"]}
                    )
                    await self.report("idle", "Stopped by AgentTape")
                else:
                    await self.client.call(
                        "agent.send_keys", {"target": target, "keys": ["ctrl+c"]}
                    )
                await self.event(
                    "interrupt", f"Sent ctrl+c to {target} ({reason.value})", sent=True
                )
            except HerdrError as exc:
                await self.event(
                    "interrupt",
                    f"Could not interrupt {target} ({exc.category})",
                    severity=Severity.WARNING,
                    sent=False,
                    category=exc.category,
                )
        await self.persist(status="completed", lifecycle_state=self.state, last_sync_at=self._now())
        return AdapterResult.failure(reason, ctx.cancel.message or "Stopped")

    async def cleanup(self) -> None:
        if self.cfg.get("cleanup") != "close_owned":
            return
        details = self.row.get("details", {})
        try:
            if self.row.get("owned_workspace") and self.row.get("workspace_id"):
                await self.client.call(
                    "workspace.close", {"workspace_id": self.row["workspace_id"]}
                )
                closed = f"workspace {self.row['workspace_id']}"
            elif details.get("owned_tab") and self.row.get("tab_id"):
                await self.client.call("tab.close", {"tab_id": self.row["tab_id"]})
                closed = f"tab {self.row['tab_id']}"
            else:
                return  # nothing AgentTape owns
        except HerdrError as exc:
            await self.event(
                "cleanup",
                f"Could not close the AgentTape-owned Herdr {exc.category}",
                severity=Severity.WARNING,
                category=exc.category,
            )
            return
        await self.persist(details={"closed": closed})
        await self.event("cleanup", f"Closed the AgentTape-owned Herdr {closed}", closed=closed)

    # -- demo --------------------------------------------------------------------------------

    def telemetry_path(self) -> Path:
        return self.ctx.log_dir / f"attempt-{self.ctx.attempt.number}.herdr-telemetry.jsonl"

    async def report(self, state: str, message: str | None = None) -> None:
        params: dict[str, Any] = {
            "pane_id": self.row["pane_id"],
            "source": REPORT_SOURCE,
            "agent": DEMO_AGENT_LABEL,
            "state": state,
        }
        if message:
            params["message"] = message
        await self.client.call("pane.report_agent", params)

    async def run_demo(self, phase: str, offset: int) -> AdapterResult:
        """Run the evidence demo agent in the pane: the same workdir choice (isolated
        fixture worktree, else a private clone) and argv as the local ``full_evidence``."""
        assert self.demo is not None
        telemetry = self.telemetry_path()
        telemetry.parent.mkdir(parents=True, exist_ok=True)
        if phase == "start":
            telemetry.write_bytes(b"")
        resume = self.ctx.resume_state or {}
        workdir = await self.demo.evidence_workdir(resume)
        variant = self.demo.variant(resume)
        self.demo_checkpoint = {"workdir": str(workdir), "variant": variant}
        argv = self.demo.evidence_argv(workdir, str(telemetry), phase, variant)
        await self.report("working", f"AgentTape run {self.ctx.run.id[:8]}")
        # AgentTape-built argv (never user text), shell-quoted for the pane's shell.
        await self.client.call(
            "pane.send_text", {"pane_id": self.row["pane_id"], "text": shlex.join(argv) + "\r"}
        )
        await self.persist(
            agent_name=None,
            owned_agent=True,
            lifecycle_state="working",
            status="active",
            details={"telemetry_file": telemetry.name, "phase": phase},
        )
        self.state = "working"
        await self.event(
            "start",
            f"Started the demo agent ({phase}) in Herdr pane {self.row['pane_id']}",
            pane_id=self.row["pane_id"],
            phase=phase,
        )
        return await self.tail_demo(offset)

    async def tail_demo(self, offset: int) -> AdapterResult:
        ctx = self.ctx
        telemetry = self.telemetry_path()
        loop = asyncio.get_running_loop()
        next_poll = loop.time() + POLL_SECONDS
        last_ok = loop.time()
        approval: dict[str, Any] | None = None
        exit_code: int | None = None
        normalizer = DemoAdapter()
        while exit_code is None:
            if ctx.cancel.cancelled:
                return await self.interrupt(str(self.row["pane_id"]))
            try:
                with telemetry.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read(1024 * 1024)
            except FileNotFoundError:
                chunk = b""
            consumed = 0
            for raw in chunk.splitlines(keepends=True):
                if not raw.endswith(b"\n"):
                    break  # partial line; wait for the rest
                line_offset = offset + consumed
                consumed += len(raw)
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                if kind == "exit":
                    exit_code = int(record.get("code", 1))
                    break
                if kind == "approval_request":
                    approval = record
                    # Kept on the row: a restart may land between this line and the exit.
                    await self.persist(details={"approval": record})
                if kind == "usage":
                    self.usage = {
                        "input_tokens": int(record.get("input_tokens", 0)),
                        "output_tokens": int(record.get("output_tokens", 0)),
                        "simulated": True,
                    }
                await self.emit_record(normalizer, record, line_offset)
            if consumed:
                offset += consumed
                await self.persist(telemetry_offset=offset, last_sync_at=self._now())
            if exit_code is not None:
                break
            if loop.time() >= next_poll:
                next_poll = loop.time() + POLL_SECONDS
                try:
                    await self.probe()
                    last_ok = loop.time()
                except HerdrError as exc:
                    if exc.category not in (hc.UNAVAILABLE, hc.SOCKET_DISCONNECT, hc.TIMEOUT):
                        raise
                    if loop.time() - last_ok > UNREACHABLE_GRACE_SECONDS:
                        raise
            await ctx.cancel.sleep(TICK_SECONDS)

        pane_id = str(self.row["pane_id"])
        approval = approval or self.row.get("details", {}).get("approval")
        if exit_code == 3 and approval is not None:
            await self.report("blocked", "Waiting for approval in AgentTape")
            await self.persist(
                status="waiting", lifecycle_state="blocked", last_sync_at=self._now()
            )
            await self.event(
                "wait",
                "The demo agent asked for approval; waiting in AgentTape (Herdr shows it blocked)",
                state="blocked",
            )
            spec = evidence_approval(approval)
            return AdapterResult(
                outcome="needs_approval",
                exit_code=3,
                usage=self.usage,
                approval=spec,
                # After a reattach the workdir is not known here; the resume then picks it
                # again (the isolated fixture worktree is stable).
                checkpoint={
                    "runtime": "herdr",
                    "phase": "approved-resume",
                    "pane_id": pane_id,
                    "paths": spec.requested_action["paths"],
                    **self.demo_checkpoint,
                },
            )
        await self.report("idle", "Finished" if exit_code == 0 else f"Exited {exit_code}")
        if exit_code == 0:
            await self.persist(status="completed", lifecycle_state="idle", last_sync_at=self._now())
            await self.cleanup()
            return AdapterResult.success(exit_code=0, usage=self.usage)
        await self.persist(status="completed", lifecycle_state="idle", last_sync_at=self._now())
        await self.cleanup()
        return AdapterResult.failure(
            FailureCategory.ADAPTER_ERROR,
            f"the demo agent exited with code {exit_code}",
            exit_code=exit_code,
            usage=self.usage,
        )

    async def emit_record(
        self, normalizer: AgentAdapter, record: dict[str, Any], line_offset: int
    ) -> None:
        drafts = normalizer.normalize(record)
        ids = [
            str(uuid.uuid5(_ID_NAMESPACE, f"{self.ctx.attempt.id}:{line_offset}:{i}"))
            for i in range(len(drafts))
        ]
        skip: set[str] = set()
        if self.dedupe and self.store is not None:
            skip = await asyncio.to_thread(self.store.existing_event_ids, ids)
            if not skip:
                self.dedupe = False  # past the already-recorded part of the file
        for draft, event_id in zip(drafts, ids, strict=True):
            # Record deterministic ids for tool_use/tool_result linking (mirrors the local
            # demo run's `_DemoRun.record()`) even for a draft skipped by dedupe below: the
            # ids are deterministic, and a later tool_result still needs its parent.
            if record.get("type") == "tool_use":
                self.tool_events[str(record.get("id"))] = event_id
            if record.get("type") == "tool_result":
                draft.parent_event_id = self.tool_events.get(str(record.get("tool_use_id")))
            if event_id in skip:
                continue
            draft.id = event_id
            await self.ctx.events.emit(draft)

    # -- resume / reattach -------------------------------------------------------------------

    async def resume(self, resume: dict[str, Any]) -> AdapterResult:
        stored = await asyncio.to_thread(self.store.load) if self.store is not None else None
        if not stored or not stored.get("pane_id"):
            return await self.fail(hc.STALE_ID, "no recorded Herdr session to resume")
        self.row = {**stored, "details": dict(stored.get("details") or {})}
        reattach = resume.get("herdr_reattach")
        try:
            await self.client.ping()
        except HerdrError as exc:
            # Never fall back to local execution once the attempt started in Herdr.
            return await self.fail(exc.category, f"cannot reach Herdr to resume: {exc.message}")
        info = await self.probe()
        self.state = str(info.get("agent_status") or "unknown")
        now = self._now()
        if reattach:
            await self.event(
                "reconnect",
                "Reattached to the Herdr session after AgentTape restarted",
                severity=Severity.WARNING,
                offline_from=reattach.get("offline_from"),
                offline_to=now.isoformat() if now is not None else None,
                pane_id=self.row.get("pane_id"),
            )
        await self.persist(status="active", lifecycle_state=self.state, last_sync_at=now)
        if self.kind == "demo":
            offset = int(self.row.get("telemetry_offset") or 0)
            if reattach:
                self.dedupe = True
                return await self.tail_demo(offset)
            return await self.run_demo("resume", offset)
        name = str(self.row.get("agent_name"))
        details = self.row["details"]
        seq, base = info.get("state_change_seq"), details.get("prompt_seq")
        # Interrupted between "prompting" and "prompted": delivered unless Herdr shows the
        # agent's state has not moved since (then the prompt never reached it).
        delivered = details.get("prompting") and not (
            isinstance(seq, int) and isinstance(base, int) and seq <= base
        )
        if not details.get("prompted") and not delivered:
            return await self.run_agent()
        return await self.finish_agent(name, saw_activity=bool(details.get("saw_activity")))
