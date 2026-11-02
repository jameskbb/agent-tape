"""Artifacts: bytes in the artifact store, metadata (hash, size, type) in the database."""

from __future__ import annotations

import mimetypes
import re
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.schemas.runs import ArtifactOut
from agenttape.application.services.common import get_or_404
from agenttape.infrastructure.db.base import new_id
from agenttape.infrastructure.db.models import Artifact
from agenttape.security.redaction import DEFAULT_REDACTOR, Redactor

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_artifact_name(name: str) -> str:
    """Flatten a display name into a single safe path segment."""
    base = PurePosixPath(name.replace("\\", "/")).name or "artifact"
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "artifact"
    return cleaned[:120]


def is_text_media(media_type: str) -> bool:
    mt = media_type.split(";")[0].strip().lower()
    return mt.startswith("text/") or mt in {
        "application/json",
        "application/x-ndjson",
        "application/xml",
        "application/yaml",
        "application/x-diff",
    }


class ArtifactService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def store(
        self,
        session: Session,
        *,
        run_id: str,
        name: str,
        data: bytes,
        kind: str = "file",
        media_type: str | None = None,
        attempt_id: str | None = None,
        step_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        redactor: Redactor | None = None,
    ) -> Artifact:
        """Store ``data`` atomically and record it. Text artifacts are redacted first."""
        mt = media_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        text = is_text_media(mt)
        if text:
            data = (
                (redactor or DEFAULT_REDACTOR)
                .redact_text(data.decode("utf-8", errors="replace"))
                .encode("utf-8")
            )
        artifact_id = new_id()
        key = f"runs/{run_id}/{artifact_id}-{safe_artifact_name(name)}"
        stored = self.ctx.artifact_store.put_bytes(key, data)
        artifact = Artifact(
            id=artifact_id,
            run_id=run_id,
            attempt_id=attempt_id,
            step_run_id=step_run_id,
            name=name[:255],
            kind=kind,
            media_type=mt,
            storage_path=stored.storage_path,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            is_text=text,
            metadata_=metadata or {},
            created_at=self.ctx.clock.now(),
        )
        session.add(artifact)
        session.flush()
        return artifact

    def list_for_run(self, run_id: str) -> list[ArtifactOut]:
        with self.ctx.session() as session:
            rows = session.scalars(
                select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
            ).all()
            return [ArtifactOut.model_validate(r) for r in rows]

    def get(self, artifact_id: str) -> ArtifactOut:
        with self.ctx.session() as session:
            return ArtifactOut.model_validate(
                get_or_404(session, Artifact, artifact_id, "artifact")
            )

    def get_storage(self, artifact_id: str) -> tuple[ArtifactOut, str]:
        with self.ctx.session() as session:
            row = get_or_404(session, Artifact, artifact_id, "artifact")
            return ArtifactOut.model_validate(row), row.storage_path

    def read_text(self, artifact_id: str, max_bytes: int = 2 * 1024 * 1024) -> str:
        _out, path = self.get_storage(artifact_id)
        with self.ctx.artifact_store.open(path) as fh:
            data = fh.read(max_bytes)
        return data.decode("utf-8", errors="replace")
