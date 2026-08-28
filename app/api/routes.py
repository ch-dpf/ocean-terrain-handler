"""REST API routes."""

import json
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.config import get_settings
from app.schemas import (
    CtbOptions,
    DiskPublishRequest,
    JobProgress,
    JobStatus,
    PreprocessOptions,
    TerrainJobCreate,
    TerrainJobDetail,
    TerrainJobResponse,
    TilesetInfo,
    TilesetListResponse,
    WorkspaceEntryInfo,
    WorkspaceListResponse,
)
from app.services.job_progress import compute_elapsed_seconds
from app.services.job_store import JobStore
from app.services.layer_json import read_layer_metadata
from app.services.tile_publisher import (
    PublishError,
    list_published_tilesets,
    publish_from_disk,
    unpublish_tileset,
)
from app.services.workspace_browser import WorkspacePathError, list_workspace
from app.worker.tasks import (
    create_job_from_path,
    create_job_from_upload,
    publish_completed_job,
    unpublish_completed_job,
)

router = APIRouter(prefix="/api/v1/terrain", tags=["terrain"])

_JOB_DETAIL_FIELDS = {
    "job_id",
    "status",
    "stage",
    "progress",
    "created_at",
    "completed_at",
    "elapsed_seconds",
    "input_path",
    "output_dir",
    "terrain_url",
    "tileset_name",
    "published",
    "error",
}


def _store() -> JobStore:
    return JobStore(get_settings())


def _progress_from_store(data: dict) -> JobProgress | None:
    raw = data.get("progress")
    if not raw:
        return None
    return JobProgress.model_validate(raw)


def _job_detail_from_store(data: dict) -> TerrainJobDetail:
    return TerrainJobDetail(
        job_id=data["job_id"],
        status=JobStatus(data["status"]),
        progress=_progress_from_store(data),
        stage=data.get("stage"),
        created_at=data.get("created_at"),
        completed_at=data.get("completed_at"),
        elapsed_seconds=compute_elapsed_seconds(data),
        input_path=data.get("input_path"),
        output_dir=data.get("output_dir"),
        terrain_url=data.get("terrain_url"),
        tileset_name=data.get("tileset_name"),
        published=bool(data.get("published")),
        error=data.get("error"),
        metadata={
            key: value
            for key, value in data.items()
            if key not in _JOB_DETAIL_FIELDS | {"request"}
        },
    )


class ManualPublishRequest(BaseModel):
    tileset_name: str | None = Field(
        default=None,
        description="Override tileset name; omit to use job_id",
    )


@router.post("/jobs", response_model=TerrainJobResponse)
async def create_job(request: TerrainJobCreate) -> TerrainJobResponse:
    """Submit a tiling job for an existing file in the workspace."""
    try:
        job_id = create_job_from_path(request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return TerrainJobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        progress_url=f"/api/v1/terrain/jobs/{job_id}",
        message="Job queued",
    )


@router.post("/jobs/upload", response_model=TerrainJobResponse)
async def create_job_with_upload(
    file: UploadFile = File(...),
    preprocess_json: str | None = Form(default=None),
    ctb_options_json: str | None = Form(default=None),
    publish_json: str | None = Form(default=None),
) -> TerrainJobResponse:
    """Upload a TIF and submit a tiling job."""
    settings = get_settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".tif", ".tiff", ".dem", ".img"}:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    request = TerrainJobCreate()
    if preprocess_json:
        request.preprocess = PreprocessOptions.model_validate(json.loads(preprocess_json))
    if ctb_options_json:
        request.ctb_options = CtbOptions.model_validate(json.loads(ctb_options_json))
    if publish_json:
        from app.schemas import PublishOptions

        request.publish = PublishOptions.model_validate(json.loads(publish_json))

    temp_path = settings.uploads_dir / f"{uuid4()}{suffix}"
    with temp_path.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    try:
        job_id = create_job_from_upload(temp_path, request)
    finally:
        temp_path.unlink(missing_ok=True)

    return TerrainJobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        progress_url=f"/api/v1/terrain/jobs/{job_id}",
        message="Upload received, job queued",
    )


