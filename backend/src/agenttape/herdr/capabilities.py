"""The explicit runtime capability model: degraded capabilities are reported honestly.

Each runtime/adapter combination declares, per capability, ``supported``, ``degraded``
(works with a stated limitation), or ``unsupported``. Nothing pretends: e.g. a local
process cannot be reattached after an AgentTape restart, and Claude Code under Herdr gives
AgentTape lifecycle state but no tool-level file telemetry.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Support = Literal["supported", "degraded", "unsupported"]

CAPABILITIES: dict[str, str] = {
    "start": "Start an agent for a run",
    "prompt": "Send the resolved prompt",
    "observe_state": "Observe lifecycle state (working, blocked, idle, done, unknown)",
    "subscribe_events": "Push notifications instead of polling",
    "read_output": "Read recent visible output",
    "interrupt": "Interrupt or cancel the agent",
    "reconnect": "Reattach after AgentTape restarts",
    "attach": "A person can open the live session interactively",
    "file_telemetry": "Tool-level file read/write telemetry",
}


class Capability(TypedDict):
    name: str
    description: str
    support: Support
    note: str


def _caps(**entries: tuple[Support, str]) -> list[Capability]:
    missing = set(CAPABILITIES) - set(entries)
    assert not missing, f"capability table incomplete: {missing}"
    return [
        Capability(name=name, description=CAPABILITIES[name], support=s, note=note)
        for name, (s, note) in entries.items()
    ]


_LOCAL_COMMON: dict[str, tuple[Support, str]] = {
    "start": ("supported", "Runs as a child process of the AgentTape worker"),
    "observe_state": ("degraded", "Running or exited only; no blocked/idle distinction"),
    "read_output": ("supported", "Full stdout/stderr is captured"),
    "interrupt": ("supported", "SIGTERM, then SIGKILL after the grace period"),
    "reconnect": ("unsupported", "The process dies with the worker; the attempt is retried"),
    "attach": ("unsupported", "Headless process; nothing to attach to"),
}

RUNTIME_TABLE: dict[str, list[Capability]] = {
    "local:claude-code": _caps(
        **_LOCAL_COMMON,
        prompt=("degraded", "Once, at start (print mode)"),
        subscribe_events=("supported", "stream-json on stdout"),
        file_telemetry=("supported", "Tool calls with file paths from stream-json"),
    ),
    "local:codex-cli": _caps(
        **_LOCAL_COMMON,
        prompt=("degraded", "Once, at start (exec mode)"),
        subscribe_events=("supported", "JSON events on stdout"),
        file_telemetry=("supported", "Tool calls with file paths from JSON events"),
    ),
    "local:generic-command": _caps(
        **_LOCAL_COMMON,
        prompt=("degraded", "Once, at start (stdin, env, or argument)"),
        subscribe_events=("degraded", "Only when the command prints AgentTape JSONL"),
        file_telemetry=("degraded", "Only when the command reports it"),
    ),
    "local:demo": _caps(
        **_LOCAL_COMMON,
        prompt=("degraded", "Once, at start"),
        subscribe_events=("supported", "Structured records"),
        file_telemetry=("supported", "Explicit file_read / file_write records"),
    ),
    "herdr:claude": _caps(
        start=("supported", "agent.start in an AgentTape-created pane"),
        prompt=("supported", "agent.prompt (not raw keystrokes)"),
        observe_state=("supported", "idle, working, blocked, done, unknown from Herdr"),
        subscribe_events=(
            "supported",
            "events.subscribe, with bounded agent.get polling as fallback",
        ),
        read_output=(
            "degraded",
            "Recent screen text only; alternate-screen output is not in scrollback",
        ),
        interrupt=("supported", "agent.send_keys ctrl+c (never while blocked)"),
        reconnect=(
            "supported",
            "Herdr keeps the agent running; AgentTape reattaches by pane/agent id",
        ),
        attach=("supported", "herdr agent attach <name>"),
        file_telemetry=(
            "unsupported",
            "Herdr reports lifecycle state, not tool calls; Git reconciliation still runs",
        ),
    ),
    "herdr:codex": _caps(
        start=("supported", "agent.start in an AgentTape-created pane"),
        prompt=("supported", "agent.prompt (not raw keystrokes)"),
        observe_state=("supported", "idle, working, blocked, done, unknown from Herdr"),
        subscribe_events=(
            "supported",
            "events.subscribe, with bounded agent.get polling as fallback",
        ),
        read_output=(
            "degraded",
            "Recent screen text only; alternate-screen output is not in scrollback",
        ),
        interrupt=("supported", "agent.send_keys ctrl+c (never while blocked)"),
        reconnect=(
            "supported",
            "Herdr keeps the agent running; AgentTape reattaches by pane/agent id",
        ),
        attach=("supported", "herdr agent attach <name>"),
        file_telemetry=(
            "unsupported",
            "Herdr reports lifecycle state, not tool calls; Git reconciliation still runs",
        ),
    ),
    "herdr:demo": _caps(
        start=("supported", "An AgentTape-built command in an AgentTape-created pane"),
        prompt=("degraded", "The demo agent follows a fixed scenario"),
        observe_state=("supported", "AgentTape reports working/blocked/idle to Herdr for the pane"),
        subscribe_events=("supported", "events.subscribe, with bounded polling as fallback"),
        read_output=("supported", "The telemetry file (every record the agent wrote)"),
        interrupt=("supported", "pane.send_keys ctrl+c"),
        reconnect=("supported", "Continues the telemetry file from the stored offset"),
        attach=("supported", "herdr agent attach <pane>"),
        file_telemetry=("supported", "The demo agent's telemetry records"),
    ),
}


def capabilities_for(runtime: str, kind: str) -> list[Capability]:
    return RUNTIME_TABLE.get(f"{runtime}:{kind}", [])


def table() -> dict[str, list[Capability]]:
    return dict(RUNTIME_TABLE)
