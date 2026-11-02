"""Shared helpers for the capture tests: hook payload builders, a loopback client, and
the MCP client and server helpers (``test_hooks*.py``, ``test_mcp*.py``).

Not collected by pytest: the module name has no ``test_`` prefix.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient
from mcp import StdioServerParameters
from sqlalchemy import func, select

from agenttape.api.app import create_app
from agenttape.api.routers import mcp as mcp_router
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import ProjectCreate
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.infrastructure.db.extra_models.runtime_sessions import RuntimeSession
from agenttape.infrastructure.db.models import Event, Project, Run, RunAttempt
from agenttape.mcp.client import CaptureClient, CaptureClientConfig, Transport
from agenttape.security.capture import CaptureTokenStore
from tests.integration.helpers import git


def svc_project(ctx: AppContext) -> tuple[str, Path]:
    """A git repo ``svc`` under the first allowed root, registered as a project."""
    repo = Path(ctx.settings.allowed_roots[0]) / "svc"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "greeter.py").write_text("def greet():\n    return 'hi'\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "init")
    project = ProjectService(ctx).create(ProjectCreate(name="Svc", repo_path=str(repo)))
    return project.id, repo


def running_attempt(
    ctx: AppContext, project_id: str, *, adapter_type: str = "claude-code"
) -> tuple[str, str]:
    """Insert a running ``Run``/``RunAttempt`` pair directly through the ORM."""
    with ctx.session() as session:
        ws_id = default_workspace_id(session, ctx.clock)
        now = ctx.clock.now()
        project = session.get(Project, project_id)
        assert project is not None
        run = Run(
            workspace_id=ws_id,
            project_id=project_id,
            title="capture test run",
            kind="agent",
            source="api",
            status="running",
            adapter_type=adapter_type,
            started_at=now,
        )
        session.add(run)
        session.flush()
        attempt = RunAttempt(
            run_id=run.id,
            attempt_number=1,
            status="running",
            workdir=project.repo_path,
        )
        session.add(attempt)
        session.flush()
        return run.id, attempt.id


def hooks_token(ctx: AppContext) -> str:
    """Mint (or replace) the per-machine ``hooks`` capture token."""
    return CaptureTokenStore(ctx.settings.data_dir).mint("hooks")


def hosted_attempt(ctx: AppContext, project_id: str) -> tuple[str, str]:
    """A running Herdr attempt (``adapter_type "herdr"``) with an active
    ``RuntimeSession`` (``agent_kind "claude"``) - a Herdr-hosted candidate for hook
    attachment (:meth:`ObservedSessionService._hosted_candidate`)."""
    run_id, attempt_id = running_attempt(ctx, project_id, adapter_type="herdr")
    with ctx.session() as session:
        session.add(
            RuntimeSession(
                run_id=run_id,
                attempt_id=attempt_id,
                runtime="herdr",
                status="active",
                agent_kind="claude",
                details={},
            )
        )
    return run_id, attempt_id


def post_hook(
    client: TestClient,
    token: str,
    body: dict[str, object],
    *,
    drain: bool = True,
    **headers: str,
):  # type: ignore[no-untyped-def]
    """``POST /api/v1/hooks/claude-code`` with a bearer ``token`` (``""``/``None`` omits
    the header entirely, so a missing-token test can be expressed the same way).

    The endpoint answers before its own ingest thread has necessarily finished:
    ``drain=True`` (the default) blocks until that context's ingest queue is empty, so
    existing synchronous assertions ("the run now exists") keep working unmodified. A
    drain that times out fails the test, so an assertion that nothing was recorded can
    never pass only because the hook had not been processed yet.
    """
    request_headers = {"Content-Type": "application/json", **headers}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    response = client.post("/api/v1/hooks/claude-code", json=body, headers=request_headers)
    if drain:
        ingest = client.app.state.ctx.extensions.get("hook_ingest")
        if ingest is not None:
            assert ingest.drain(), "hook ingest did not drain"
    return response


def loopback_client(ctx: AppContext) -> TestClient:
    """A TestClient whose peer address is loopback (the default "testclient" is not)."""
    app = create_app(ctx, web_dist=ctx.settings.data_dir / "no-web-dist")
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def live_server(ctx: AppContext, port: int = 0) -> Iterator[int]:
    """A real uvicorn server bound to 127.0.0.1, yielding the bound port."""
    import uvicorn

    app = create_app(ctx, web_dist=ctx.settings.data_dir / "no-web-dist")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    bound_port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield bound_port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ------------------------------------------------------------------ hook payloads

TRANSCRIPT_PATH = "/home/u/.claude/projects/x/abc.jsonl"
GREETER_CONTENT = "def greet():\n    return 'hi'\n"


def session_start(
    cwd: str, *, session_id: str, source: str = "startup", model: str | None = None
) -> dict[str, object]:
    body: dict[str, object] = {
        "hook_event_name": "SessionStart",
        "session_id": session_id,
        "cwd": cwd,
        "source": source,
        "transcript_path": TRANSCRIPT_PATH,
    }
    if model is not None:
        body["model"] = model
    return body


def session_end(
    cwd: str, *, session_id: str, reason: str = "prompt_input_exit"
) -> dict[str, object]:
    return {
        "hook_event_name": "SessionEnd",
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": TRANSCRIPT_PATH,
        "reason": reason,
    }


def pre_read(
    cwd: str, file_path: str, *, session_id: str, tool_use_id: str = "toolu_R1"
) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": TRANSCRIPT_PATH,
        "tool_name": "Read",
        "tool_use_id": tool_use_id,
        "tool_input": {"file_path": file_path},
    }


def post_tool_use(
    cwd: str,
    *,
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, object],
    tool_response: object,
    session_id: str,
) -> dict[str, object]:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": TRANSCRIPT_PATH,
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": tool_input,
        "tool_response": tool_response,
    }


def post_read(
    cwd: str,
    file_path: str,
    *,
    session_id: str,
    tool_use_id: str = "toolu_R1",
    content: str = GREETER_CONTENT,
) -> dict[str, object]:
    """A Claude Code ``Read`` PostToolUse: the file's text from line 1."""
    return post_tool_use(
        cwd,
        tool_name="Read",
        tool_use_id=tool_use_id,
        tool_input={"file_path": file_path},
        tool_response={"type": "text", "file": {"content": content, "startLine": 1}},
        session_id=session_id,
    )


