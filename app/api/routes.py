"""REST API routes."""

import json
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.config import get_settings
from app.schemas import (
    CtbOptions,
    JobStatus,
    PreprocessOptions,
    TerrainJobCreate,
    TerrainJobDetail,
    TerrainJobResponse,
    TilesetInfo,
    TilesetListResponse,
)
from app.services.job_store import JobStore
from app.services.tile_publisher import PublishError, list_published_tilesets
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
    "input_path",
    "output_dir",
    "terrain_url",
    "tileset_name",
    "published",
    "error",
}


def _store() -> JobStore:
    return JobStore(get_settings())


def _job_detail_from_store(data: dict) -> TerrainJobDetail:
    return TerrainJobDetail(
        job_id=data["job_id"],
        status=JobStatus(data["status"]),
        stage=data.get("stage"),
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
    """Publish a completed job's tiles via cesium-terrain-server."""
    tileset_name = body.tileset_name if body is not None else None
    try:
        publish_completed_job(job_id, tileset_name=tileset_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PublishError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    data = _store().get(job_id)
    assert data is not None
    return _job_detail_from_store(data)


@router.delete("/jobs/{job_id}/publish", response_model=TerrainJobDetail)
async def unpublish_job(job_id: str) -> TerrainJobDetail:
    """Remove a job's published tileset registration."""
    try:
        unpublish_completed_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PublishError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = _store().get(job_id)
    assert data is not None
    return _job_detail_from_store(data)


@router.get("/tilesets", response_model=TilesetListResponse)
async def list_tilesets() -> TilesetListResponse:
    """List tilesets registered for cesium-terrain-server."""
    settings = get_settings()
    names = list_published_tilesets(settings.tilesets_dir)
    tilesets = [
        TilesetInfo(name=name, terrain_url=settings.terrain_url_for(name))
        for name in names
    ]
    return TilesetListResponse(tilesets=tilesets)
