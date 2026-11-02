"""Herdr client, capability model, and profile validation (M2)."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenttape.adapters.builtin.herdr import HerdrAdapter
from agenttape.config import Settings
from agenttape.herdr import client as hc
from agenttape.herdr.capabilities import CAPABILITIES, RUNTIME_TABLE, capabilities_for
from agenttape.herdr.client import HerdrClient, HerdrError, parse_pong


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="hc", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- parsing


def test_pong_parsing_and_protocol_support() -> None:
    info = parse_pong(
        {"type": "pong", "version": "0.9.0", "protocol": 22, "capabilities": {"live_handoff": True}}
    )
    assert (info.version, info.protocol) == ("0.9.0", 22)
    assert info.protocol_supported and info.protocol_tested
    assert info.capabilities == {"live_handoff": True}
    newer = parse_pong({"type": "pong", "version": "1.0", "protocol": 23})
    assert newer.protocol_supported and not newer.protocol_tested
    assert not parse_pong({"type": "pong", "version": "0.1", "protocol": 3}).protocol_supported
    with pytest.raises(HerdrError) as exc:
        parse_pong({"type": "agent_list"})
    assert exc.value.category == hc.PROTOCOL_MISMATCH


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("agent_not_found", hc.STALE_ID),
        ("pane_not_found", hc.STALE_ID),
        ("workspace_not_found", hc.STALE_ID),
        ("agent_blocked", hc.BLOCKED),
        ("timeout", hc.TIMEOUT),
        ("invalid_request", hc.API_ERROR),
    ],
)
def test_error_codes_map_to_structured_categories(code: str, category: str) -> None:
    with pytest.raises(HerdrError) as exc:
        HerdrClient._unwrap({"id": "1", "error": {"code": code, "message": "m"}})
    assert exc.value.category == category and exc.value.code == code


def test_socket_path_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTTAPE_HERDR_SOCKET")
    home = Path.home() / ".config" / "herdr"
    assert hc.socket_path(Settings(data_dir=tmp_path)) == home / "herdr.sock"
    named = Settings(data_dir=tmp_path, herdr_session="work")
    assert hc.socket_path(named) == home / "sessions" / "work" / "herdr.sock"
    explicit = Settings(data_dir=tmp_path, herdr_session="work", herdr_socket=tmp_path / "s.sock")
    assert hc.socket_path(explicit) == tmp_path / "s.sock"
    assert hc.session_label(named) == "work"
    assert hc.session_label(Settings(data_dir=tmp_path)) == "default"


def test_attach_commands() -> None:
    local = hc.attach_commands(None, "local", "at-1234-a1")
    assert local["command"] == "herdr agent attach at-1234-a1"
    named = hc.attach_commands("work", "local", "w1:p1")
    assert named["command"] == "herdr --session work agent attach w1:p1"
    remote = hc.attach_commands(None, "buildbox", "at-1")
    assert remote["command"] is None and "buildbox" in remote["guidance"]


def test_machine_list_parsing_keeps_only_display_fields() -> None:
    text = json.dumps([{"label": "a", "ssh_target": "me@a", "enabled": True, "password": "x"}, "b"])
    assert hc.parse_machines(text) == [
        {"label": "a", "ssh_target": "me@a", "enabled": True},
        {"label": "b"},
    ]
    assert hc.parse_machines(json.dumps({"machines": [{"label": "c"}]})) == [{"label": "c"}]
    assert hc.parse_machines("not json") == []


# --------------------------------------------------------------------------- capability model


def test_capability_table_is_complete_and_honest() -> None:
    for key, caps in RUNTIME_TABLE.items():
        assert {c["name"] for c in caps} == set(CAPABILITIES), key
        assert all(c["support"] in ("supported", "degraded", "unsupported") for c in caps)
    by = {c["name"]: c["support"] for c in capabilities_for("herdr", "claude")}
    assert by["reconnect"] == "supported" and by["file_telemetry"] == "unsupported"
    assert by["read_output"] == "degraded"
    local = {c["name"]: c["support"] for c in capabilities_for("local", "claude-code")}
    assert local["reconnect"] == "unsupported" and local["attach"] == "unsupported"
    assert capabilities_for("herdr", "nope") == []


def test_profile_validation() -> None:
    adapter = HerdrAdapter()
    errors = lambda cfg: {i.field for i in adapter.validate_profile(cfg) if i.severity == "error"}  # noqa: E731
    assert errors({"agent_kind": "claude"}) == set()
    assert errors({}) == {"agent_kind"}
    assert errors({"agent_kind": "demo", "cleanup": "delete_everything"}) == {"cleanup"}
    assert errors({"agent_kind": "claude", "workspace_policy": "reuse"}) == {"workspace_policy"}
    assert (
        errors({"agent_kind": "claude", "workspace_policy": "reuse", "reuse_workspace_id": "w3"})
        == set()
    )
    assert "reuse_workspace_id" in errors(
        {"agent_kind": "claude", "workspace_policy": "reuse", "reuse_workspace_id": "; rm -rf"}
    )
    assert "reuse_agent" in errors(
        {"agent_kind": "demo", "workspace_policy": "reuse", "reuse_agent": "reviewer"}
    )
    warnings = [i for i in adapter.validate_profile({"agent_kind": "demo", "fallback": "local"})]
    assert [w.severity for w in warnings] == ["warning"]


def test_discovery_reports_unavailable_without_a_server() -> None:
    result = HerdrAdapter().discover()  # conftest points the socket at a missing path
    assert result.status == "unavailable" and "unavailable" in (result.message or "")
    assert set(result.details["runtime_capabilities"]) == {"claude", "codex", "demo"}


# --------------------------------------------------------------------------- transport failures


async def test_timeout_and_disconnect_are_structured(short_dir: Path) -> None:
    silent = short_dir / "silent.sock"
    closing = short_dir / "closing.sock"

    async def never_answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        await asyncio.sleep(5)

    async def hang_up(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.close()

    s1 = await asyncio.start_unix_server(never_answer, path=str(silent))
    s2 = await asyncio.start_unix_server(hang_up, path=str(closing))
    try:
        with pytest.raises(HerdrError) as exc:
            await HerdrClient(silent, timeout=0.2).ping()
        assert exc.value.category == hc.TIMEOUT
        with pytest.raises(HerdrError) as exc:
            await HerdrClient(closing, timeout=1).ping()
        assert exc.value.category == hc.SOCKET_DISCONNECT
        with pytest.raises(HerdrError) as exc:
            await HerdrClient(short_dir / "missing.sock").ping()
        assert exc.value.category == hc.UNAVAILABLE
    finally:
        s1.close()
        s2.close()


# --------------------------------------------------------------------------- real protocol

#: Request/response pairs captured from a real Herdr server (``herdr api schema --json``
#: and live calls in an isolated named session); see the file's ``about``.
PROTOCOL = json.loads((Path(__file__).parents[1] / "fixtures" / "herdr_protocol.json").read_text())
SRC = Path(hc.__file__).resolve().parents[1]


def test_every_method_agenttape_calls_exists_in_the_real_schema() -> None:
    import re

    used: set[str] = {"events.subscribe"}
    for path in (
        SRC / "adapters" / "builtin" / "herdr.py",
        SRC / "application" / "services" / "runtime.py",
        SRC / "herdr" / "client.py",
    ):
        used |= set(re.findall(r'call\(\s*"([a-z_.]+)"', path.read_text()))
    assert {"ping", "agent.get", "agent.prompt", "pane.report_agent"} <= used
    assert used <= set(PROTOCOL["schema"]["methods"])


def test_agent_fields_the_adapter_relies_on_are_in_the_real_schema() -> None:
    agent = PROTOCOL["schema"]["AgentInfo"]["properties"]
    # state_change_seq detects a whole turn between two polls; agent_session.kind is the
    # native session reference kind; agent/name/terminal_id prove the pane's identity.
    for name in ("agent_status", "state_change_seq", "agent_session", "agent", "name"):
        assert name in agent
    assert "terminal_id" in PROTOCOL["schema"]["AgentInfo"]["required"]
    assert "kind" in PROTOCOL["schema"]["AgentSessionInfo"]["properties"]
    assert set(PROTOCOL["schema"]["AgentStatus"]) == set(hc.AGENT_STATES)
    assert PROTOCOL["protocol"] == hc.TESTED_PROTOCOL


def test_client_parses_real_responses() -> None:
    by_method = {e["request"]["method"]: e["response"] for e in PROTOCOL["exchanges"]}
    info = parse_pong(HerdrClient._unwrap(by_method["ping"]))
    assert info.protocol_supported and info.protocol_tested
    errors = [e["response"] for e in PROTOCOL["exchanges"] if "error" in e["response"]]
    categories = {}
    for response in errors:
        with pytest.raises(HerdrError) as caught:
            HerdrClient._unwrap(response)
        categories[caught.value.code] = caught.value.category
    assert categories["agent_not_found"] == hc.STALE_ID
    assert categories["pane_not_found"] == hc.STALE_ID
    assert categories["invalid_request"] == hc.API_ERROR
    [reported] = [
        e["response"]["result"]["agent"]
        for e in PROTOCOL["exchanges"]
        if e["request"]["method"] == "agent.get"
        and e["response"].get("result", {}).get("agent", {}).get("agent_status") == "blocked"
    ]
    assert reported["agent"] == "agenttape-demo" and isinstance(reported["state_change_seq"], int)


def _keys_subset(fake: object, real: object, where: str) -> None:
    """Every key the fake returns exists in the real response (the fake invents nothing)."""
    if isinstance(fake, dict) and isinstance(real, dict):
        extra = set(fake) - set(real)
        assert not extra, f"{where}: fake-only keys {sorted(extra)}"
        for key in fake:
            _keys_subset(fake[key], real[key], f"{where}.{key}")
    elif isinstance(fake, list) and isinstance(real, list) and fake and real:
        _keys_subset(fake[0], real[0], f"{where}[0]")


def test_fake_herdr_answers_the_real_requests_with_real_shapes(short_dir: Path) -> None:
    from tests.fixtures.fake_herdr import FakeHerdr

    fake = FakeHerdr().start(short_dir / "h.sock")
    client = HerdrClient(short_dir / "h.sock", timeout=5)
    try:
        for exchange in PROTOCOL["exchanges"]:
            request, real = exchange["request"], exchange["response"]
            method = request["method"]
            if method in ("agent.list", "integration.list"):
                continue  # live lists differ by machine; their item shape is checked below
            try:
                result = asyncio.run(client.call(method, request["params"]))
            except HerdrError as exc:
                assert "error" in real, f"{method}: fake failed ({exc.code}) where Herdr answered"
                assert exc.code == real["error"]["code"], method
                continue
            assert "result" in real, f"{method}: fake answered where Herdr failed"
            assert result.get("type") == real["result"].get("type"), method
            _keys_subset(result, real["result"], method)
        sub = PROTOCOL["subscription"]

        async def first_event() -> tuple[dict[str, object], dict[str, object]]:
            created = await client.call("workspace.create", {"cwd": str(short_dir)})
            pane_id = created["root_pane"]["pane_id"]
            stream = client.subscribe([{"type": "pane.agent_status_changed", "pane_id": pane_id}])
            started = await anext(stream)
            await asyncio.to_thread(fake.set_status, pane_id, "blocked")
            event = await asyncio.wait_for(anext(stream), 5)
            await stream.aclose()
            return started, event

        started, event = asyncio.run(first_event())
        assert started["event"] == "subscription_started"
        assert event["event"] == sub["event"]["event"]
        _keys_subset(event, sub["event"], "event")
    finally:
        fake.stop()
