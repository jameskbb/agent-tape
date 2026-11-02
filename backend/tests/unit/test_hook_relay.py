"""``agenttape hooks relay`` (``cli/hook_relay.py``): the ``command`` hook that carries
Claude Code's SessionStart to AgentTape, because Claude Code will not run an ``http``
hook for SessionStart.

The relay posts its stdin unchanged to the hooks endpoint with the per-machine hooks
token read from AgentTape's 0600 token store, and fails open: it never raises, never
prints, and never outlives its hard deadline, whether AgentTape is down, slow, or
missing its token.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agenttape.api.routers.hooks import MAX_HOOK_BODY_BYTES
from agenttape.capture.claude_settings import (
    HOOK_URL_PATH,
    RELAY_HOOK_TIMEOUT_SECONDS,
    RELAY_PROBE_ARGS,
    RUN_ID_HEADER,
    RelayCommand,
    relay_entry,
)
from agenttape.cli import hook_relay
from agenttape.cli.hook_relay import (
    MAX_RELAY_BODY_BYTES,
    OUTCOMES,
    RELAY_DEADLINE_SECONDS,
    read_last_outcome,
    record_outcome,
    relay,
    relay_url_allowed,
)
from agenttape.security.capture import CaptureTokenStore

BODY = json.dumps(
    {
        "hook_event_name": "SessionStart",
        "session_id": "sess-relay",
        "cwd": "/home/u/repo",
        "source": "startup",
        "transcript_path": "/home/u/.claude/projects/x/t.jsonl",
    }
).encode("utf-8")


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeAgentTape:
    port: int
    requests: list[Received] = field(default_factory=list)
    delay: float = 0.0
    status: int = 200

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{HOOK_URL_PATH}"


@pytest.fixture
def agenttape() -> Iterator[FakeAgentTape]:
    """A loopback HTTP server that records every POST it receives and answers ``{}``."""
    state = FakeAgentTape(port=0)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            state.requests.append(
                Received(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            )
            if state.delay:
                time.sleep(state.delay)
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            return

    class QuietServer(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request: object, client_address: object) -> None:
            return  # the relay hanging up on a slow answer is the point of some tests

    server = QuietServer(("127.0.0.1", 0), Handler)
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _mint(data_dir: Path) -> str:
    return CaptureTokenStore(data_dir).mint("hooks", client_copy=True)


def _stdin(body: bytes, *, close: bool = True) -> tuple[int, int]:
    """A pipe carrying ``body``; its write end is closed (EOF) unless ``close`` is False.
    Returns ``(read_fd, write_fd)``; the caller closes whatever is still open."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, body)
    if close:
        os.close(write_fd)
        return read_fd, -1
    return read_fd, write_fd


def _relay(url: str, data_dir: Path, body: bytes, **kwargs: object) -> tuple[str, int | None]:
    read_fd, _ = _stdin(body)
    try:
        return relay(url=url, data_dir=data_dir, stdin_fd=read_fd, environ={}, **kwargs)  # type: ignore[arg-type]
    finally:
        os.close(read_fd)


def test_the_body_cap_matches_the_router() -> None:
    assert MAX_RELAY_BODY_BYTES == MAX_HOOK_BODY_BYTES


def test_the_relay_path_and_header_match_the_settings_module() -> None:
    """The relay keeps its own copies so it imports nothing but the standard library."""
    assert hook_relay.HOOK_URL_PATH == HOOK_URL_PATH
    assert hook_relay.RUN_ID_HEADER == RUN_ID_HEADER


