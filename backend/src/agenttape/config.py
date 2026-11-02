"""Runtime settings loaded from the environment (``AGENTTAPE_*`` and ``DATABASE_URL``).

Settings are immutable once loaded. Tests and end-to-end runs always pass an
explicit temporary ``data_dir``; nothing in the test suite touches ``~/.agenttape``.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_PORT = 8787


def is_loopback_host(host: str) -> bool:
    """Return True when ``host`` only accepts connections from this machine."""
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    """Process configuration. See ``.env.example`` for every variable."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTTAPE_",
        env_file=None,
        extra="ignore",
        frozen=True,
    )

    data_dir: Path = Field(default_factory=lambda: Path.home() / ".agenttape")
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL", "AGENTTAPE_DATABASE_URL"),
    )
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    allowed_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    api_token_hash: str | None = None
    require_token: bool = False
    #: Extra ``Host`` names accepted when no token is required (loopback names always are).
    #: The check blocks DNS rebinding: a website that points its own name at 127.0.0.1.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Names of environment variables whose *values* are always redacted.
    redact_env: Annotated[list[str], NoDecode] = Field(default_factory=list)
    log_level: str = "info"

    # Worker / execution defaults (profiles and projects may override per run).
    worker_concurrency: int = 2
    worker_poll_interval_seconds: float = 1.0
    lease_seconds: float = 30.0
    heartbeat_interval_seconds: float = 5.0
    default_timeout_seconds: float = 1800.0
    default_idle_timeout_seconds: float = 600.0
    #: Interactive Claude Code sessions routinely pause longer than a launched run's own
    #: 600s idle timeout, so observed runs (Claude Code hooks) get a longer default window.
    observed_idle_timeout_seconds: float = 3600.0
    default_max_attempts: int = 1
    retry_backoff_base_seconds: float = 2.0
    retry_backoff_max_seconds: float = 300.0
    cancel_grace_seconds: float = 5.0

    # Size caps (bytes).
    max_event_payload_bytes: int = 64 * 1024
    max_event_text_bytes: int = 16 * 1024
    max_raw_log_bytes: int = 50 * 1024 * 1024
    max_artifact_bytes: int = 25 * 1024 * 1024
    max_sse_message_bytes: int = 128 * 1024
    max_request_body_bytes: int = 2 * 1024 * 1024

    # Demo adapter pacing (milliseconds between simulated steps).
    demo_step_delay_ms: int = 450

    # Herdr runtime (docs/HERDR.md). Socket paths are never shown in the UI or exports.
    #: Named Herdr session to use; empty means Herdr's default session.
    herdr_session: str | None = None
    #: Explicit API socket path (overrides the session name).
    herdr_socket: Path | None = None
    #: The ``herdr`` CLI (used read-only for its version and saved machine list).
    herdr_executable: str = "herdr"
    #: Label of the machine the socket reaches ("local", or a saved Herdr machine name).
    herdr_machine: str = "local"

    @field_validator("allowed_roots", mode="before")
    @classmethod
    def _split_roots(cls, value: object) -> object:
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace(",", os.pathsep).split(os.pathsep)]
            return [Path(p).expanduser() for p in parts if p]
        return value

    @field_validator("redact_env", "allowed_hosts", mode="before")
    @classmethod
    def _split_names(cls, value: object) -> object:
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        return value

    @field_validator("data_dir", mode="after")
    @classmethod
    def _expand_data_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'agenttape.db'}"

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    @property
    def binds_beyond_loopback(self) -> bool:
        return not is_loopback_host(self.host)

    @property
    def token_required(self) -> bool:
        """Tokens are mandatory beyond loopback and optional (opt-in) on loopback."""
        return self.binds_beyond_loopback or self.require_token

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def fixtures_dir(self) -> Path:
        return self.data_dir / "fixtures"

    @property
    def workspaces_dir(self) -> Path:
        """Directory for AgentTape-owned working copies (demo clones, worktrees)."""
        return self.data_dir / "workspaces"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "api_token.sha256"

    def effective_token_hash(self) -> str | None:
        """Token hash from the environment, else from the data-dir token file."""
        if self.api_token_hash:
            return self.api_token_hash.strip()
        try:
            text = self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.artifacts_dir,
            self.logs_dir,
            self.fixtures_dir,
            self.workspaces_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        # Owner-only: the database, raw logs, artifacts, and worktrees of private
        # repositories live here. Other local accounts cannot traverse into it.
        with contextlib.suppress(OSError):
            self.data_dir.chmod(0o700)

    def accepts_host(self, host: str) -> bool:
        """Whether a request's ``Host`` (or ``Origin``) name may reach a token-less API."""
        name = host.strip().lower()
        if name.startswith("["):
            name = name[: name.find("]") + 1]
        elif name.count(":") == 1:
            name = name.split(":", 1)[0]
        if not name:
            return False
        if is_loopback_host(name) or name == self.host.lower():
            return True
        return name in {h.lower() for h in self.allowed_hosts}


def load_settings(**overrides: object) -> Settings:
    """Load settings from the environment, applying explicit overrides."""
    return Settings(**overrides)  # type: ignore[arg-type]
