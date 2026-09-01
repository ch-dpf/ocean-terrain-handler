"""REST API routes."""

import asyncio
import json
import logging
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    Body,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
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
from app.services.provenance import load_job_from_disk
from app.services.tile_publisher import (
    PublishError,
    get_tileset_display_meta,
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/terrain", tags=["地形"])

_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})

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


def _resolve_job(job_id: str) -> dict | None:
    """Resolve job metadata from Redis, falling back to durable disk lineage."""
    data = _store().get(job_id)
    if data is not None:
        return data
    return load_job_from_disk(get_settings().jobs_dir, job_id)


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


def _job_urls(job_id: str) -> tuple[str, str]:
    base = f"/api/v1/terrain/jobs/{job_id}"
    return base, f"{base}/ws"


def _job_queued_response(job_id: str, message: str) -> TerrainJobResponse:
    progress_url, progress_ws_url = _job_urls(job_id)
    return TerrainJobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        progress_url=progress_url,
        progress_ws_url=progress_ws_url,
        message=message,
    )


class ManualPublishRequest(BaseModel):
    tileset_name: str | None = Field(
        default=None,
        description="覆盖发布名称；省略则使用 job_id",
    )


@router.post("/jobs", response_model=TerrainJobResponse, summary="提交工作区文件任务")
async def create_job(request: TerrainJobCreate) -> TerrainJobResponse:
    """针对工作区内已有 DEM/TIF 文件提交切片任务。"""
    try:
        job_id = create_job_from_path(request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _job_queued_response(job_id, "Job queued")


@router.post("/jobs/upload", response_model=TerrainJobResponse, summary="上传文件并提交任务")
async def create_job_with_upload(
    file: UploadFile = File(..., description="待处理的 DEM/TIF 文件（.tif / .tiff / .dem / .img）"),
    preprocess_json: str | None = Form(
        default=None,
        description="预处理选项 JSON 字符串（对应 PreprocessOptions）",
    ),
    ctb_options_json: str | None = Form(
        default=None,
        description="CTB 切片选项 JSON 字符串（对应 CtbOptions）",
    ),
    publish_json: str | None = Form(
        default=None,
        description="发布选项 JSON 字符串（对应 PublishOptions）",
    ),
) -> TerrainJobResponse:
    """上传 TIF/DEM 文件并提交切片任务。"""
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

    return _job_queued_response(job_id, "Upload received, job queued")


@router.get("/jobs/{job_id}", response_model=TerrainJobDetail, summary="查询任务详情")
async def get_job(job_id: str) -> TerrainJobDetail:
    """获取任务状态、进度、输出路径与发布信息（REST 快照；实时进度见 WebSocket）。

    Redis 记录过期后，回退读取 ``jobs/{job_id}/manifest.json``（或已有 tiles 目录）。
    """
    data = _resolve_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return _job_detail_from_store(data)


@router.websocket("/jobs/{job_id}/ws")
async def watch_job_progress(websocket: WebSocket, job_id: str) -> None:
    """推送任务进度：连接时先发当前快照，随后订阅 Redis pub/sub。

    消息体与 ``GET /jobs/{job_id}`` 相同（``TerrainJobDetail`` JSON）。
    任务进入 ``completed`` / ``failed`` 后发送终态并关闭连接。
    Redis 过期时回退磁盘快照；无 live Redis 记录时仅发送快照后关闭。
    """
    await websocket.accept()
    store = _store()
    live = store.get(job_id)
    data = live if live is not None else load_job_from_disk(get_settings().jobs_dir, job_id)
    if data is None:
        await websocket.send_json({"detail": "Job not found"})
        await websocket.close(code=4404)
        return

    detail = _job_detail_from_store(data)
    await websocket.send_json(detail.model_dump(mode="json"))
    if detail.status in _TERMINAL_STATUSES or live is None:
        await websocket.close()
        return

    channel = store.events_channel(job_id)
    pubsub = store.redis.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(channel)
    try:
        while True:
            if websocket.client_state.name != "CONNECTED":
                return
            raw_message = await asyncio.to_thread(pubsub.get_message, True, 1.0)
            if raw_message is None or raw_message.get("type") != "message":
                continue

            payload = raw_message.get("data")
            if not isinstance(payload, str):
                continue
            try:
                event_data = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("Invalid progress event JSON for job %s", job_id)
                continue

            detail = _job_detail_from_store(event_data)
            try:
                await websocket.send_json(detail.model_dump(mode="json"))
            except (WebSocketDisconnect, RuntimeError):
                return
            if detail.status in _TERMINAL_STATUSES:
                await websocket.close()
                return
    except WebSocketDisconnect:
        return
    finally:
        try:
            pubsub.unsubscribe(channel)
            pubsub.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.debug("pubsub cleanup failed for job %s", job_id, exc_info=True)


@router.post("/jobs/{job_id}/publish", response_model=TerrainJobDetail, summary="按任务发布瓦片")
async def publish_job(
    job_id: str,
    body: ManualPublishRequest | None = Body(default=None),
) -> TerrainJobDetail:
    """通过 terrain-server（nginx）发布已完成任务的瓦片。

    若 Redis 中任务元数据已过期，则从磁盘路径 ``jobs/{job_id}/tiles/`` 发布。
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

    data = _resolve_job(job_id)
    if data is not None:
        data = {
            **data,
            "status": JobStatus.COMPLETED.value,
            "stage": "done",
            "terrain_url": terrain_url,
            "tileset_name": resolved_name,
            "published": True,
        }
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


@router.delete("/jobs/{job_id}/publish", response_model=TerrainJobDetail, summary="按任务下架瓦片")
async def unpublish_job(job_id: str) -> TerrainJobDetail:
    """移除任务对应的已发布 tileset 注册。

    若 Redis 元数据已不存在，则在存在时删除以 job_id 命名的符号链接。
    """
    try:
        unpublish_completed_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PublishError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = _resolve_job(job_id)
    if data is not None:
        data = {
            **data,
            "published": False,
            "terrain_url": None,
            "tileset_name": None,
        }
        return _job_detail_from_store(data)

    return TerrainJobDetail(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        published=False,
    )


@router.get("/tilesets", response_model=TilesetListResponse, summary="列出已发布图层")
async def list_tilesets() -> TilesetListResponse:
    """列出已在 terrain-server（nginx）注册的 tileset。

    展示元数据优先读发布旁路的 ``.{name}.layer-meta.json`` / 内存缓存，
    避免每次跟随 symlink 进入大型 tiles 目录。
    """
    settings = get_settings()

    def _load() -> list[TilesetInfo]:
        names = list_published_tilesets(settings.tilesets_dir)
        return [_tileset_info_from_name(name, settings) for name in names]

    tilesets = await asyncio.to_thread(_load)
    return TilesetListResponse(tilesets=tilesets)


@router.post("/tilesets/publish", response_model=TilesetInfo, summary="按磁盘路径发布图层")
async def publish_tileset_from_disk(body: DiskPublishRequest) -> TilesetInfo:
    """不依赖 Redis 任务元数据，直接从磁盘发布瓦片。

    需提供 ``job_id``（使用 ``jobs/{job_id}/tiles/``）或 ``tiles_dir`` 之一。
    元数据在已有 ``layer.json`` 时自动推断。
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

    return _tileset_info_from_name(name, settings, tiles_dir=tiles_dir)


@router.delete("/tilesets/{tileset_name}", response_model=TilesetInfo, summary="按名称下架图层")
async def unpublish_tileset_by_name(tileset_name: str) -> TilesetInfo:
    """按名称下架 tileset，无需 Redis 任务元数据。"""
    settings = get_settings()
    info = _tileset_info_from_name(tileset_name, settings)
    try:
        unpublish_tileset(settings.tilesets_dir, tileset_name)
    except PublishError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return info


def _tileset_info_from_name(
    name: str,
    settings,
    *,
    tiles_dir: Path | None = None,
) -> TilesetInfo:
    meta = get_tileset_display_meta(settings.tilesets_dir, name, tiles_dir=tiles_dir)
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


@router.get("/workspace", response_model=WorkspaceListResponse, summary="浏览工作区文件")
async def list_workspace_entries(
    path: str = Query(default="", description="相对工作区根目录的路径"),
) -> WorkspaceListResponse:
    """列出工作区内的目录与可选 DEM 文件（大目录在线程池中扫描，避免阻塞事件循环）。"""
    settings = get_settings()
    try:
        listing = await asyncio.to_thread(list_workspace, settings.workspace_dir, path)
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
