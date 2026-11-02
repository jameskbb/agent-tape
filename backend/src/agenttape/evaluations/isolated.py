"""CPU-bound matching for evaluators, run in a short-lived child process with a deadline.

A user-written regular expression (or a JSON Schema ``pattern``) can backtrack
catastrophically on agent-shaped output. Python's ``re`` holds the GIL while it matches, so
a thread would still stall the worker's event loop and every other run on it. A child
process can be killed at the deadline instead; the evaluation then reports an error.

The child reads one JSON request from stdin (never argv, which other users can see) and
writes one JSON result to stdout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from typing import Any

MATCH_TIMEOUT_SECONDS = 10.0
MAX_MATCHES = 20


class MatchTimeoutError(Exception):
    """The matching work did not finish before the deadline."""


async def run_isolated(task: str, payload: dict[str, Any], *, timeout: float | None = None) -> Any:
    timeout = MATCH_TIMEOUT_SECONDS if timeout is None else timeout
    request = json.dumps({"task": task, **payload}, default=str).encode()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agenttape.evaluations.isolated",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(request), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise MatchTimeoutError(
            f"matching did not finish within {timeout:g} s; the pattern may backtrack "
            "catastrophically on this output"
        ) from None
    if proc.returncode != 0:
        tail = err.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"matching failed: {tail[0][:300]}")
    return json.loads(out)


def _regex(req: dict[str, Any]) -> list[str]:
    matches: list[str] = []
    for m in re.compile(req["pattern"], int(req["flags"])).finditer(req["text"]):
        matches.append(m.group(0)[:200])
        if len(matches) >= MAX_MATCHES:
            break
    return matches


def _json_schema(req: dict[str, Any]) -> list[dict[str, str]]:
    import jsonschema

    validator = jsonschema.Draft202012Validator(req["schema"])
    errors = sorted(validator.iter_errors(req["document"]), key=lambda e: list(e.path))
    return [
        {"path": "/".join(str(p) for p in e.path), "message": e.message[:300]}
        for e in errors[:MAX_MATCHES]
    ]


_TASKS = {"regex": _regex, "json_schema": _json_schema}


def _main() -> None:
    req = json.loads(sys.stdin.buffer.read())
    json.dump(_TASKS[req["task"]](req), sys.stdout)


if __name__ == "__main__":
    _main()
