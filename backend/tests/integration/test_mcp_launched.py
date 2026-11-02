"""A launched claude-code run gets the AgentTape MCP server wired via a per-attempt config
file, records a finding through it, and the token/config are gone once the attempt ends."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agenttape.api.app import create_app
from agenttape.application.context import AppContext
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.runs import RunService
from agenttape.bootstrap import build_context
from tests.conftest import drive, make_settings
from tests.integration.capture_helpers import free_port, live_server, svc_project
from tests.integration.test_process_adapters import make_exe, profile

FIXTURES = Path(__file__).parent.parent / "fixtures" / "adapters"

_FAKE_CLAUDE_BODY = f"""
import asyncio, json, os, sys

def main():
    argv = sys.argv[1:]
    stdin = sys.stdin.read()
    idx = argv.index("--mcp-config")
    config_path = argv[idx + 1]
    mode = os.stat(config_path).st_mode & 0o777
    with open(config_path) as fh:
        config = json.load(fh)
    server_cfg = config["mcpServers"]["agenttape"]

    async def call() -> bool:
        from mcp import Client, StdioServerParameters

        params = StdioServerParameters(
            command=server_cfg["command"], args=server_cfg["args"], env=server_cfg["env"]
        )
        async with Client(params) as client:
            result = await client.call_tool(
                "record_finding",
                {{
                    "kind": "risk",
                    "text": "greet() ignores its caller",
                    "citations": [
                        {{
                            "path": "src/greeter.py",
                            "line_start": 1,
                            "line_end": 2,
                            "quote": "def greet():\\n    return 'hi'",
                        }}
                    ],
                }},
            )
            return bool(result.is_error)

    is_error = asyncio.run(call())
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "mcp-call-record.json"), "w") as fh:
        json.dump({{"mode": mode, "is_error": is_error, "config": config}}, fh)
    record = {{"argv": argv, "stdin": stdin, "cwd": os.getcwd(), "env_keys": sorted(os.environ)}}
    with open(os.path.join(here, "last-call.json"), "w") as fh:
        json.dump(record, fh)
    for line in open({str(FIXTURES / "claude_code_success.jsonl")!r}):
        sys.stdout.write(line)
        sys.stdout.flush()
    sys.exit(0)

main()
"""


def mcp_call_record(ctx: AppContext) -> dict[str, object]:
    return dict(
        json.loads((ctx.settings.data_dir.parent / "bin" / "mcp-call-record.json").read_text())
    )


def test_launched_run_gets_the_mcp_server_and_records_a_finding(tmp_path: Path) -> None:
    port = free_port()
    settings = make_settings(tmp_path, port=port)
    ctx = build_context(settings, migrate=True)
    try:
        project_id, _repo = svc_project(ctx)
        exe = make_exe(ctx, "claude", _FAKE_CLAUDE_BODY)

        with live_server(ctx, port=port):
            pid = profile(ctx, "claude-code", executable=str(exe), project_id=project_id)
            run = RunService(ctx).create_run(
                RunCreate(agent_profile_id=pid, project_id=project_id, prompt="Fix src/greeter.py")
            )
            detail = drive(ctx, run.run.id)
            assert detail.run.status == "succeeded", detail.run.failure_message
            attempt_id = detail.attempts[0].id

            record = mcp_call_record(ctx)
            assert record["mode"] == 0o600
            assert set(record["config"]["mcpServers"]) == {"agenttape"}  # type: ignore[index]
            env = record["config"]["mcpServers"]["agenttape"]["env"]  # type: ignore[index]
            assert env["AGENTTAPE_RUN_ID"] == run.run.id
            assert env["AGENTTAPE_ATTEMPT_ID"] == attempt_id
            assert record["is_error"] is False

            last = json.loads((ctx.settings.data_dir.parent / "bin" / "last-call.json").read_text())
            argv = last["argv"]
            mcp_i = argv.index("--mcp-config")
            assert argv[mcp_i + 1] == str(
                ctx.settings.data_dir / "capture" / "attempts" / f"{attempt_id}.mcp.json"
            )
            assert argv[mcp_i + 2] == "--strict-mcp-config"
            assert "--append-system-prompt" in argv

            app = create_app(ctx, web_dist=ctx.settings.data_dir / "no-web-dist")
            with TestClient(
                app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
            ) as loopback:
                response = loopback.get(f"/api/v1/runs/{run.run.id}/context/findings")
                assert response.status_code == 200
                findings = response.json()
                assert any(f["source"] == "agenttape-mcp" for f in findings)

            token = env["AGENTTAPE_MCP_TOKEN"]
            data_dir = ctx.settings.data_dir
            assert not (data_dir / "capture" / "attempts" / f"{attempt_id}.mcp.json").exists()
            assert not (data_dir / "capture" / "attempts" / f"{attempt_id}.sha256").exists()

            with TestClient(
                app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
            ) as loopback:
                retry = loopback.post(
                    "/api/v1/mcp/findings",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-AgentTape-Attempt": attempt_id,
                        "Content-Type": "application/json",
                    },
                    json={"kind": "risk", "text": "should be refused", "citations": []},
                )
                assert retry.status_code == 401

            events_blob = json.dumps(
                [
                    e.model_dump(mode="json")
                    for e in RunService(ctx).list_events(run.run.id, limit=500).items
                ]
            )
            assert token not in events_blob
            assert token not in json.dumps(argv)
    finally:
        ctx.close()
