"""A small asyncio client for Herdr's socket API (newline-delimited JSON over a Unix socket).

Verified against Herdr 0.9.0, protocol 22 (``herdr api schema --json``). One request per
connection, except :meth:`HerdrClient.subscribe`, which keeps its connection open and
yields event lines. Every failure is a :class:`HerdrError` with a structured ``category``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agenttape.config import Settings

#: Oldest protocol this client was written against. Newer protocols are used with a warning.
MIN_PROTOCOL = 22
TESTED_PROTOCOL = 22

# Structured failure categories (runtime_sessions.failure_category, runtime event payloads).
UNAVAILABLE = "unavailable"  # no socket / nothing listening
SOCKET_DISCONNECT = "socket_disconnect"  # connection dropped mid-request or mid-stream
TIMEOUT = "timeout"
STALE_ID = "stale_id"  # workspace/pane/agent id no longer exists (closed, or Herdr restarted)
AGENT_REPLACED = "agent_replaced"  # the pane now hosts a different agent or process
PROTOCOL_MISMATCH = "protocol_mismatch"
BLOCKED = "blocked"  # the agent waits on an approval/question UI
API_ERROR = "api_error"  # any other error reported by Herdr

AGENT_STATES = ("idle", "working", "blocked", "done", "unknown")


class HerdrError(Exception):
    def __init__(self, category: str, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.message = message


def _category_for_code(code: str) -> str:
    if code.endswith("_not_found"):
        return STALE_ID
    if code == "agent_blocked":
        return BLOCKED
    if code == "timeout":
        return TIMEOUT
    return API_ERROR


@dataclass
class ServerInfo:
    version: str
    protocol: int
    capabilities: dict[str, Any] = field(default_factory=dict)

    @property
    def protocol_supported(self) -> bool:
        return self.protocol >= MIN_PROTOCOL

    @property
    def protocol_tested(self) -> bool:
        return self.protocol == TESTED_PROTOCOL


def parse_pong(result: dict[str, Any]) -> ServerInfo:
    """Parse a ``ping`` result; raises ``protocol_mismatch`` when it is not a pong."""
    if result.get("type") != "pong" or not isinstance(result.get("protocol"), int):
        raise HerdrError(PROTOCOL_MISMATCH, f"unexpected ping result: {str(result)[:200]}")
    caps = result.get("capabilities")
    return ServerInfo(
        version=str(result.get("version") or "unknown"),
        protocol=int(result["protocol"]),
        capabilities=dict(caps) if isinstance(caps, dict) else {},
    )


# --------------------------------------------------------------------------- settings


def socket_path(settings: Settings) -> Path:
    """The API socket AgentTape uses: explicit path, else named session, else default."""
    if settings.herdr_socket is not None:
        return Path(settings.herdr_socket).expanduser()
    base = Path.home() / ".config" / "herdr"
    if settings.herdr_session:
        return base / "sessions" / settings.herdr_session / "herdr.sock"
    return base / "herdr.sock"


def session_label(settings: Settings) -> str:
    if settings.herdr_session:
        return settings.herdr_session
    return "custom socket" if settings.herdr_socket is not None else "default"


def attach_commands(
    settings_session: str | None, machine: str, target: str | None
) -> dict[str, Any]:
    """Copyable attach guidance. A browser cannot attach to a terminal; this is a command."""
    session_flag = ["--session", settings_session] if settings_session else []
    if machine != "local":
        return {
            "command": None,
            "guidance": (
                f"This run's Herdr session is on the saved machine '{machine}'. Open Herdr, "
                "select that machine in the sidebar, then open "
                + (f"agent '{target}'." if target else "the run's workspace.")
            ),
        }
    if not target:
        return {"command": None, "guidance": "No agent to attach to."}
    parts = ["herdr", *session_flag, "agent", "attach", target]
    return {
        "command": " ".join(parts),
        "guidance": "Run this in a terminal to open the agent in Herdr (the browser cannot attach to a terminal).",
    }


# --------------------------------------------------------------------------- client


class HerdrClient:
    def __init__(self, path: Path, *, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._seq = 0

    @classmethod
    def from_settings(cls, settings: Settings, *, timeout: float = 10.0) -> HerdrClient:
        return cls(socket_path(settings), timeout=timeout)

    def _request(self, method: str, params: dict[str, Any] | None) -> tuple[str, bytes]:
        self._seq += 1
        rid = f"agenttape:{self._seq}"
        line = json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n"
        return rid, line.encode()

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if not self.path.exists():
            raise HerdrError(UNAVAILABLE, "Herdr socket not found (is the Herdr server running?)")
        try:
            return await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.path), limit=16 * 1024 * 1024),
                timeout=self.timeout,
            )
        except TimeoutError:
            raise HerdrError(TIMEOUT, "timed out connecting to Herdr") from None
        except OSError as exc:
            raise HerdrError(
                UNAVAILABLE, f"cannot connect to Herdr: {exc.strerror or exc}"
            ) from None

    @staticmethod
    def _unwrap(message: dict[str, Any]) -> dict[str, Any]:
        if "error" in message:
            err = message.get("error") or {}
            code = str(err.get("code") or "error")
            raise HerdrError(_category_for_code(code), str(err.get("message") or code), code=code)
        result = message.get("result")
        if not isinstance(result, dict):
            raise HerdrError(PROTOCOL_MISMATCH, "Herdr response has no result object")
        return result

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send one request and return its ``result`` object."""
        _rid, data = self._request(method, params)
        reader, writer = await self._open()
        try:
            writer.write(data)
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout or self.timeout)
        except TimeoutError:
            raise HerdrError(TIMEOUT, f"Herdr did not answer {method} in time") from None
        except (ConnectionError, OSError) as exc:
            raise HerdrError(
                SOCKET_DISCONNECT, f"Herdr connection lost during {method}: {exc}"
            ) from None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if not raw:
            raise HerdrError(SOCKET_DISCONNECT, f"Herdr closed the connection during {method}")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            raise HerdrError(PROTOCOL_MISMATCH, f"Herdr sent invalid JSON for {method}") from None
        return self._unwrap(message)

    async def subscribe(
        self, subscriptions: list[dict[str, Any]]
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield ``{"event": ..., "data": ...}`` messages until the stream ends (then raises).

        The first message is a synthetic ``subscription_started`` once Herdr acknowledged.
        """
        _rid, data = self._request("events.subscribe", {"subscriptions": subscriptions})
        reader, writer = await self._open()
        try:
            writer.write(data)
            await writer.drain()
            first = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not first:
                raise HerdrError(SOCKET_DISCONNECT, "Herdr closed the subscription")
            self._unwrap(json.loads(first))
            yield {"event": "subscription_started", "data": {}}
            while True:
                raw = await reader.readline()
                if not raw:
                    raise HerdrError(SOCKET_DISCONNECT, "Herdr event stream ended")
                with contextlib.suppress(json.JSONDecodeError):
                    message = json.loads(raw)
                    if isinstance(message, dict) and "event" in message:
                        yield message
        except TimeoutError:
            raise HerdrError(TIMEOUT, "Herdr did not acknowledge the subscription") from None
        except (ConnectionError, OSError) as exc:
            raise HerdrError(SOCKET_DISCONNECT, f"Herdr event stream lost: {exc}") from None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # ------------------------------------------------------------------ typed helpers

    async def ping(self) -> ServerInfo:
        return parse_pong(await self.call("ping", {}))

    async def agent_get(self, target: str) -> dict[str, Any]:
        agent = (await self.call("agent.get", {"target": target})).get("agent")
        if not isinstance(agent, dict):
            raise HerdrError(PROTOCOL_MISMATCH, "agent.get returned no agent")
        return agent

    async def pane_get(self, pane_id: str) -> dict[str, Any]:
        pane = (await self.call("pane.get", {"pane_id": pane_id})).get("pane")
        if not isinstance(pane, dict):
            raise HerdrError(PROTOCOL_MISMATCH, "pane.get returned no pane")
        return pane


def ping_sync(path: Path, *, timeout: float = 1.0) -> ServerInfo:
    """Blocking ping for synchronous discovery (``AgentAdapter.discover``)."""
    if not path.exists():
        raise HerdrError(UNAVAILABLE, "Herdr socket not found (is the Herdr server running?)")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(b'{"id":"agenttape:ping","method":"ping","params":{}}\n')
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except TimeoutError:
        raise HerdrError(TIMEOUT, "Herdr did not answer ping in time") from None
    except OSError as exc:
        raise HerdrError(UNAVAILABLE, f"cannot connect to Herdr: {exc.strerror or exc}") from None
    try:
        message = json.loads(buf)
    except json.JSONDecodeError:
        raise HerdrError(PROTOCOL_MISMATCH, "Herdr sent invalid JSON for ping") from None
    return parse_pong(HerdrClient._unwrap(message))


# --------------------------------------------------------------------------- CLI (read-only)


def cli_path(settings: Settings) -> str | None:
    exe = settings.herdr_executable
    if "/" in exe:
        return exe if Path(exe).is_file() else None
    return shutil.which(exe)


def _run_cli(exe: str, args: list[str]) -> str | None:
    try:
        done = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def cli_version(exe: str) -> str | None:
    """Parse ``herdr --version`` output (``herdr 0.9.0``)."""
    out = _run_cli(exe, ["--version"])
    if not out:
        return None
    parts = out.strip().split()
    return parts[-1] if parts else None


#: Saved-machine fields shown in the UI (the list is read-only; SSH stays in Herdr).
MACHINE_FIELDS = ("label", "name", "ssh_target", "target", "host", "enabled", "remote_session")


def parse_machines(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("machines", [])
    if not isinstance(data, list):
        return []
    machines: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            machines.append({k: item[k] for k in MACHINE_FIELDS if k in item})
        elif isinstance(item, str):
            machines.append({"label": item})
    return machines


def machine_list(exe: str) -> list[dict[str, Any]] | None:
    """``herdr machine list --json`` (None when the CLI failed)."""
    out = _run_cli(exe, ["machine", "list", "--json"])
    return None if out is None else parse_machines(out)
