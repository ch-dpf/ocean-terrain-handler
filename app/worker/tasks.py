"""Celery tasks for terrain processing pipeline."""

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.config import get_settings
from app.schemas import JobProgress, JobStatus, TerrainJobCreate
from app.services.byte_progress import ByteBudget, fraction_to_bytes, plan_pipeline_bytes
from app.services.ctb.tiler import CtbError, run_ctb_tile
from app.services.job_progress import (
    JobProgressTracker,
    ThrottledProgressWriter,
    parse_zoom_level,
    progress_to_store_fields,
)
from app.services.job_store import JobStore
from app.services.preprocessor import PreprocessError, preprocess_dem
from app.services.raster.errors import RasterError
from app.services.provenance import (
    build_source_info,
    update_manifest,
    write_manifest_completed,
    write_manifest_created,
    write_manifest_failed,
)
from app.services.tile_publisher import PublishError, publish_tileset
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def _store() -> JobStore:
    return JobStore(get_settings())


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _JobProgressReporter:
    def __init__(self, job_id: str, budget: ByteBudget) -> None:
        self._job_id = job_id
        self._store = _store()
        self.budget = budget
        self.tracker = JobProgressTracker(
            bytes_planned=budget.total,
            weight_source="bytes",
        )
        self._writer = ThrottledProgressWriter(self._persist)

    def _persist(self, progress: JobProgress) -> None:
        self._store.update(self._job_id, **progress_to_store_fields(progress))

    def begin_stage(
        self,
        stage: str,
        *,
        status: JobStatus,
        message: str | None = None,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
    ) -> None:
        progress = self.tracker.set_stage(
            stage,
            message=message,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
        self._store.update(
            self._job_id,
            status=status.value,
            stage=stage,
            **progress_to_store_fields(progress),
        )
        self._writer.emit(progress, force=True)

    def set_bytes_done(
        self,
        done: int,
        *,
        message: str | None = None,
        current_zoom: int | None = None,
    ) -> None:
        progress = self.tracker.set_bytes_done(
            done,
            message=message,
            current_zoom=current_zoom,
        )
        self._writer.emit(progress)

    def complete(self, *, message: str = "Done") -> None:
        self.tracker.set_bytes_done(self.budget.total, message=message)
        progress = self.tracker.set_stage("done", message=message)
        self._writer.emit(progress, force=True)


def _should_auto_publish(request: TerrainJobCreate, settings) -> bool:
    if request.publish.auto_publish is not None:
        return request.publish.auto_publish
    return settings.auto_publish


def _publish_job_tileset(
    job_id: str,
    output_dir: Path,
    request: TerrainJobCreate,
    settings,
    reporter: _JobProgressReporter | None = None,
) -> tuple[str, str]:
    store = _store()
    if reporter is not None:
        reporter.begin_stage(
            "register_tileset",
            status=JobStatus.PUBLISHING,
            message="Registering tileset",
        )
    else:
        store.update(job_id, status=JobStatus.PUBLISHING.value, stage="register_tileset")

    terrain_url, tileset_name = publish_tileset(
        job_id=job_id,
        tiles_dir=output_dir,
        tilesets_dir=settings.tilesets_dir,
        public_url=settings.terrain_server_public_url,
        base_path=settings.terrain_base_path,
        output_format=request.ctb_options.output_format,
        profile=request.ctb_options.profile,
        tileset_name=request.publish.tileset_name,
    )
    return terrain_url, tileset_name


@celery_app.task(bind=True, name="terrain.process_job")
def process_terrain_job(self, job_id: str, request_data: dict) -> dict:
    settings = get_settings()
    store = _store()
    request = TerrainJobCreate.model_validate(request_data)

    job_dir = settings.jobs_dir / job_id
    preprocess_dir = job_dir / "preprocess"
    output_dir = job_dir / "tiles"
    auto_publish = _should_auto_publish(request, settings)
    reporter: _JobProgressReporter | None = None

    try:
        if not request.input_path:
            raise ValueError("input_path is required for background processing")

        input_path = Path(request.input_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        cache_bytes = max(int(settings.gdal_cachemax or 64), 1) * 1024 * 1024
        budget = plan_pipeline_bytes(
            input_path,
            request.preprocess,
            request.ctb_options,
            cache_bytes=cache_bytes,
        )
        reporter = _JobProgressReporter(job_id, budget)

        reporter.begin_stage(
            "initializing",
            status=JobStatus.RUNNING,
            message="Initializing job",
        )

        update_manifest(
            job_dir,
            status="running",
            source=build_source_info(input_path, compute_hash=True),
            output_dir=str(output_dir),
        )

        reporter.begin_stage(
            "gdal_preprocess",
            status=JobStatus.PREPROCESSING,
            message="Running raster preprocess",
        )

        def _preprocess_progress(sub_percent: float, message: str | None) -> None:
            reporter.set_bytes_done(
                fraction_to_bytes(budget.preprocess, sub_percent),
                message=message,
            )

        preprocessed = preprocess_dem(
            input_path=input_path,
            work_dir=preprocess_dir,
            options=request.preprocess,
            gdal_cachemax=settings.gdal_cachemax,
            on_subprogress=_preprocess_progress,
        )

        ctb = request.ctb_options
        min_zoom = ctb.end_zoom
        max_zoom = ctb.start_zoom
        reporter.begin_stage(
            "ctb_tile",
            status=JobStatus.TILING,
            message="Generating terrain tiles",
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )

        def _tile_progress(sub_percent: float, message: str | None) -> None:
            current_zoom = parse_zoom_level(message) if message else None
            reporter.set_bytes_done(
                budget.preprocess + fraction_to_bytes(budget.tiles, sub_percent),
                message=message or "Generating terrain tiles",
                current_zoom=current_zoom,
            )

        run_ctb_tile(
            input_path=preprocessed,
            output_dir=output_dir,
            options=request.ctb_options,
            gdal_cachemax=settings.gdal_cachemax,
            on_subprogress=_tile_progress,
        )

        result: dict[str, str | bool] = {
            "job_id": job_id,
            "status": JobStatus.COMPLETED.value,
            "output_dir": str(output_dir),
            "published": False,
        }

        completed_at = _utc_now_iso()
        if auto_publish:
            terrain_url, tileset_name = _publish_job_tileset(
                job_id, output_dir, request, settings, reporter=reporter
            )
            store.update(
                job_id,
                status=JobStatus.COMPLETED.value,
                stage="done",
                output_dir=str(output_dir),
                terrain_url=terrain_url,
                tileset_name=tileset_name,
                published=True,
                error=None,
                completed_at=completed_at,
            )
            write_manifest_completed(
                job_dir,
                output_dir=output_dir,
                published=True,
                tileset_name=tileset_name,
                terrain_url=terrain_url,
                completed_at=completed_at,
            )
            reporter.complete(message="Completed and published")
            result.update(
                {
                    "terrain_url": terrain_url,
                    "tileset_name": tileset_name,
                    "published": True,
                }
            )
        else:
            store.update(
                job_id,
                status=JobStatus.COMPLETED.value,
                stage="done",
                output_dir=str(output_dir),
                published=False,
                error=None,
                completed_at=completed_at,
            )
            write_manifest_completed(
                job_dir,
                output_dir=output_dir,
                published=False,
                completed_at=completed_at,
            )
            reporter.complete(message="Completed")

        return result

    except (PreprocessError, RasterError, CtbError, PublishError, OSError, ValueError) as exc:
        logger.exception("Job %s failed", job_id)
        failed_fields: dict = {
            "status": JobStatus.FAILED.value,
            "stage": "failed",
            "error": str(exc),
            "completed_at": _utc_now_iso(),
        }
        if reporter is not None:
            failed_progress = reporter.tracker.snapshot()
            failed_progress.message = str(exc)
            failed_progress.phase = "failed"
            failed_fields.update(progress_to_store_fields(failed_progress))
        failed_at = _utc_now_iso()
        failed_fields["completed_at"] = failed_at
        store.update(job_id, **failed_fields)
        write_manifest_failed(job_dir, error=str(exc), completed_at=failed_at)
        raise


def publish_completed_job(job_id: str, tileset_name: str | None = None) -> tuple[str, str]:
    """Publish tiles for a completed job (manual API).

    Uses Redis metadata when available; otherwise publishes from disk
    at jobs/{job_id}/tiles/ (for expired Redis TTL cases).
    """
    from app.services.tile_publisher import publish_from_disk

    settings = get_settings()
    store = _store()
    data = store.get(job_id)

    if data is None:
        terrain_url, resolved_name, _tiles_dir = publish_from_disk(
            jobs_dir=settings.jobs_dir,
            workspace_dir=settings.workspace_dir,
            tilesets_dir=settings.tilesets_dir,
            public_url=settings.terrain_server_public_url,
            base_path=settings.terrain_base_path,
            job_id=job_id,
            tileset_name=tileset_name,
        )
        return terrain_url, resolved_name

    status = data.get("status")
    allowed = {JobStatus.COMPLETED.value, JobStatus.PUBLISHING.value}
    if status not in allowed:
        raise ValueError(f"Job is not ready to publish: {status}")

    output_dir = data.get("output_dir")
    if not output_dir:
        raise ValueError("Job has no output_dir")

    request_data = data.get("request") or {}
    request = TerrainJobCreate.model_validate(request_data)
    if tileset_name is not None:
        request = request.model_copy(
            update={"publish": request.publish.model_copy(update={"tileset_name": tileset_name})}
        )

    terrain_url, resolved_name = _publish_job_tileset(
        job_id,
        Path(output_dir),
        request,
        settings,
    )
    store.update(
        job_id,
        status=JobStatus.COMPLETED.value,
        terrain_url=terrain_url,
        tileset_name=resolved_name,
        published=True,
        stage="done",
    )
    return terrain_url, resolved_name


def unpublish_completed_job(job_id: str) -> None:
    """Remove published tileset for a job.

    If Redis metadata is gone, attempts to unpublish the symlink named job_id.
    """
    from app.services.tile_publisher import unpublish_tileset

    settings = get_settings()
    store = _store()
    data = store.get(job_id)

    if data is None:
        # Redis expired: still try to remove symlink registered under job_id.
        unpublish_tileset(settings.tilesets_dir, job_id)
        return

    tileset_name = data.get("tileset_name") or job_id
    unpublish_tileset(settings.tilesets_dir, tileset_name)
    store.update(job_id, published=False, terrain_url=None, tileset_name=None)


def create_job_from_upload(
    uploaded_path: Path,
    request: TerrainJobCreate,
) -> str:
    """Persist upload and enqueue processing job."""
    settings = get_settings()
    store = _store()
    job_id = str(uuid4())

    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_dest = job_dir / "input.tif"
    shutil.copy2(uploaded_path, input_dest)

    request_with_path = request.model_copy(update={"input_path": str(input_dest)})
    output_dir = job_dir / "tiles"
    store.create(
        job_id,
        {
            "input_path": str(input_dest),
            "output_dir": str(output_dir),
            "request": request_with_path.model_dump(),
        },
    )
    write_manifest_created(
        job_dir,
        job_id=job_id,
        input_path=input_dest,
        output_dir=output_dir,
        request=request_with_path,
    )
    process_terrain_job.delay(job_id, request_with_path.model_dump())
    return job_id


def create_job_from_path(request: TerrainJobCreate) -> str:
    """Enqueue processing job for an existing workspace path."""
    settings = get_settings()
    store = _store()
    job_id = str(uuid4())

    if not request.input_path:
        raise ValueError("input_path is required")

    input_path = Path(request.input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_dir = job_dir / "tiles"
    store.create(
        job_id,
        {
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "request": request.model_dump(),
        },
    )
    write_manifest_created(
        job_dir,
        job_id=job_id,
        input_path=input_path,
        output_dir=output_dir,
        request=request,
    )
    process_terrain_job.delay(job_id, request.model_dump())
    return job_id
