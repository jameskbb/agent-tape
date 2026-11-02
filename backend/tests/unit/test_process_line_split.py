"""Over-long output lines are split where no secret straddles the two pieces."""

from __future__ import annotations

from agenttape.adapters.process import (
    MAX_LINE_BYTES,
    REDACTION_OVERLAP_BYTES,
    _redaction_safe_cut,
)
from agenttape.security.redaction import Redactor

SECRET = "sk-live-" + "4f1c2d3e4b5a69788796a5b4c3d2e1f0"  # split so the secret scan skips it


def _pieces(data: bytes, redact: Redactor) -> list[str]:
    """Split ``data`` the way the output pump does and redact each piece on its own."""
    out: list[str] = []
    while len(data) > MAX_LINE_BYTES + REDACTION_OVERLAP_BYTES:
        cut = _redaction_safe_cut(data, redact.redact_text)
        out.append(redact.redact_text(data[:cut].decode()))
        data = data[cut:]
    out.append(redact.redact_text(data.decode()))
    return out


def test_secret_straddling_the_line_limit_is_not_split() -> None:
    redactor = Redactor([SECRET])
    for offset in (1, 10, len(SECRET) // 2, len(SECRET) - 1):
        start = MAX_LINE_BYTES - offset
        line = ("x" * start + SECRET + "y" * 100_000).encode()
        pieces = _pieces(line, redactor)
        assert len(pieces) > 1
        joined = "".join(pieces)
        assert SECRET not in joined
        assert SECRET[:10] not in joined and SECRET[-10:] not in joined


def test_line_without_secrets_is_cut_at_the_limit() -> None:
    redactor = Redactor([SECRET])
    line = b"z" * (MAX_LINE_BYTES * 3)
    assert _redaction_safe_cut(line, redactor.redact_text) == MAX_LINE_BYTES
