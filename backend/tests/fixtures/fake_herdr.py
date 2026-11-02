"""A fake Herdr server for tests and end-to-end runs (no real agents, no paid APIs).

Speaks the subset of Herdr's socket API (protocol 22) that AgentTape uses, over a Unix
socket with newline-delimited JSON:

* workspaces/tabs/panes with Herdr-shaped ids (``w1``, ``w1:t1``, ``w1:p1``);
* ``pane.send_text``: text ending in ``\\r`` is run as a command with ``shlex.split`` and a
  subprocess in the pane's cwd (no shell); output goes to the pane buffer;
* simulated ``claude``/``codex`` agents: ``agent.prompt`` -> working -> done; a prompt
  containing ``BLOCK`` ends blocked, ``UNKNOWN`` ends unknown, ``HANG`` stays working;
* ``pane.report_agent`` / ``agent.get`` / ``events.subscribe`` like the real server.

Runs in a background thread (``FakeHerdr().start(path)``) so tests can drive AgentTape in
other event loops, or standalone for Playwright::

    python tests/fixtures/fake_herdr.py --socket /tmp/fake-herdr.sock

The request and response shapes follow ``tests/fixtures/herdr_protocol.json`` (captured from
a real Herdr 0.9.0); ``tests/unit/test_herdr_client.py`` checks both against it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import os
import shlex
import signal
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "0.9.0-fake"
PROTOCOL = 22


@dataclass
class Pane:
    pane_id: str
    tab_id: str
    workspace_id: str
    cwd: str
    terminal_id: str
    lines: list[str] = field(default_factory=list)
    input: str = ""
    agent: str | None = None
    name: str | None = None
    status: str = "unknown"
    session_ref: dict[str, Any] | None = None
    seq: int = 0
    proc: asyncio.subprocess.Process | None = None


class FakeHerdr:
    def __init__(self, *, work_seconds: float = 0.4) -> None:
        self.work_seconds = work_seconds
        self.path: Path | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.server: asyncio.base_events.Server | None = None
        self.panes: dict[str, Pane] = {}
        self.workspaces: dict[str, dict[str, Any]] = {}
        self.tabs: dict[str, dict[str, Any]] = {}
        self.subscribers: list[tuple[asyncio.StreamWriter, set[str] | None]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._ws_counter = itertools.count(1)
        self._term_counter = itertools.count(1)
        self._ready = threading.Event()

    # ------------------------------------------------------------------ lifecycle

    def start(self, path: Path) -> FakeHerdr:
        self.path = Path(path)
        self.thread = threading.Thread(target=self._thread_main, name="fake-herdr", daemon=True)
        self.thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("fake Herdr did not start")
        return self

    def _thread_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self._serve())
        self._ready.set()
        self.loop.run_forever()
        self.loop.run_until_complete(self._shutdown())
        self.loop.close()

    async def _serve(self) -> None:
        assert self.path is not None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))

    async def _shutdown(self) -> None:
        for pane in self.panes.values():
            if pane.proc is not None and pane.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    pane.proc.kill()
                with contextlib.suppress(Exception):
                    await pane.proc.wait()
        self._drop_subscribers()
        if self.server is not None:
            self.server.close()
        if self.path is not None:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()

    def stop(self) -> None:
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(10)

    def run(self, fn: Any, *args: Any) -> Any:
        """Run ``fn(*args)`` on the fake's loop (thread-safe test control)."""
        assert self.loop is not None

        async def _call() -> Any:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        return asyncio.run_coroutine_threadsafe(_call(), self.loop).result(5)

    # ------------------------------------------------------------------ test controls

    def drop_subscriptions(self) -> None:
        self.run(self._drop_subscribers)

    def _drop_subscribers(self) -> None:
        for writer, _ in self.subscribers:
            writer.close()
        self.subscribers.clear()

    def set_status(self, pane_id: str, status: str) -> None:
        self.run(self._set_status, self.panes[pane_id], status)

    def close_pane(self, pane_id: str) -> None:
        self.run(self._close_pane, pane_id)

    def replace_agent(self, pane_id: str) -> None:
        def _replace() -> None:
            pane = self.panes[pane_id]
            pane.agent, pane.name = "codex", None
            pane.terminal_id = f"term_fake{next(self._term_counter):04d}"

        self.run(_replace)

    def pane_for_label(self, label_prefix: str) -> Pane | None:
        for ws in self.workspaces.values():
            if str(ws["label"]).startswith(label_prefix):
                for pane in self.panes.values():
                    if pane.workspace_id == ws["workspace_id"]:
                        return pane
        return None

    # ------------------------------------------------------------------ protocol

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    request = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send(
                        writer,
                        {"id": "", "error": {"code": "invalid_request", "message": "bad json"}},
                    )
                    continue
                rid = str(request.get("id", ""))
                method = str(request.get("method", ""))
                params = request.get("params") or {}
                self.calls.append((method, params))
                if method == "events.subscribe":
                    panes = {
                        s["pane_id"] for s in params.get("subscriptions", []) if s.get("pane_id")
                    }
                    self.subscribers.append((writer, panes or None))
                    await self._send(
                        writer, {"id": rid, "result": {"type": "subscription_started"}}
                    )
                    continue
                try:
                    result = await self._dispatch(method, params)
                    await self._send(writer, {"id": rid, "result": result})
                except _ApiError as exc:
                    await self._send(
                        writer, {"id": rid, "error": {"code": exc.code, "message": exc.message}}
                    )
        except (ConnectionError, OSError):
            pass
        finally:
            self.subscribers = [s for s in self.subscribers if s[0] is not writer]
            writer.close()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        writer.write((json.dumps(message) + "\n").encode())
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()

    def _pane(self, pane_id: str) -> Pane:
        pane = self.panes.get(str(pane_id))
        if pane is None:
            raise _ApiError("pane_not_found", f"pane {pane_id} not found")
        return pane

    def _agent(self, target: str) -> Pane:
        for pane in self.panes.values():
            if pane.name and pane.name == target:
                return pane
        pane = self.panes.get(target)
        if pane is not None and pane.agent:
            return pane
        raise _ApiError("agent_not_found", f"agent target {target} not found")

    def _pane_info(self, pane: Pane) -> dict[str, Any]:
        info = {
            "pane_id": pane.pane_id,
            "terminal_id": pane.terminal_id,
            "workspace_id": pane.workspace_id,
            "tab_id": pane.tab_id,
            "focused": False,
            "cwd": pane.cwd,
            "agent": pane.agent,
            "agent_status": pane.status,
            "revision": pane.seq,
        }
        return {k: v for k, v in info.items() if v is not None}  # Herdr omits null fields

    def _agent_info(self, pane: Pane) -> dict[str, Any]:
        info = {**self._pane_info(pane), "state_change_seq": pane.seq}
        if pane.name:
            info["name"] = pane.name
        if pane.session_ref:
            info["agent_session"] = pane.session_ref
        return info

    def _new_pane(self, ws_id: str, tab_id: str, cwd: str, n: int) -> Pane:
        pane = Pane(
            pane_id=f"{ws_id}:p{n}",
            tab_id=tab_id,
            workspace_id=ws_id,
            cwd=cwd,
            terminal_id=f"term_fake{next(self._term_counter):04d}",
            lines=["$"],
        )
        self.panes[pane.pane_id] = pane
        return pane

    async def _dispatch(self, method: str, p: dict[str, Any]) -> dict[str, Any]:
        if method == "ping":
            return {
                "type": "pong",
                "version": VERSION,
                "protocol": PROTOCOL,
                "capabilities": {"live_handoff": False, "health_check": True},
            }
        if method == "integration.list":
            return {
                "type": "integration_list",
                "integrations": [
                    {
                        "target": "claude",
                        "label": "claude",
                        "command": "claude",
                        "available": True,
                        "state": "current",
                    },
                    {
                        "target": "codex",
                        "label": "codex",
                        "command": "codex",
                        "available": False,
                        "state": "not_installed",
                    },
                ],
            }
        if method == "workspace.create":
            n = next(self._ws_counter)
            ws_id = f"w{n}"
            tab_id = f"{ws_id}:t1"
            cwd = str(p.get("cwd") or os.getcwd())
            ws = {
                "workspace_id": ws_id,
                "number": n,
                "label": p.get("label") or str(n),
                "focused": False,
                "pane_count": 1,
                "tab_count": 1,
                "active_tab_id": tab_id,
                "agent_status": "unknown",
            }
            self.workspaces[ws_id] = ws
            self.tabs[tab_id] = {
                "tab_id": tab_id,
                "workspace_id": ws_id,
                "number": 1,
                "label": "1",
                "pane_count": 1,
            }
            pane = self._new_pane(ws_id, tab_id, cwd, 1)
            return {
                "type": "workspace_created",
                "workspace": ws,
                "tab": self.tabs[tab_id],
                "root_pane": self._pane_info(pane),
            }
        if method == "workspace.get":
            ws = self.workspaces.get(str(p.get("workspace_id")))
            if ws is None:
                raise _ApiError(
                    "workspace_not_found", f"workspace {p.get('workspace_id')} not found"
                )
            return {"type": "workspace_info", "workspace": ws}
        if method == "workspace.close":
            ws_id = str(p.get("workspace_id"))
            if ws_id not in self.workspaces:
                raise _ApiError("workspace_not_found", f"workspace {ws_id} not found")
            for pane_id in [k for k, v in self.panes.items() if v.workspace_id == ws_id]:
                await self._close_pane(pane_id)
            self.workspaces.pop(ws_id, None)
            return {"type": "ok"}
        if method == "tab.create":
            ws_id = str(p.get("workspace_id"))
            if ws_id not in self.workspaces:
                raise _ApiError("workspace_not_found", f"workspace {ws_id} not found")
            count = sum(1 for t in self.tabs.values() if t["workspace_id"] == ws_id) + 1
            tab_id = f"{ws_id}:t{count}"
            self.tabs[tab_id] = {
                "tab_id": tab_id,
                "workspace_id": ws_id,
                "number": count,
                "label": p.get("label") or str(count),
            }
            n = sum(1 for v in self.panes.values() if v.workspace_id == ws_id) + 1
            pane = self._new_pane(ws_id, tab_id, str(p.get("cwd") or os.getcwd()), n)
            return {
                "type": "tab_created",
                "tab": self.tabs[tab_id],
                "root_pane": self._pane_info(pane),
            }
        if method == "tab.close":
            tab_id = str(p.get("tab_id"))
            if tab_id not in self.tabs:
                raise _ApiError("tab_not_found", f"tab {tab_id} not found")
            for pane_id in [k for k, v in self.panes.items() if v.tab_id == tab_id]:
                await self._close_pane(pane_id)
            self.tabs.pop(tab_id, None)
            return {"type": "ok"}
        if method == "pane.get":
            return {"type": "pane_info", "pane": self._pane_info(self._pane(p.get("pane_id", "")))}
        if method in ("pane.read", "agent.read"):
            pane = (
                self._pane(p["pane_id"])
                if method == "pane.read"
                else self._agent(str(p.get("target")))
            )
            lines = int(p.get("lines") or 50)
            text = "\n".join(pane.lines[-lines:])
            return {
                "type": "pane_read",
                "read": {
                    "pane_id": pane.pane_id,
                    "workspace_id": pane.workspace_id,
                    "tab_id": pane.tab_id,
                    "source": p.get("source"),
                    "format": "text",
                    "text": text,
                    "revision": pane.seq,
                    "truncated": len(pane.lines) > lines,
                },
            }
        if method == "pane.send_text":
            pane = self._pane(p.get("pane_id", ""))
            pane.input += str(p.get("text", ""))
            while "\r" in pane.input:
                line, pane.input = pane.input.split("\r", 1)
                await self._run_command(pane, line)
            return {"type": "ok"}
        if method in ("pane.send_keys", "agent.send_keys"):
            pane = (
                self._pane(p["pane_id"])
                if method == "pane.send_keys"
                else self._agent(str(p.get("target")))
            )
            if "ctrl+c" in (p.get("keys") or []):
                if pane.proc is not None and pane.proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        pane.proc.send_signal(signal.SIGINT)
                if pane.name:
                    await self._set_status(pane, "idle")
            return {"type": "ok"}
        if method == "pane.report_agent":
            pane = self._pane(p.get("pane_id", ""))
            pane.agent = str(p.get("agent"))
            await self._set_status(pane, str(p.get("state")))
            return {"type": "ok"}
        if method == "pane.release_agent":
            pane = self._pane(p.get("pane_id", ""))
            pane.agent, pane.name = None, None
            await self._set_status(pane, "unknown")
            return {"type": "ok"}
        if method == "agent.list":
            return {
                "type": "agent_list",
                "agents": [self._agent_info(v) for v in self.panes.values() if v.agent],
            }
        if method == "agent.get":
            return {
                "type": "agent_info",
                "agent": self._agent_info(self._agent(str(p.get("target")))),
            }
        if method == "agent.start":
            pane = self._pane(p.get("pane_id", ""))
            if p.get("kind") not in ("claude", "codex"):
                raise _ApiError("invalid_request", f"unknown agent kind {p.get('kind')}")
            if any(v.name == p.get("name") for v in self.panes.values()):
                raise _ApiError("agent_name_taken", f"agent name {p.get('name')} already in use")
            pane.agent, pane.name = str(p["kind"]), str(p["name"])
            pane.session_ref = {
                "source": "fake",
                "agent": pane.agent,
                "kind": "id",
                "value": "fake-session-secret-id",
            }
            pane.lines.append(f"[{pane.agent} started]")
            await self._set_status(pane, "idle")
            return {"type": "agent_started", "agent": self._agent_info(pane)}
        if method == "agent.prompt":
            pane = self._agent(str(p.get("target")))
            if pane.status == "blocked":
                raise _ApiError("agent_blocked", "agent is waiting at an approval or question")
            text = str(p.get("text", ""))
            pane.lines.append(f"> prompt received ({len(text)} characters)")
            await self._set_status(pane, "working")
            assert self.loop is not None
            self.loop.create_task(self._simulate_turn(pane, text))
            return {"type": "agent_prompted", "agent": self._agent_info(pane)}
        raise _ApiError("invalid_request", f"invalid request: unknown variant `{method}`")

    async def _simulate_turn(self, pane: Pane, text: str) -> None:
        await asyncio.sleep(self.work_seconds)
        if pane.pane_id not in self.panes or pane.status != "working":
            return
        if "HANG" in text:
            return
        if "BLOCK" in text:
            pane.lines.append("? Allow the agent to edit files? (y/n)")
            await self._set_status(pane, "blocked")
            return
        pane.lines.append(f"[{pane.agent}] finished the task")
        await self._set_status(pane, "unknown" if "UNKNOWN" in text else "done")

    async def _set_status(self, pane: Pane, status: str) -> None:
        pane.status = status
        pane.seq += 1
        message = {
            "event": "pane.agent_status_changed",
            "data": {
                "pane_id": pane.pane_id,
                "workspace_id": pane.workspace_id,
                "agent": pane.agent,
                "agent_status": status,
            },
        }
        for writer, panes in list(self.subscribers):
            if panes is None or pane.pane_id in panes:
                try:
                    writer.write((json.dumps(message) + "\n").encode())
                except (ConnectionError, OSError, RuntimeError):
                    self.subscribers = [s for s in self.subscribers if s[0] is not writer]

    async def _close_pane(self, pane_id: str) -> None:
        pane = self.panes.pop(pane_id, None)
        if pane is not None and pane.proc is not None and pane.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                pane.proc.kill()

    async def _run_command(self, pane: Pane, line: str) -> None:
        pane.lines.append(f"$ {line}")
        if not line.strip():
            return
        try:
            argv = shlex.split(line)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=pane.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            pane.lines.append(f"fake-herdr: {exc}")
            return
        pane.proc = proc
        assert self.loop is not None
        self.loop.create_task(self._pump(pane, proc))

    async def _pump(self, pane: Pane, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            pane.lines.append(raw.decode(errors="replace").rstrip("\n"))
        await proc.wait()
        pane.lines.append("$")


class _ApiError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    fake = FakeHerdr().start(Path(args.socket))
    print(f"fake herdr listening on {args.socket}", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    fake.stop()
    sys.exit(0)


if __name__ == "__main__":
    main()