def test_relay_posts_stdin_unchanged_with_the_stored_hooks_token(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    token = _mint(data_dir)
    assert _relay(agenttape.url, data_dir, BODY) == ("posted", 200)
    [request] = agenttape.requests
    assert request.path == HOOK_URL_PATH
    assert request.body == BODY
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.headers["content-type"] == "application/json"
    assert "x-agenttape-run-id" not in request.headers
    # Nothing a capture guard refuses: no browser or proxy headers.
    for refused in ("origin", "sec-fetch-site", "forwarded", "x-forwarded-for", "x-real-ip"):
        assert refused not in request.headers


def test_relay_forwards_the_run_id_env_the_http_hooks_send(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    _mint(data_dir)
    run_id = "0b6f1c3e-8f59-4d3c-9d8e-7c1f0a2b3c4d"
    read_fd, _ = _stdin(BODY)
    try:
        relay(
            url=agenttape.url,
            data_dir=data_dir,
            stdin_fd=read_fd,
            environ={"AGENTTAPE_RUN_ID": run_id},
        )
    finally:
        os.close(read_fd)
    [request] = agenttape.requests
    assert request.headers["x-agenttape-run-id"] == run_id


@pytest.mark.parametrize("run_id", ["x\ny", "x\r\nX-Evil: 1", "a b", "\u00e9t\u00e9", "r" * 129])
def test_a_malformed_run_id_is_left_out_and_the_hook_still_posts(
    agenttape: FakeAgentTape, data_dir: Path, run_id: str
) -> None:
    """``http.client`` refuses a header value with a CR or LF before it connects, which
    dropped the whole SessionStart; the server would have discarded the value anyway."""
    _mint(data_dir)
    read_fd, _ = _stdin(BODY)
    try:
        relay(
            url=agenttape.url,
            data_dir=data_dir,
            stdin_fd=read_fd,
            environ={"AGENTTAPE_RUN_ID": run_id},
        )
    finally:
        os.close(read_fd)
    [request] = agenttape.requests
    assert request.body == BODY
    assert "x-agenttape-run-id" not in request.headers
    assert "x-evil" not in request.headers


def test_relay_without_a_stored_token_sends_nothing(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    assert _relay(agenttape.url, data_dir, BODY) == ("no_token", None)
    assert agenttape.requests == []
    # Reading the token never creates AgentTape's data dir on a machine without one.
    assert not data_dir.exists()


def test_relay_ignores_proxy_settings(
    agenttape: FakeAgentTape, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy must never see the hooks token or a loopback capture request."""
    _mint(data_dir)
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    _relay(agenttape.url, data_dir, BODY)
    assert len(agenttape.requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com" + HOOK_URL_PATH,
        "https://127.0.0.1:8787" + HOOK_URL_PATH,
        "http://127.0.0.1:8787/api/v1/mcp/findings",
        "http://user:pw@127.0.0.1:8787" + HOOK_URL_PATH,
        "not a url",
        # Host names resolve through /etc/hosts and DNS at post time; setup only ever
        # writes an IP literal, so the relay accepts nothing else.
        "http://localhost:8787" + HOOK_URL_PATH,
        "http://localhost.localdomain:8787" + HOOK_URL_PATH,
        "http://[::1%25eth0]:8787" + HOOK_URL_PATH,
        "http://127.1:8787" + HOOK_URL_PATH,
        "http://2130706433:8787" + HOOK_URL_PATH,
        "http://0.0.0.0:8787" + HOOK_URL_PATH,
        "http://127.0.0.1.evil.com:8787" + HOOK_URL_PATH,
    ],
)
def test_relay_only_posts_to_agenttapes_loopback_hooks_endpoint(url: str) -> None:
    assert relay_url_allowed(url) is False


def test_relay_url_allowed_for_loopback_ip_literals() -> None:
    assert relay_url_allowed("http://127.0.0.1:8787" + HOOK_URL_PATH)
    assert relay_url_allowed("http://127.0.0.2:8787" + HOOK_URL_PATH)
    assert relay_url_allowed("http://[::1]:8787" + HOOK_URL_PATH)


def test_every_url_setup_writes_is_one_the_relay_accepts(tmp_path: Path) -> None:
    from agenttape.security.capture import capture_api_base
    from tests.conftest import make_settings

    for host in ("127.0.0.1", "0.0.0.0", "::1", "[::1]", "localhost", "example.com"):
        settings = make_settings(tmp_path, host=host, port=8787)
        assert relay_url_allowed(capture_api_base(settings) + HOOK_URL_PATH), host


def test_relay_fails_open_when_agenttape_is_down(data_dir: Path) -> None:
    _mint(data_dir)
    started = time.monotonic()
    # Port 9 (discard) on loopback: nothing listens there, so the connect is refused.
    assert _relay("http://127.0.0.1:9" + HOOK_URL_PATH, data_dir, BODY) == ("unreachable", None)
    assert time.monotonic() - started < RELAY_DEADLINE_SECONDS


def test_relay_gives_up_at_its_deadline_when_agenttape_is_slow(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    _mint(data_dir)
    agenttape.delay = 5.0
    started = time.monotonic()
    outcome = _relay(agenttape.url, data_dir, BODY, deadline=0.3)
    elapsed = time.monotonic() - started
    assert outcome == ("timeout", None)
    assert elapsed < 1.5


def test_relay_gives_up_at_its_deadline_when_stdin_never_closes(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    _mint(data_dir)
    read_fd, write_fd = _stdin(b'{"hook_event_name": ', close=False)
    try:
        started = time.monotonic()
        outcome = relay(
            url=agenttape.url, data_dir=data_dir, stdin_fd=read_fd, environ={}, deadline=0.3
        )
        assert outcome == ("timeout", None)
        assert time.monotonic() - started < 1.5
    finally:
        os.close(write_fd)
        os.close(read_fd)
    assert agenttape.requests == []


def test_relay_forwards_at_most_one_byte_past_the_cap(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    """An oversized body still reaches AgentTape (which answers 413 and counts the hook
    as dropped) without the relay reading an unbounded stdin into memory."""
    _mint(data_dir)
    huge = b"x" * (MAX_RELAY_BODY_BYTES + 4096)
    read_fd, write_fd = os.pipe()

    def feed() -> None:
        try:
            os.write(write_fd, huge)
        except OSError:
            pass
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        relay(url=agenttape.url, data_dir=data_dir, stdin_fd=read_fd, environ={})
    finally:
        os.close(read_fd)
        writer.join(timeout=5)
    [request] = agenttape.requests
    assert len(request.body) == MAX_RELAY_BODY_BYTES + 1


# ------------------------------------------------------------------ the CLI command


def _run_cli(
    url: str, data_dir: Path, stdin: bytes, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """The exact argv ``hooks setup`` writes into settings.json, run as Claude Code runs a
    ``command`` hook in exec form (no shell)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        **(extra_env or {}),
    }
    entry = relay_entry(RelayCommand(interpreter=sys.executable, data_dir=str(data_dir)), url=url)
    argv = [entry["command"], *entry["args"]]
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        env=env,
        timeout=RELAY_HOOK_TIMEOUT_SECONDS - 2,
        check=False,
    )


def test_cli_relay_posts_exits_zero_and_prints_nothing(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    token = _mint(data_dir)
    proc = _run_cli(agenttape.url, data_dir, BODY)
    assert proc.returncode == 0
    assert proc.stdout == b""  # SessionStart stdout would become model context
    assert proc.stderr == b""
    [request] = agenttape.requests
    assert request.body == BODY
    assert request.headers["authorization"] == f"Bearer {token}"


def test_cli_relay_exits_zero_silently_when_agenttape_is_down(data_dir: Path) -> None:
    _mint(data_dir)
    proc = _run_cli("http://127.0.0.1:9" + HOOK_URL_PATH, data_dir, BODY)
    assert proc.returncode == 0
    assert proc.stdout == b""
    assert proc.stderr == b""


def test_cli_relay_exits_zero_silently_with_no_token_and_bad_input(data_dir: Path) -> None:
    proc = _run_cli("http://example.com/elsewhere", data_dir, b"not json at all")
    assert proc.returncode == 0
    assert proc.stdout == b""
    assert proc.stderr == b""


def test_cli_relay_exits_zero_silently_when_agenttape_settings_are_invalid(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    _mint(data_dir)
    proc = _run_cli(agenttape.url, data_dir, BODY, {"AGENTTAPE_PORT": "not-a-port"})
    assert proc.returncode == 0
    assert proc.stdout == b""
    assert proc.stderr == b""


#: What the relay process may import from outside the standard library: not the
#: CLI, not AgentTape's settings (pydantic), not the API or database stack.
RELAY_AGENTTAPE_MODULES = {
    "agenttape",
    "agenttape.cli",
    "agenttape.cli.hook_relay",
    "agenttape.security",
    "agenttape.security.client_token",
}


def test_the_relay_imports_only_the_standard_library_and_the_token_reader(
    data_dir: Path,
) -> None:
    """Every SessionStart pays the relay's start-up, so it imports nothing but the stdlib
    and the token reader; an import error anywhere else in AgentTape cannot reach it."""
    _mint(data_dir)
    code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from agenttape.cli import hook_relay\n"
        "r, w = os.pipe(); os.write(w, b'{}'); os.close(w)\n"
        f"hook_relay.relay(url='http://127.0.0.1:9{HOOK_URL_PATH}', data_dir=Path({str(data_dir)!r}),"
        " stdin_fd=r, environ={})\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        timeout=RELAY_HOOK_TIMEOUT_SECONDS - 2,
        check=True,
    )
    loaded = set(proc.stdout.decode().split())
    bare = subprocess.run(
        [sys.executable, "-I", "-c", "import sys; print('\\n'.join(sys.modules))"],
        capture_output=True,
        timeout=RELAY_HOOK_TIMEOUT_SECONDS - 2,
        check=True,
    )
    at_start_up = set(bare.stdout.decode().split())  # e.g. a virtualenv's own site hook
    assert "agenttape.security.client_token" in loaded  # the token was read
    assert {m for m in loaded if m.split(".")[0] == "agenttape"} <= RELAY_AGENTTAPE_MODULES
    stdlib = set(sys.stdlib_module_names)
    third_party = {m for m in loaded - at_start_up if m.split(".")[0] not in stdlib | {"agenttape"}}
    assert third_party == set()


def test_the_relay_exits_zero_silently_on_arguments_it_does_not_expect(data_dir: Path) -> None:
    for args in (
        [],
        ["--url"],
        ["--data-dir", "relative/dir", "--url", "http://127.0.0.1:9" + HOOK_URL_PATH],
        ["--unknown", "x", "--data-dir", str(data_dir)],
    ):
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "agenttape.cli.hook_relay", *args],
            input=BODY,
            capture_output=True,
            timeout=RELAY_HOOK_TIMEOUT_SECONDS - 2,
            check=False,
        )
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b""), args


