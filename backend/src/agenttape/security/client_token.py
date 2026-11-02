"""Reads a capture client's plaintext token copy (``<data_dir>/capture/<name>-client.token``).

Standard library only, on purpose: the SessionStart hook relay (``cli/hook_relay.py``)
imports this at every session start, so it must stay cheap to import and must not pull
in AgentTape's settings, pydantic, or anything else that could fail at import time.
``security/capture.py`` (``CaptureTokenStore``) writes the copy and delegates its own
reads here.
"""

from __future__ import annotations

from pathlib import Path


def client_token_path(data_dir: Path, name: str) -> Path:
    return data_dir / "capture" / f"{name}-client.token"


def read_client_token(data_dir: Path, name: str) -> str | None:
    """``name``'s client copy, else ``None``. Creates nothing: a client on a machine with
    no AgentTape data dir must not make one."""
    try:
        text = client_token_path(data_dir, name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None
