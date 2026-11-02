"""Helpers shared by built-in evaluators (private module: not auto-registered)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenttape.adapters.base import build_child_env
from agenttape.evaluations.base import EvaluationContext
from agenttape.security.redaction import DEFAULT_REDACTOR

MAX_TEXT_BYTES = 2 * 1024 * 1024


def project_settings(ctx: EvaluationContext) -> dict[str, Any]:
    return dict(ctx.extras.get("project_settings") or {})


def resolve_inside(workdir: Path, relative: str) -> Path | None:
    """Resolve ``relative`` under ``workdir``; ``None`` when it escapes the directory."""
    if not relative or Path(relative).is_absolute():
        return None
    root = workdir.resolve()
    target = (root / relative).resolve()
    if target != root and not target.is_relative_to(root):
        return None
    return target


def text_of(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    size = 0
    for e in events:
        payload = e.get("payload") or {}
        text = payload.get("text") or payload.get("output") or payload.get("message") or ""
        if not isinstance(text, str):
            text = str(text)
        parts.append(text)
        size += len(text)
        if size > MAX_TEXT_BYTES:
            break
    return "\n".join(parts)[:MAX_TEXT_BYTES]


@dataclass
class CommandRun:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    error: str | None = None


async def run_argv(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    env_allowlist: list[str],
    max_output: int = 64 * 1024,
    extra_env: Mapping[str, str] | None = None,
) -> CommandRun:
    """Run an argument array (no shell) with a timeout, killing its process group.
    ``extra_env`` is applied over the allowlisted environment (a replay pins ``PATH``)."""
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=build_child_env(env_allowlist, extra_env or {}),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandRun(None, "", "", 0, False, error=str(exc))
    timed_out = False
    out, err = bytearray(), bytearray()

    async def drain(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
        if stream is None:
            return
        while chunk := await stream.read(65536):
            buf.extend(chunk)
            if len(buf) > 2 * max_output:
                del buf[: len(buf) - max_output]  # keep a bounded tail

    try:
        await asyncio.wait_for(
            asyncio.gather(drain(proc.stdout, out), drain(proc.stderr, err), proc.wait()),
            timeout=timeout,
        )
    except TimeoutError:
        timed_out = True
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)

    def clip(data: bytearray) -> str:
        text = data.decode("utf-8", errors="replace")
        if len(text) > max_output:
            text = "...[truncated]...\n" + text[-max_output:]
        return DEFAULT_REDACTOR.redact_text(text)

    return CommandRun(
        exit_code=proc.returncode,
        stdout=clip(out),
        stderr=clip(err),
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=timed_out,
    )
