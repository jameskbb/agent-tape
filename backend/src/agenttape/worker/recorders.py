"""Context services the executor hands to adapters: artifact writer and file-change recorder."""

from __future__ import annotations

import asyncio
from typing import Any

from agenttape.adapters.base import EventEmitter, FileChangeDraft
from agenttape.application.context import AppContext
from agenttape.application.services.artifacts import ArtifactService
from agenttape.domain.enums import EventType
from agenttape.infrastructure.db.models import FileChange
from agenttape.security.redaction import Redactor


class DbArtifactWriter:
    def __init__(
        self,
        ctx: AppContext,
        *,
        run_id: str,
        attempt_id: str | None,
        step_run_id: str | None,
        redactor: Redactor,
        events: EventEmitter,
    ) -> None:
        self._ctx = ctx
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._step_run_id = step_run_id
        self._redactor = redactor
        self._events = events

    async def write_text(
        self,
        name: str,
        text: str,
        *,
        kind: str = "file",
        media_type: str = "text/plain; charset=utf-8",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        return await self.write_bytes(
            name, text.encode("utf-8"), kind=kind, media_type=media_type, metadata=metadata
        )

    async def write_bytes(
        self,
        name: str,
        data: bytes,
        *,
        kind: str = "file",
        media_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        def _store() -> tuple[str, int, str, str]:
            with self._ctx.session() as session:
                art = ArtifactService(self._ctx).store(
                    session,
                    run_id=self._run_id,
                    attempt_id=self._attempt_id,
                    step_run_id=self._step_run_id,
                    name=name,
                    data=data,
                    kind=kind,
                    media_type=media_type,
                    metadata=metadata,
                    redactor=self._redactor,
                )
                return art.id, art.size_bytes, art.sha256, art.media_type

        artifact_id, size, sha, mt = await asyncio.to_thread(_store)
        await self._events.event(
            EventType.ARTIFACT,
            f"Artifact saved: {name}",
            {
                "artifact_id": artifact_id,
                "name": name,
                "kind": kind,
                "size_bytes": size,
                "sha256": sha,
                "media_type": mt,
            },
        )
        return artifact_id


class DbFileChangeRecorder:
    def __init__(
        self,
        ctx: AppContext,
        *,
        run_id: str,
        attempt_id: str | None,
        step_run_id: str | None,
        events: EventEmitter,
    ) -> None:
        self._ctx = ctx
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._step_run_id = step_run_id
        self._events = events

    async def record(self, changes: list[FileChangeDraft]) -> None:
        if not changes:
            return

        def _insert() -> None:
            with self._ctx.session() as session:
                now = self._ctx.clock.now()
                for c in changes:
                    session.add(
                        FileChange(
                            run_id=self._run_id,
                            attempt_id=self._attempt_id,
                            step_run_id=self._step_run_id,
                            path=c.path,
                            old_path=c.old_path,
                            change_type=c.change_type,
                            insertions=c.insertions,
                            deletions=c.deletions,
                            is_binary=c.is_binary,
                            size_bytes=c.size_bytes,
                            is_protected=c.is_protected,
                            diff_artifact_id=c.diff_artifact_id,
                            metadata_=c.metadata,
                            created_at=now,
                        )
                    )

        await asyncio.to_thread(_insert)
        ins = sum(c.insertions or 0 for c in changes)
        dels = sum(c.deletions or 0 for c in changes)
        await self._events.event(
            EventType.GIT_CHANGE,
            f"{len(changes)} file{'s' if len(changes) != 1 else ''} changed (+{ins} -{dels})",
            {
                "files": [
                    {
                        "path": c.path,
                        "change_type": c.change_type,
                        "insertions": c.insertions,
                        "deletions": c.deletions,
                        "is_binary": c.is_binary,
                        "is_protected": c.is_protected,
                    }
                    for c in changes[:200]
                ],
                "total_files": len(changes),
                "insertions": ins,
                "deletions": dels,
            },
        )
