"""Loopback capture client used by the stdio MCP server.

Imports nothing from the ``mcp`` SDK: this module only knows how to reach AgentTape's own
loopback HTTP API. A proxy must never see loopback capture traffic, so the default
transport disables proxying explicitly.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from agenttape.application.schemas.capture import FindingRecordIn
from agenttape.config import load_settings
from agenttape.security.capture import CaptureTokenStore, capture_api_base

Transport = Callable[[str, bytes, Mapping[str, str]], tuple[int, bytes]]

#: Longer than the server's whole bounded wait for an interactive finding
#: (``api.routers.mcp.MCP_QUEUE_WAIT_SECONDS`` + ``MCP_WRITE_WAIT_SECONDS``), so the agent
#: hears AgentTape's own answer (recorded, not recorded, or maybe recorded) rather than a
#: client timeout that cannot say which.
_TIMEOUT_SECONDS = 30.0


class CaptureError(Exception):
    """A plain-English message the MCP tool surfaces to the agent."""


@dataclass(frozen=True)
class CaptureClientConfig:
    api_base: str
    token: str
    attempt_id: str
    run_id: str
    instance_id: str
    cwd: str

    @staticmethod
    def from_env(environ: Mapping[str, str]) -> CaptureClientConfig:
        """A per-attempt (launched) config when ``AGENTTAPE_ATTEMPT_ID`` is set; otherwise an
        interactive config: no per-attempt token, the per-machine ``mcp`` client token read
        from the data dir instead (``None`` when this machine has never run ``agenttape mcp
        register``; :meth:`CaptureClient.post_finding` refuses to call out without one)."""
        attempt_id = environ.get("AGENTTAPE_ATTEMPT_ID", "")
        if attempt_id:
            return CaptureClientConfig(
                api_base=environ.get("AGENTTAPE_API_URL", ""),
                token=environ.get("AGENTTAPE_MCP_TOKEN", ""),
                attempt_id=attempt_id,
                run_id=environ.get("AGENTTAPE_RUN_ID", ""),
                instance_id=str(uuid.uuid4()),
                cwd=str(Path.cwd()),
            )
        settings = load_settings()
        api_base = environ.get("AGENTTAPE_API_URL") or capture_api_base(settings)
        data_dir_env = environ.get("AGENTTAPE_DATA_DIR")
        data_dir = Path(data_dir_env) if data_dir_env else settings.data_dir
        token = CaptureTokenStore(data_dir).read_client_token("mcp") or ""
        return CaptureClientConfig(
            api_base=api_base,
            token=token,
            attempt_id="",
            run_id="",
            instance_id=str(uuid.uuid4()),
            cwd=str(Path.cwd()),
        )


def _default_transport(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
    opener = build_opener(ProxyHandler({}))
    request = Request(url, data=body, headers=dict(headers), method="POST")  # noqa: S310
    try:
        with opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()
    except (URLError, TimeoutError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(reason, TimeoutError):
            # The request may have reached AgentTape and been written after all.
            raise CaptureError(
                f"AgentTape did not answer within {int(_TIMEOUT_SECONDS)} seconds, so this "
                "finding may have been recorded. Recording the identical finding again within "
                "a minute is safe: AgentTape keeps one copy."
            ) from exc
        raise CaptureError(
            f"AgentTape is not reachable at {url}; the finding was not recorded. "
            "Start it with `agenttape serve`."
        ) from exc


@dataclass
class CaptureClient:
    config: CaptureClientConfig
    transport: Transport = field(default=_default_transport)

    def post_finding(self, record: FindingRecordIn) -> dict[str, Any]:
        if not self.config.token:
            raise CaptureError(
                "AgentTape's MCP server is not registered on this machine; run "
                "`agenttape mcp register`. The finding was not recorded."
            )
        url = f"{self.config.api_base}/api/v1/mcp/findings"
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
        }
        if self.config.attempt_id:
            headers["X-AgentTape-Attempt"] = self.config.attempt_id
        body = record.model_dump_json(exclude_none=True).encode("utf-8")
        status, raw = self.transport(url, body, headers)
        if status == 401:
            raise CaptureError(
                "AgentTape refused this session's capture token; the finding was not recorded."
            )
        if status == 413:
            raise CaptureError(
                f"{_error_message(raw)} Split the finding into smaller findings or quote "
                "fewer lines per citation, then try again."
            )
        if status == 422:
            raise CaptureError(f"AgentTape rejected this finding: {_error_message(raw)}")
        if status == 503:
            # AgentTape's own sentence says whether the finding was recorded; say it as is.
            raise CaptureError(_error_message(raw))
        if status == 429:
            raise CaptureError(
                f"AgentTape is rate-limiting capture requests right now: {_error_message(raw)} "
                "Wait a moment and try again."
            )
        if status >= 300:
            raise CaptureError(f"AgentTape did not record this finding: {_error_message(raw)}")
        try:
            return dict(json.loads(raw.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CaptureError("AgentTape returned an unreadable response") from exc


def _error_message(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8"))
        message = data.get("error", {}).get("message")
        if isinstance(message, str) and message:
            return message
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    return raw.decode("utf-8", errors="ignore")[:200] or "request failed"