@router.get("/jobs/{job_id}", response_model=TerrainJobDetail)
async def get_job(job_id: str) -> TerrainJobDetail:
    """Get job status and result paths."""
    data = _store().get(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return _job_detail_from_store(data)


@router.post("/jobs/{job_id}/publish", response_model=TerrainJobDetail)
async def publish_job(
    job_id: str,
    body: ManualPublishRequest | None = Body(default=None),
) -> TerrainJobDetail:
    """Publish a completed job's tiles via terrain-server (nginx).

    If Redis job metadata has expired, publishes from disk at jobs/{job_id}/tiles/.
    """
    tileset_name = body.tileset_name if body is not None else None
    try:
        terrain_url, resolved_name = publish_completed_job(job_id, tileset_name=tileset_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PublishError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail.lower() else 500
        raise HTTPException(status_code=status, detail=detail) from exc

    data = _store().get(job_id)
    if data is not None:
        return _job_detail_from_store(data)

    settings = get_settings()
    return TerrainJobDetail(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        stage="done",
        output_dir=str(settings.jobs_dir / job_id / "tiles"),
        terrain_url=terrain_url,
        tileset_name=resolved_name,
        published=True,
    )


@router.delete("/jobs/{job_id}/publish", response_model=TerrainJobDetail)
async def unpublish_job(job_id: str) -> TerrainJobDetail:
    """Remove a job's published tileset registration.

    If Redis metadata is gone, removes the symlink named after job_id when present.
    """
    try:
        unpublish_completed_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PublishError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = _store().get(job_id)
    if data is not None:
        return _job_detail_from_store(data)

    return TerrainJobDetail(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        published=False,
    )


@router.get("/tilesets", response_model=TilesetListResponse)
async def list_tilesets() -> TilesetListResponse:
    """List tilesets registered for terrain-server (nginx)."""
    settings = get_settings()
    names = list_published_tilesets(settings.tilesets_dir)
    tilesets = [_tileset_info_from_name(name, settings) for name in names]
    return TilesetListResponse(tilesets=tilesets)


@router.post("/tilesets/publish", response_model=TilesetInfo)
async def publish_tileset_from_disk(body: DiskPublishRequest) -> TilesetInfo:
    """Publish tiles from disk without requiring Redis job metadata.

    Provide either ``job_id`` (uses ``jobs/{job_id}/tiles/``) or ``tiles_dir``.
    Metadata is inferred from existing ``layer.json`` when available.
    """
    settings = get_settings()
    try:
        terrain_url, name, tiles_dir = publish_from_disk(
            jobs_dir=settings.jobs_dir,
            workspace_dir=settings.workspace_dir,
            tilesets_dir=settings.tilesets_dir,
            public_url=settings.terrain_server_public_url,
            base_path=settings.terrain_base_path,
            job_id=body.job_id,
            tiles_dir=body.tiles_dir,
            tileset_name=body.tileset_name,
            output_format=body.output_format,
            profile=body.profile,
        )
    except PublishError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status, detail=detail) from exc

    linked_job_id = body.job_id
    if linked_job_id is None:
        from app.services.tile_publisher import infer_job_id_from_tiles_dir

        linked_job_id = infer_job_id_from_tiles_dir(tiles_dir)

    if linked_job_id:
        store = _store()
        data = store.get(linked_job_id)
        if data is not None:
            store.update(
                linked_job_id,
                status=JobStatus.COMPLETED.value,
                terrain_url=terrain_url,
                tileset_name=name,
                published=True,
                stage="done",
                output_dir=str(tiles_dir),
            )

    return _tileset_info_from_name(name, settings)


@router.delete("/tilesets/{tileset_name}", response_model=TilesetInfo)
async def unpublish_tileset_by_name(tileset_name: str) -> TilesetInfo:
    """Unpublish a tileset by name without requiring Redis job metadata."""
    settings = get_settings()
    info = _tileset_info_from_name(tileset_name, settings)
    try:
        unpublish_tileset(settings.tilesets_dir, tileset_name)
    except PublishError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return info


def _tileset_info_from_name(name: str, settings) -> TilesetInfo:
    meta = read_layer_metadata(settings.tilesets_dir / name)
    return TilesetInfo(
        name=name,
        terrain_url=settings.terrain_url_for(name),
        format=meta["format"],
        format_label=meta["format_label"],
        projection=meta["projection"],
        crs=meta["crs"],
        min_zoom=meta["min_zoom"],
        max_zoom=meta["max_zoom"],
    )


@router.get("/workspace", response_model=WorkspaceListResponse)
async def list_workspace_entries(
    path: str = Query(default="", description="Directory path relative to workspace root"),
) -> WorkspaceListResponse:
    """List directories and selectable DEM files in the workspace."""
    settings = get_settings()
    try:
        listing = list_workspace(settings.workspace_dir, path)
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return WorkspaceListResponse(
        relative_path=listing.relative_path,
        absolute_path=listing.absolute_path,
        parent_relative_path=listing.parent_relative_path,
        entries=[
            WorkspaceEntryInfo(
                name=entry.name,
                relative_path=entry.relative_path,
                absolute_path=entry.absolute_path,
                entry_type=entry.entry_type,
                size_bytes=entry.size_bytes,
                selectable=entry.selectable,
            )
            for entry in listing.entries
        ],
    )
