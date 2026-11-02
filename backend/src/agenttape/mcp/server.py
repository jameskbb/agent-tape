"""The AgentTape MCP server: one tool, ``record_finding``, served over stdio.

Never touches the database, and never opens SQLAlchemy or an application context - it
converts a tool call into one loopback HTTP POST via :class:`agenttape.mcp.client.CaptureClient`.
Logging must go to stderr only; stdout is the MCP wire protocol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agenttape.application.schemas.capture import (
    EventCitationIn,
    FileCitationIn,
    FindingRecordIn,
    ReviewTargetIn,
)
from agenttape.mcp.client import CaptureClient, CaptureClientConfig, CaptureError
from agenttape.mcp.launch import SERVER_NAME

_RECORD_FINDING_DOCSTRING = (
    "Record one finding AgentTape will independently check. Cite the exact text you read: "
    "the quote is the evidence, line numbers are a hint AgentTape recomputes. A file citation "
    "is {path, line_start, line_end, quote}. An event citation is {event_ref: the tool_use id "
    "of a tool call whose output you quote, quote}. Text seen only in Grep or Glob output is "
    "not a file read: Read the file before citing it as a file citation, or cite that search "
    "as an event citation. `target` {run_id, finding_key, outcome: "
    "upheld | refuted | inconclusive} is only for an adversarial reviewer challenging another "
    "run's finding; leave it out otherwise. In an interactive session, `session_id` is only "
    "needed if AgentTape reports more than one active Claude Code session in this directory."
)


def build_server(client: CaptureClient) -> MCPServer:
    server = MCPServer(SERVER_NAME)

    @server.tool(description=_RECORD_FINDING_DOCSTRING)
    async def record_finding(
        kind: str,
        text: str,
        citations: list[FileCitationIn | EventCitationIn],
        target: ReviewTargetIn | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        record = FindingRecordIn(
            kind=kind,
            text=text,
            citations=citations,
            target=target,
            session_id=session_id,
            cwd=client.config.cwd,
            instance_id=client.config.instance_id,
        )
        try:
            result = await asyncio.to_thread(client.post_finding, record)
        except CaptureError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "summary": f"Recorded finding on run {result.get('run_id')}",
            "run_id": result.get("run_id"),
            "event_id": result.get("event_id"),
            "citations": result.get("citations"),
        }

    return server


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    config = CaptureClientConfig.from_env(os.environ)
    client = CaptureClient(config)
    server = build_server(client)
    server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
