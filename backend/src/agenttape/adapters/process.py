"""Subprocess helper shared by every process-based adapter (ADR 0005).

Guarantees:

* argument arrays only (``create_subprocess_exec``); a shell is never involved;
* the child runs in its own session/process group, so cancellation and timeouts kill the
  whole tree (SIGTERM, then SIGKILL after a grace period);
* stdout and stderr are drained concurrently in bounded chunks, so a chatty child can
  never deadlock on a full pipe and one enormous line cannot exhaust memory: a line longer
  than :data:`MAX_LINE_BYTES` is split into pieces of at most that size (a stream-json
  adapter passes a larger ``max_stdout_line_bytes`` so a long tool result on stdout still
  arrives as one parseable line; stderr always keeps the plain-output bound);
* every line is redacted, appended to a size-capped raw log file, and forwarded as an
  event (the event sink batches and caps what reaches the database); a line that is a JSON
  object or array is redacted string by string (:func:`redact_line`), so redaction can never
  break the JSON a stream-json adapter parses; a line longer than :data:`MAX_LINE_BYTES` is
  redacted in a worker thread, off the event loop.

POSIX only (Linux, macOS, WSL).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from agenttape.adapters.base import ExecutionContext
from agenttape.domain.enums import EventType, FailureCategory, Severity
from agenttape.domain.events import EventDraft

READ_CHUNK = 64 * 1024
MAX_LINE_BYTES = 64 * 1024
#: Look-ahead used to keep a secret from being split across two pieces of an over-long line.
REDACTION_OVERLAP_BYTES = 4 * 1024
DRAIN_TIMEOUT_SECONDS = 5.0

Stream = Literal["stdout", "stderr"]
LineHandler = Callable[[str], Awaitable[bool]]
"""Return True when the line was fully handled (no default stdout/stderr event wanted)."""


@dataclass
class CommandSpec:
    argv: list[str]
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    #: Shown in the ``command`` event instead of the raw argv (e.g. to hide a long prompt).
    display: str | None = None


@dataclass
class ProcessResult:
    exit_code: int | None
    pid: int | None
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    #: Set when the process was stopped by cancellation/timeout (the token's reason).
    stopped_reason: FailureCategory | None = None
    spawn_error: str | None = None


class _RawLog:
    """Append-only, size-capped raw log file for one stream of one attempt."""

    def __init__(self, path: Path, cap: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.cap = cap
        self.written = path.stat().st_size if path.exists() else 0
        self._fh: IO[bytes] | None = path.open("ab")
        self.truncated = False

    def write(self, data: bytes) -> None:
        if self._fh is None or self.truncated:
            return
        room = self.cap - self.written
        if len(data) > room:
            self._fh.write(data[: max(0, room)])
            self._fh.write(b"\n[agenttape: raw log truncated at size cap]\n")
            self.written = self.cap
            self.truncated = True
            return
        self._fh.write(data)
        self.written += len(data)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _redact_strings(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            (redact(k) if isinstance(k, str) else k): _redact_strings(v, redact)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_strings(v, redact) for v in value]
    return value


def redact_line(text: str, redact: Callable[[str], str]) -> str:
    """Redact one output line without breaking it when it is JSON.

    A line holding a JSON object or array is redacted string by string (keys and values,
    decoded) and re-serialized only when something was redacted, so a line with nothing to
    redact comes back byte for byte. Redacting the serialized text instead would let a
    pattern consume JSON escapes: in ``PREFIX = \\"atm_\\"`` the unquoted-value branch of
    the assignment pattern takes the backslash of the escaped quote and leaves invalid JSON,
    and a ``\\n14\\t`` escape run is swallowed into the value, merging lines. Decoding first
    also catches a secret the serialization escapes (one holding a quote or a backslash).
    Any other line is redacted as text.
    """
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            record = json.loads(stripped)
        except ValueError:
            record = None
        if isinstance(record, (dict, list)):
            cleaned = _redact_strings(record, redact)
            if cleaned == record:
                return text
            return json.dumps(cleaned, ensure_ascii=False)
    return redact(text)


def _redaction_safe_cut(data: bytes, redact: Callable[[str], str]) -> int:
    """Where to split an over-long line so no redactable value spans the two pieces.

    A split is safe when redacting both sides separately gives the same text as redacting
    them together. Otherwise the cut moves back by the overlap window, which exceeds any
    realistic secret length, so a straddling value ends up whole in the next piece.
    """
    cut = MAX_LINE_BYTES
    while cut > REDACTION_OVERLAP_BYTES:
        head = data[:cut].decode("utf-8", errors="replace")
        tail = data[cut : cut + REDACTION_OVERLAP_BYTES].decode("utf-8", errors="replace")
        if redact(head + tail) == redact(head) + redact(tail):
            return cut
        cut -= REDACTION_OVERLAP_BYTES
    return cut


def _kill_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


async def run_process(
    spec: CommandSpec,
    ctx: ExecutionContext,
    *,
    on_stdout: LineHandler | None = None,
    on_stderr: LineHandler | None = None,
    grace_seconds: float | None = None,
    emit_command_event: bool = True,
    max_stdout_line_bytes: int = MAX_LINE_BYTES,
) -> ProcessResult:
    """Run ``spec`` to completion or until ``ctx.cancel`` fires.

    A line is split only once its unfinished part grows past its bound
    (``max_stdout_line_bytes`` on stdout, :data:`MAX_LINE_BYTES` on stderr) plus the
    :data:`REDACTION_OVERLAP_BYTES` look-ahead, so a line up to that sum always reaches the
    handler whole (a line up to one read chunk longer can too, when its newline arrives in
    the chunk that crosses the bound). A longer line is delivered in
    :data:`MAX_LINE_BYTES` pieces flagged ``truncated``, so at most the bound plus
    :data:`REDACTION_OVERLAP_BYTES` plus one :data:`READ_CHUNK` of a line is ever
    buffered.
    """
    if not spec.argv:
        raise ValueError("argv must not be empty")
    max_stdout_line_bytes = max(max_stdout_line_bytes, MAX_LINE_BYTES)
    redact = ctx.redact
    grace = float(
        grace_seconds if grace_seconds is not None else ctx.limits.get("cancel_grace_seconds", 5.0)
    )
    raw_log_cap = int(ctx.limits.get("max_raw_log_bytes", 50 * 1024 * 1024))
    started = time.monotonic()
    prefix = f"attempt-{ctx.attempt.number}"
    logs: dict[Stream, _RawLog] = {
        "stdout": _RawLog(ctx.log_dir / f"{prefix}.stdout.log", raw_log_cap),
        "stderr": _RawLog(ctx.log_dir / f"{prefix}.stderr.log", raw_log_cap),
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=str(spec.cwd) if spec.cwd else None,
            env=spec.env or None,
            stdin=asyncio.subprocess.PIPE if spec.stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError, OSError) as exc:
        for log in logs.values():
            log.close()
        await ctx.events.event(
            EventType.ERROR,
            f"Could not start {Path(spec.argv[0]).name}: {exc.strerror or exc}",
            {"argv0": spec.argv[0], "error": str(exc)},
            severity=Severity.ERROR,
        )
        return ProcessResult(
            exit_code=None,
            pid=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout_bytes=0,
            stderr_bytes=0,
            spawn_error=str(exc),
        )

    if emit_command_event:
        await ctx.events.event(
            EventType.COMMAND,
            spec.display or " ".join(spec.argv)[:300],
            {
                "argv": spec.argv,
                "cwd": str(spec.cwd) if spec.cwd else None,
                "pid": proc.pid,
                "env_keys": sorted(spec.env),
            },
        )

    counts: dict[Stream, int] = {"stdout": 0, "stderr": 0}

    async def feed_stdin() -> None:
        if spec.stdin is None or proc.stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(spec.stdin)
            await proc.stdin.drain()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.close()

    async def pump(stream: Stream, reader: asyncio.StreamReader | None) -> None:
        if reader is None:
            return
        handler = on_stdout if stream == "stdout" else on_stderr
        #: How long a line may grow before it is split. Only stdout carries stream-json.
        line_bound = max_stdout_line_bytes if stream == "stdout" else MAX_LINE_BYTES
        log = logs[stream]
        carry = b""
        #: How much of ``carry`` is known to hold no newline (so a long line is scanned once).
        scanned = 0
        #: True while the current line is being split: its remaining pieces go out at
        #: ``MAX_LINE_BYTES`` rather than waiting for ``line_bound`` again.
        splitting = False

        async def deliver(raw: bytes, truncated: bool = False) -> None:
            decoded = raw.decode("utf-8", errors="replace")
            if len(raw) > MAX_LINE_BYTES:
                # A line up to the stream-json bound (megabytes) can take a second or more
                # to redact; doing that on the loop would stall the cancellation watcher,
                # the other pump, and sink flushes. Pieces and short lines stay here.
                text = await asyncio.to_thread(redact_line, decoded, redact)
            else:
                text = redact_line(decoded, redact)
            log.write(text.encode("utf-8") + b"\n")
            handled = await handler(text) if handler is not None else False
            if not handled:
                await ctx.events.emit(
                    EventDraft(
                        type=EventType.STDOUT if stream == "stdout" else EventType.STDERR,
                        summary=text[:200],
                        payload={
                            "text": text,
                            "stream": stream,
                            **({"truncated": True} if truncated else {}),
                        },
                        severity=Severity.INFO if stream == "stdout" else Severity.NOTICE,
                    )
                )
            else:
                ctx.events.touch()

        while True:
            chunk = await reader.read(READ_CHUNK)
            if not chunk:
                break
            counts[stream] += len(chunk)
            carry += chunk
            while True:
                nl = carry.find(b"\n", scanned)
                if nl < 0:
                    scanned = len(carry)
                    break
                line, carry = carry[:nl], carry[nl + 1 :]
                scanned = 0
                splitting = False
                await deliver(line.rstrip(b"\r"))
            limit = MAX_LINE_BYTES if splitting else line_bound
            if len(carry) > limit + REDACTION_OVERLAP_BYTES:
                # An over-long line is split into bounded pieces rather than buffered, at a
                # point no secret straddles (each piece is redacted on its own).
                splitting = True
                pos = 0
                while len(carry) - pos > MAX_LINE_BYTES + REDACTION_OVERLAP_BYTES:
                    window = carry[pos : pos + MAX_LINE_BYTES + REDACTION_OVERLAP_BYTES]
                    cut = _redaction_safe_cut(window, redact)
                    await deliver(carry[pos : pos + cut], truncated=True)
                    pos += cut
                carry = carry[pos:]
                scanned = len(carry)
        if carry:
            await deliver(carry.rstrip(b"\r"))

    stdin_task = asyncio.create_task(feed_stdin())
    pumps = [
        asyncio.create_task(pump("stdout", proc.stdout)),
        asyncio.create_task(pump("stderr", proc.stderr)),
    ]
    wait_task = asyncio.create_task(proc.wait())
    cancel_task = asyncio.create_task(ctx.cancel.wait())
    stopped: FailureCategory | None = None
    try:
        done, _ = await asyncio.wait({wait_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in done and not wait_task.done():
            stopped = ctx.cancel.reason or FailureCategory.CANCELLED
            _kill_group(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=grace)
            except TimeoutError:
                _kill_group(proc.pid, signal.SIGKILL)
                await wait_task
        # Kill any lingering descendants still holding our pipes (the leader has exited).
        _kill_group(proc.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(asyncio.gather(*pumps), timeout=DRAIN_TIMEOUT_SECONDS)
        except TimeoutError:
            for task in pumps:
                task.cancel()
    finally:
        cancel_task.cancel()
        stdin_task.cancel()
        if not wait_task.done():
            _kill_group(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(wait_task, timeout=grace)
        for task in pumps:
            if not task.done():
                task.cancel()
        for log in logs.values():
            log.close()

    return ProcessResult(
        exit_code=proc.returncode,
        pid=proc.pid,
        duration_ms=int((time.monotonic() - started) * 1000),
        stdout_bytes=counts["stdout"],
        stderr_bytes=counts["stderr"],
        stopped_reason=stopped,
    )
