"""Per-attempt MCP launch config for the ``claude-code`` adapter.

No ``mcp`` SDK import here: the adapter stays light. The config file is written to an
owner-only path under the attempt's data dir and passed as ``--mcp-config <path>``,
naming only the AgentTape server, so ``--strict-mcp-config`` never hides a profile's other
MCP servers by accident - it hides them on purpose.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from agenttape.security.capture import attempts_dir, is_attempt_id

SERVER_NAME = "agenttape"
RECORD_FINDING_TOOL = "mcp__agenttape__record_finding"

#: One paragraph, no newline characters: a concrete trigger,
#: not just the tool's own docstring.
FINDINGS_PROMPT_CONTRACT = (
    "AgentTape records this run and will independently check your citations against what "
    "you actually read; for every bug, risk, recommendation, or decision you report, call "
    "the record_finding tool (from the agenttape MCP server) before your final answer, one "
    "call per finding; cite only text you read in this session; text seen only in Grep or "
    "Glob output is not a file read, so Read the file before citing it as a file citation, "
    "or cite that search as an event citation; a file citation is {path "
    "relative to the working directory, line_start, line_end, quote} where the quote is "
    "copied exactly from what you read (not paraphrased) and the line numbers are a hint; "
    "an event citation is {event_ref: the tool_use id of the command or tool call, quote: "
    'exact text from its output}; a finding with no citation is shown as "declared only".'
)


def launch_config(
    *, api_base: str, run_id: str, attempt_id: str, token: str, data_dir: Path
) -> dict[str, Any]:
    return {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "agenttape.cli.main", "mcp", "serve"],
                "env": {
                    "AGENTTAPE_API_URL": api_base,
                    "AGENTTAPE_RUN_ID": run_id,
                    "AGENTTAPE_ATTEMPT_ID": attempt_id,
                    "AGENTTAPE_MCP_TOKEN": token,
                    "AGENTTAPE_DATA_DIR": str(data_dir),
                },
            }
        }
    }


def write_launch_config(data_dir: Path, attempt_id: str, config: dict[str, Any]) -> Path:
    if not is_attempt_id(attempt_id):
        raise ValueError(f"attempt id {attempt_id!r} is not a canonical UUID")
    path = attempts_dir(data_dir) / f"{attempt_id}.mcp.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    return path


def remove_launch_config(data_dir: Path, attempt_id: str) -> None:
    if not is_attempt_id(attempt_id):
        return
    path = attempts_dir(data_dir) / f"{attempt_id}.mcp.json"
    path.unlink(missing_ok=True)
