"""Artifact metadata and traversal-safe content download."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from agenttape.api.deps import Ctx
from agenttape.application.schemas.runs import ArtifactOut
from agenttape.application.services.artifacts import ArtifactService, safe_artifact_name
from agenttape.application.services.runs import RunService
from agenttape.domain.errors import NotFoundError

router = APIRouter(tags=["artifacts"])


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactOut])
def list_run_artifacts(run_id: str, ctx: Ctx) -> list[ArtifactOut]:
    RunService(ctx).get_run(run_id)
    return ArtifactService(ctx).list_for_run(run_id)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut)
def get_artifact(artifact_id: str, ctx: Ctx) -> ArtifactOut:
    return ArtifactService(ctx).get(artifact_id)


@router.get(
    "/artifacts/{artifact_id}/content",
    summary="Artifact bytes. Paths come only from the database and are confined to the store.",
    response_class=StreamingResponse,
)
def artifact_content(artifact_id: str, ctx: Ctx, download: bool = False) -> StreamingResponse:
    meta, storage_path = ArtifactService(ctx).get_storage(artifact_id)
    path = ctx.artifact_store.resolve(storage_path)  # raises on traversal
    if not path.is_file():
        raise NotFoundError("artifact content", artifact_id)
    disposition = "attachment" if download or not meta.is_text else "inline"
    return StreamingResponse(
        ctx.artifact_store.iter_bytes(storage_path),
        media_type=meta.media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_artifact_name(meta.name)}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Length": str(meta.size_bytes),
        },
    )
