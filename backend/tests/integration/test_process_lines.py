"""``run_process`` line delivery with a stream-json sized stdout line bound.

A stream-json adapter (Claude Code) passes a stdout line bound far above the plain-output
one, so a long tool result reaches its parser as one line. These tests drive
``run_process`` directly against a small child process, with a minimal execution context,
so the large-line paths are covered without a full run.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agenttape.adapters.base import CancellationToken
from agenttape.adapters.process import (
    MAX_LINE_BYTES,
    REDACTION_OVERLAP_BYTES,
    CommandSpec,
    run_process,
)
from agenttape.domain.events import EventDraft
from agenttape.security.redaction import Redactor

MIB = 1024 * 1024


class RecordingEvents:
    """The part of ``EventEmitter`` ``run_process`` uses, keeping every draft."""

    def __init__(self) -> None:
        self.drafts: list[EventDraft] = []

    async def emit(self, draft: EventDraft) -> None:
        self.drafts.append(draft)

    async def event(self, type: Any, summary: str, payload: Any = None, **_: Any) -> None:
        self.drafts.append(EventDraft(type=type, summary=summary, payload=payload or {}))

    def touch(self) -> None:
        pass


def context(tmp_path: Path, redact: Any) -> Any:
    return SimpleNamespace(
        redact=redact,
        limits={},
        attempt=SimpleNamespace(number=1),
        log_dir=tmp_path / "logs",
        events=RecordingEvents(),
        cancel=CancellationToken(),
    )


def child(tmp_path: Path, body: str) -> CommandSpec:
    script = tmp_path / "child.py"
    script.write_text(body, encoding="utf-8")
    return CommandSpec(argv=[sys.executable, str(script)])


def test_a_long_stdout_line_is_redacted_off_the_event_loop(tmp_path: Path) -> None:
    """A stdout line longer than the plain-output bound is redacted whole in a
    worker thread, so a slow redaction never stalls the loop (the cancellation watcher, the
    other pipe's pump, and sink flushes); short lines stay on the loop."""
    redactor = Redactor()
    threads: dict[int, int] = {}

    def redact(text: str) -> str:
        threads[len(text)] = threading.get_ident()
        return redactor.redact_text(text)

    size = MAX_LINE_BYTES * 4
    spec = child(
        tmp_path,
        f"import sys\nsys.stdout.write('b' * {size} + '\\n')\nsys.stdout.write('short\\n')\n",
    )
    lines: list[str] = []

    async def on_stdout(text: str) -> bool:
        lines.append(text)
        return True

    async def main() -> int:
        await run_process(
            spec, context(tmp_path, redact), on_stdout=on_stdout, max_stdout_line_bytes=8 * MIB
        )
        return threading.get_ident()

    loop_thread = asyncio.run(main())
    assert lines == ["b" * size, "short"]
    assert threads[size] != loop_thread
    assert threads[len("short")] == loop_thread


def test_the_stream_json_bound_applies_to_stdout_only(tmp_path: Path) -> None:
    """Stderr is plain text, so it keeps the plain-output bound: a long stderr
    line arrives in bounded pieces flagged truncated, while the same stdout line is whole."""
    size = MAX_LINE_BYTES * 3
    spec = child(
        tmp_path,
        f"import sys\nsys.stdout.write('o' * {size} + '\\n')\nsys.stderr.write('e' * {size} + '\\n')\n",
    )
    ctx = context(tmp_path, Redactor().redact_text)
    stdout: list[str] = []
    stderr: list[str] = []

    async def on_stdout(text: str) -> bool:
        stdout.append(text)
        return True

    async def on_stderr(text: str) -> bool:
        stderr.append(text)
        return True

    asyncio.run(
        run_process(
            spec,
            ctx,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            max_stdout_line_bytes=8 * MIB,
        )
    )
    assert stdout == ["o" * size]
    assert len(stderr) > 1
    assert "".join(stderr) == "e" * size
    assert all(len(piece) <= MAX_LINE_BYTES + REDACTION_OVERLAP_BYTES for piece in stderr)


SECRET = "Zq9-" + "sekrit-VALUE-" + "771"


@pytest.mark.slow
def test_stream_json_bound_lines_split_reassemble_and_stay_redacted(tmp_path: Path) -> None:
    """With the 8 MiB stdout bound, a 9 MiB line is buffered to the bound and
    then split into pieces that reassemble exactly, with a secret at every early split point
    (the first piece, around the 8 MiB bound, and after it) redacted; a 5 MiB CRLF line
    arrives as one delivery; a final line with no newline and a trailing CR arrives whole."""
    chunk = 64 * 1024
    long_size = 9 * MIB
    secret_at = [chunk - 5, 128 * chunk - 5, 129 * chunk - 5, 130 * chunk - 5, 140 * chunk - 5]
    body = (
        "import sys\n"
        f"secret = {SECRET!r}\n"
        f"line = bytearray(b'a' * {long_size})\n"
        f"for at in {secret_at!r}:\n"
        "    line[at : at + len(secret)] = secret.encode()\n"
        "out = sys.stdout.buffer\n"
        "out.write(bytes(line) + b'\\n')\n"
        f"out.write(b'c' * {5 * MIB} + b'\\r\\n')\n"
        "out.write(b'last line\\r')\n"
        "out.flush()\n"
    )
    ctx = context(tmp_path, Redactor([SECRET]).redact_text)
    delivered: list[str] = []

    async def on_stdout(text: str) -> bool:
        delivered.append(text)
        return True

    asyncio.run(
        run_process(child(tmp_path, body), ctx, on_stdout=on_stdout, max_stdout_line_bytes=8 * MIB)
    )

    expected = bytearray(b"a" * long_size)
    for at in secret_at:
        expected[at : at + len(SECRET)] = SECRET.encode()
    redacted = expected.decode().replace(SECRET, "[REDACTED:secret]")

    assert delivered[-1] == "last line"
    assert delivered[-2] == "c" * (5 * MIB)
    pieces = delivered[:-2]
    assert len(pieces) > 1
    assert all(len(p) <= MAX_LINE_BYTES + REDACTION_OVERLAP_BYTES for p in pieces)
    assert "".join(pieces) == redacted
    assert all(SECRET not in text for text in delivered)
    raw_log = (tmp_path / "logs" / "attempt-1.stdout.log").read_bytes()
    assert SECRET.encode() not in raw_log