# ------------------------------------------------------------------ MCP


def reset_mcp_rate_limiters() -> None:
    """The MCP router's rate limiters are module-level singletons (production reality: one
    process); resetting them per test keeps one test's failures from turning another's
    expected 401 into a 429."""
    mcp_router._AUTH_FAILURES.reset()
    mcp_router._WRITES.reset()


def to_thread_transport(client: TestClient) -> Transport:
    """A ``CaptureClient`` transport that forwards to an in-process ``TestClient``."""

    def transport(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        response = client.post(urlparse(url).path, content=body, headers=dict(headers))
        return response.status_code, response.content

    return transport


def interactive_client(
    http_client: TestClient, *, token: str, cwd: str, instance_id: str
) -> CaptureClient:
    config = CaptureClientConfig(
        api_base="http://ignored",
        token=token,
        attempt_id="",
        run_id="",
        instance_id=instance_id,
        cwd=cwd,
    )
    return CaptureClient(config, transport=to_thread_transport(http_client))


def seed_hook_session(client: TestClient, token: str, *, session_id: str, cwd: str) -> None:
    response = post_hook(
        client,
        token,
        {
            "hook_event_name": "SessionStart",
            "session_id": session_id,
            "cwd": cwd,
            "source": "startup",
        },
    )
    assert response.status_code == 200


def event_count(ctx: AppContext) -> int:
    with ctx.session() as session:
        return int(session.execute(select(func.count()).select_from(Event)).scalar_one())


def mcp_token(ctx: AppContext) -> str:
    """Mint (or replace) the per-machine ``mcp`` capture token."""
    return CaptureTokenStore(ctx.settings.data_dir).mint("mcp")


def stdio_params(
    port: int, run_id: str, attempt_id: str, token: str, data_dir: str
) -> StdioServerParameters:
    """``agenttape mcp serve`` over stdio with a launched attempt's environment."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agenttape.cli.main", "mcp", "serve"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "AGENTTAPE_API_URL": f"http://127.0.0.1:{port}",
            "AGENTTAPE_RUN_ID": run_id,
            "AGENTTAPE_ATTEMPT_ID": attempt_id,
            "AGENTTAPE_MCP_TOKEN": token,
            "AGENTTAPE_DATA_DIR": data_dir,
        },
    )
