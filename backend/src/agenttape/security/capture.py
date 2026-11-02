"""Loopback-only capture ingest for the MCP server and Claude Code hooks.

Tokens are hashed at rest. Nothing here touches the database: this module is used by
both the API process (to verify) and adapter/CLI code (to mint, revoke, and build the
loopback base URL) so it must have no dependency on ``AppContext`` or SQLAlchemy.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agenttape.config import Settings, is_loopback_host
from agenttape.security.client_token import client_token_path, read_client_token
from agenttape.security.tokens import generate_token, hash_token, verify_token

ATTEMPT_TOKEN_PREFIX = "atm_"  # noqa: S105 - public token prefix, not a secret
#: Per-machine capture tokens (hooks, mcp) - distinct prefix from the per-attempt token.
MACHINE_TOKEN_PREFIX = "atc_"  # noqa: S105 - public token prefix, not a secret
CAPTURE_TOKEN_NAMES = ("hooks", "mcp")
_TOKENS_FILENAME = "tokens.json"
_TOKENS_SCHEMA_VERSION = 1


def capture_api_base(settings: Settings) -> str:
    """The loopback base URL AgentTape's own capture clients use.

    Never derived from ``settings.host`` beyond checking whether it is itself an IPv6
    loopback literal: a capture client must always reach AgentTape over loopback,
    whatever host the API additionally binds to.
    """
    host = settings.host.strip().strip("[]")
    if is_loopback_host(host) and ":" in host:
        return f"http://[{host}]:{settings.port}"
    return f"http://127.0.0.1:{settings.port}"


@dataclass(frozen=True)
class CaptureRejection:
    status: int
    code: str
    message: str


def _host_name(raw: str) -> str:
    """Strip a port from ``host`` header, handling IPv6 bracket notation."""
    name = raw.strip()
    if name.startswith("["):
        return name[: name.find("]") + 1].strip("[]")
    if name.count(":") == 1:
        return name.split(":", 1)[0]
    return name


def capture_rejection(
    client_host: str | None, headers: Mapping[str, str]
) -> CaptureRejection | None:
    """Refuse a capture request before any DB access. ``headers`` keys must be lower-case."""
    if client_host is None or not is_loopback_host(client_host):
        return CaptureRejection(403, "not_loopback", "Capture requests must come from this machine")
    host_header = headers.get("host")
    if host_header and not is_loopback_host(_host_name(host_header)):
        return CaptureRejection(
            421, "untrusted_host", "This host name is not accepted for capture requests"
        )
    if headers.get("origin") is not None or headers.get("sec-fetch-site") is not None:
        return CaptureRejection(
            403, "browser_request", "Capture requests must not come from a browser page"
        )
    if any(headers.get(h) is not None for h in ("forwarded", "x-forwarded-for", "x-real-ip")):
        return CaptureRejection(
            403, "proxied_request", "Capture requests must not be proxied or forwarded"
        )
    return None


def bearer_token(headers: Mapping[str, str]) -> str | None:
    """``Authorization: Bearer <token>`` from lower-cased headers, else None."""
    auth = headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    return auth[len("bearer ") :].strip()


def capture_dir(data_dir: Path) -> Path:
    path = data_dir / "capture"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def attempts_dir(data_dir: Path) -> Path:
    path = capture_dir(data_dir) / "attempts"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def is_attempt_id(attempt_id: str) -> bool:
    """Whether ``attempt_id`` is a canonical UUID (lowercase, hyphenated, exactly what
    ``str(uuid.UUID(...))`` produces), the only shape a per-attempt file name may take;
    braces, a ``urn:uuid:`` prefix, bare hex, and upper case are refused."""
    try:
        return str(uuid.UUID(attempt_id)) == attempt_id
    except (ValueError, AttributeError, TypeError):
        return False


def mint_attempt_token(data_dir: Path, attempt_id: str) -> str:
    """Generate a per-attempt token and store its hash in a 0600 file. Never in the DB."""
    if not is_attempt_id(attempt_id):
        raise ValueError(f"attempt id {attempt_id!r} is not a canonical UUID")
    token = generate_token(ATTEMPT_TOKEN_PREFIX)
    path = attempts_dir(data_dir) / f"{attempt_id}.sha256"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(hash_token(token))
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    return token


def verify_attempt_token(data_dir: Path, attempt_id: str, token: str | None) -> bool:
    if not is_attempt_id(attempt_id):
        return False
    path = attempts_dir(data_dir) / f"{attempt_id}.sha256"
    try:
        stored = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return verify_token(token, stored)


def revoke_attempt_token(data_dir: Path, attempt_id: str) -> None:
    if not is_attempt_id(attempt_id):
        return
    path = attempts_dir(data_dir) / f"{attempt_id}.sha256"
    path.unlink(missing_ok=True)


class CaptureTokenStore:
    """Per-machine capture tokens (``hooks``, ``mcp``): hashes only, in
    ``<data_dir>/capture/tokens.json`` (0600, inside the 0700 capture dir), written
    atomically (temp file + ``os.replace``). Never the database.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    @property
    def _path(self) -> Path:
        return capture_dir(self._data_dir) / _TOKENS_FILENAME

    def _client_copy_path(self, name: str) -> Path:
        capture_dir(self._data_dir)
        return client_token_path(self._data_dir, name)

    @staticmethod
    def _check_name(name: str) -> None:
        if name not in CAPTURE_TOKEN_NAMES:
            raise ValueError(f"unknown capture token name {name!r}")

    def _load(self) -> dict[str, Any]:
        empty: dict[str, Any] = {"version": _TOKENS_SCHEMA_VERSION, "tokens": {}}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return empty
        try:
            data = json.loads(raw)
        except ValueError:
            return empty
        if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
            return empty
        return data

    def _write(self, data: dict[str, Any]) -> None:
        directory = capture_dir(self._data_dir)
        fd, tmp_name_str = tempfile.mkstemp(dir=directory, prefix=".tokens-", suffix=".tmp")
        tmp_path = Path(tmp_name_str)
        try:
            tmp_path.chmod(0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            tmp_path.replace(self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def mint(self, name: str, *, client_copy: bool = False) -> str:
        """Generate a fresh token for ``name``, replacing any previous one."""
        self._check_name(name)
        token = generate_token(MACHINE_TOKEN_PREFIX)
        data = self._load()
        data["tokens"][name] = {
            "hash": hash_token(token),
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._write(data)
        if client_copy:
            self._write_client_copy(name, token)
        else:
            self._client_copy_path(name).unlink(missing_ok=True)
        return token

    def _write_client_copy(self, name: str, token: str) -> None:
        """The plaintext token in ``<name>-client.token`` (0600, in the 0700 capture dir),
        written atomically, for a local client that must present it (the MCP server, the
        hooks relay)."""
        directory = capture_dir(self._data_dir)
        fd, tmp_name_str = tempfile.mkstemp(dir=directory, prefix=f".{name}-client-", suffix=".tmp")
        tmp_path = Path(tmp_name_str)
        try:
            tmp_path.chmod(0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
            tmp_path.replace(self._client_copy_path(name))
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def ensure_client_copy(self, name: str, token: str) -> bool:
        """Write ``token`` as ``name``'s client copy when it is ``name``'s current valid
        token and the copy on disk is missing or different. Never mints. Returns whether
        it wrote."""
        self._check_name(name)
        if not self.verify(name, token) or self.read_client_token(name) == token:
            return False
        self._write_client_copy(name, token)
        return True

    def verify(self, name: str, token: str | None) -> bool:
        self._check_name(name)
        entry = self._load()["tokens"].get(name)
        if not isinstance(entry, dict):
            return False
        return verify_token(token, entry.get("hash"))

    def revoke(self, name: str) -> bool:
        """Remove ``name``'s token (and client copy). Returns whether one existed."""
        self._check_name(name)
        data = self._load()
        existed = data["tokens"].pop(name, None) is not None
        if existed:
            self._write(data)
        self._client_copy_path(name).unlink(missing_ok=True)
        return existed

    def read_client_token(self, name: str) -> str | None:
        """``name``'s client copy, else ``None``. Creates nothing: a client on a machine
        with no AgentTape data dir must not make one."""
        self._check_name(name)
        return read_client_token(self._data_dir, name)