def test_an_import_failure_inside_the_relay_still_exits_zero_silently(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    """The catch-all covers imports too: the token reader failing to import is swallowed."""
    _mint(data_dir)
    code = (
        "import runpy, sys\n"
        "sys.modules['agenttape.security.client_token'] = None\n"
        f"sys.argv = ['x', '--data-dir', {str(data_dir)!r}, '--url', {agenttape.url!r}]\n"
        "runpy.run_module('agenttape.cli.hook_relay', run_name='__main__')\n"
        "print('the relay returned instead of exiting')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        input=BODY,
        capture_output=True,
        timeout=RELAY_HOOK_TIMEOUT_SECONDS - 2,
        check=False,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert agenttape.requests == []


def test_the_probe_proves_the_interpreter_can_run_the_relay(tmp_path: Path) -> None:
    """``hooks setup`` and ``status`` run this with the relay's interpreter; from a
    directory shadowing its imports, too."""
    marker = tmp_path / "hostile-code-ran"
    evil = _hostile_dir(tmp_path / "repo", marker)
    proc = subprocess.run(
        [sys.executable, *RELAY_PROBE_ARGS],
        cwd=evil,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=RELAY_HOOK_TIMEOUT_SECONDS - 2,
        check=False,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, hook_relay.PROBE_OUTPUT, b"")
    assert not marker.exists()


def test_the_older_hooks_relay_command_still_relays(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    """An entry an older setup wrote keeps working until ``hooks setup`` replaces it."""
    token = _mint(data_dir)
    argv = [
        sys.executable,
        "-I",
        "-m",
        "agenttape.cli.main",
        "--data-dir",
        str(data_dir),
        "hooks",
        "relay",
        "--url",
        agenttape.url,
    ]
    proc = subprocess.run(
        argv, input=BODY, capture_output=True, timeout=RELAY_HOOK_TIMEOUT_SECONDS - 2, check=False
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    [request] = agenttape.requests
    assert request.headers["authorization"] == f"Bearer {token}"


def test_cli_relay_exits_zero_at_its_deadline_when_agenttape_hangs(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    _mint(data_dir)
    agenttape.delay = 20.0
    started = time.monotonic()
    proc = _run_cli(agenttape.url, data_dir, BODY)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0
    assert proc.stdout == b""
    assert proc.stderr == b""
    # Interpreter start-up plus the relay's own deadline must finish with room to spare
    # inside Claude Code's own timeout for the hook, or Claude Code kills it.
    assert elapsed < RELAY_HOOK_TIMEOUT_SECONDS - 2


# ------------------------------------------------------- a hostile working directory

#: Modules the relay (or anything it imports) could load after start-up, plus the ones
#: Python itself looks for on ``PYTHONPATH`` at start-up. A repository the user opens
#: Claude Code in may hold any of them at its top level.
HOSTILE_MODULES = (
    "typer",
    "click",
    "pydantic",
    "json",
    "logging",
    "threading",
    "uuid",
    "typing",
    "secrets",
    "socket",
    "ipaddress",
    "tempfile",
    "sitecustomize",
    "usercustomize",
)


def _hostile_dir(root: Path, marker: Path) -> Path:
    """A directory whose every module (and an ``agenttape`` package) records that it ran
    and prints: exactly what a hostile or merely unlucky repository could hold."""
    root.mkdir()
    code = (
        "import os as _o\n"
        f"_fd = _o.open({str(marker)!r}, _o.O_WRONLY | _o.O_CREAT | _o.O_APPEND, 0o600)\n"
        "_o.write(_fd, __name__.encode() + b'\\n')\n"
        "_o.close(_fd)\n"
        "print('INJECTED', __name__)\n"
    )
    for name in HOSTILE_MODULES:
        (root / f"{name}.py").write_text(code)
    (root / "agenttape").mkdir()
    (root / "agenttape" / "__init__.py").write_text(code)
    return root


def _installed_relay_entry(
    tmp_path: Path, data_dir: Path, port: int, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    """The SessionStart entry ``agenttape hooks setup`` writes into settings.json."""
    from typer.testing import CliRunner

    from agenttape.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTTAPE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AGENTTAPE_PORT", str(port))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    settings_path = tmp_path / "claude" / "settings.json"
    result = CliRunner().invoke(app, ["hooks", "setup", "--settings", str(settings_path)])
    assert result.exit_code == 0, result.output
    doc = json.loads(settings_path.read_text())
    entry: dict[str, object] = doc["hooks"]["SessionStart"][0]["hooks"][0]
    assert entry["type"] == "command"
    return entry


@pytest.mark.parametrize("hostile", ["working_directory", "environment"])
def test_the_installed_relay_runs_nothing_from_the_session_directory_or_environment(
    hostile: str,
    agenttape: FakeAgentTape,
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code runs a hook with the session's project as its working directory and
    the session's environment (which a project's direnv can set). Neither may put code in
    place of AgentTape's, or put a single byte on stdout, which becomes model context."""
    entry = _installed_relay_entry(tmp_path, data_dir, agenttape.port, monkeypatch)
    marker = tmp_path / "hostile-code-ran"
    evil = _hostile_dir(tmp_path / "repo", marker)
    clean = tmp_path / "clean"
    clean.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home")}
    if hostile == "environment":
        env |= {
            "PYTHONPATH": str(evil),
            "PYTHONHOME": str(evil),
            "PYTHONSTARTUP": str(evil / "json.py"),
            "PYTHONINSPECT": "1",
            "PYTHONWARNINGS": "error",
            "PYTHONUSERBASE": str(evil),
        }
    args = entry["args"]
    assert isinstance(args, list)
    proc = subprocess.run(
        [str(entry["command"]), *args],
        input=BODY,
        capture_output=True,
        cwd=evil if hostile == "working_directory" else clean,
        env=env,
        timeout=RELAY_HOOK_TIMEOUT_SECONDS - 2,
        check=False,
    )
    assert not marker.exists(), marker.read_text()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b""
    assert proc.stderr == b""
    [request] = agenttape.requests
    assert request.body == BODY


# ------------------------------------------------------------ the last outcome


def _last_outcome_file(data_dir: Path) -> Path:
    return data_dir / "capture" / "hook-relay-last.json"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("posted", ("posted", 200)),
        ("rejected", ("rejected", 401)),
        ("unreachable", ("unreachable", None)),
        ("refused_url", ("refused_url", None)),
        ("no_token", ("no_token", None)),
        ("empty_input", ("empty_input", None)),
    ],
)
def test_the_relay_records_its_last_outcome_owner_only_without_token_or_body(
    agenttape: FakeAgentTape, data_dir: Path, case: str, expected: tuple[str, int | None]
) -> None:
    """Every outcome looked the same from outside (a later joined_mid_session verdict):
    the relay now leaves one small record, a timestamp and a closed code."""
    store = CaptureTokenStore(data_dir)
    token = store.mint("hooks", client_copy=case != "no_token")
    url = agenttape.url
    body = BODY
    if case == "rejected":
        agenttape.status = 401
    elif case == "unreachable":
        url = "http://127.0.0.1:9" + HOOK_URL_PATH
    elif case == "refused_url":
        url = "http://localhost:8787" + HOOK_URL_PATH
    elif case == "empty_input":
        body = b"  \n"
    proc = _run_cli(url, data_dir, body)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")

    path = _last_outcome_file(data_dir)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert token not in raw
    assert "sess-relay" not in raw and "SessionStart" not in raw
    record = json.loads(raw)
    assert set(record) == {"at", "outcome", "http_status"}
    assert (record["outcome"], record["http_status"]) == expected
    assert record["outcome"] in OUTCOMES
    assert read_last_outcome(data_dir) == record
    assert time.strftime("%Y-%m-%dT", time.gmtime()) in record["at"]


def test_the_relay_records_a_timeout_when_agenttape_hangs(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    _mint(data_dir)
    agenttape.delay = 20.0
    proc = _run_cli(agenttape.url, data_dir, BODY)
    assert proc.returncode == 0
    assert read_last_outcome(data_dir) is not None
    assert read_last_outcome(data_dir)["outcome"] == "timeout"  # type: ignore[index]


def test_the_relay_never_creates_a_data_dir_to_record_an_outcome(data_dir: Path) -> None:
    proc = _run_cli("http://127.0.0.1:9" + HOOK_URL_PATH, data_dir, BODY)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert not data_dir.exists()
    record_outcome(data_dir, ("no_token", None))
    assert not data_dir.exists()
    assert read_last_outcome(data_dir) is None


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"at": "x", "outcome": "made_up", "http_status": null}',
        '{"at": "x", "outcome": "posted", "http_status": "200"}',
        '{"at": 1, "outcome": "posted", "http_status": 200}',
    ],
)
def test_a_malformed_outcome_record_reads_as_none(data_dir: Path, raw: str) -> None:
    _mint(data_dir)
    _last_outcome_file(data_dir).write_text(raw)
    assert read_last_outcome(data_dir) is None


def test_the_relay_removes_its_own_stale_temp_files(
    agenttape: FakeAgentTape, data_dir: Path
) -> None:
    """A relay killed between writing its temp file and renaming it leaves the temp behind;
    the next run removes ones over a minute old, and nothing else."""
    _mint(data_dir)
    capture = data_dir / "capture"
    stale = capture / ".hook-relay-killed.tmp"
    fresh = capture / ".hook-relay-in-flight.tmp"
    unrelated = capture / ".tokens-old.tmp"
    for path in (stale, fresh, unrelated):
        path.write_text("{}")
    old = time.time() - 120
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))
    proc = _run_cli(agenttape.url, data_dir, BODY)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()
    assert not [p for p in capture.iterdir() if p.name.startswith(".hook-relay-") and p != fresh]
