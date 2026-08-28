"""Celery tasks for terrain processing pipeline."""

import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.config import get_settings
from app.schemas import JobProgress, JobStatus, TerrainJobCreate
from app.services.ctb_runner import CtbError, run_ctb_tile
from app.services.job_progress import (
    JobProgressTracker,
    ThrottledProgressWriter,
    parse_zoom_level,
    progress_to_store_fields,
)
from app.services.job_store import JobStore
from app.services.preprocessor import PreprocessError, preprocess_dem
from app.services.progress_calibration import ProgressCalibrationStore
from app.services.tile_publisher import PublishError, publish_tileset
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def _store() -> JobStore:
    return JobStore(get_settings())


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _JobProgressReporter:
    def __init__(self, job_id: str, *, auto_publish: bool) -> None:
        self._job_id = job_id
        self._auto_publish = auto_publish
        self._store = _store()
        settings = get_settings()
        calibration = ProgressCalibrationStore(settings, self._store.redis)
        stage_ranges, weight_source, calibration_samples = calibration.get_stage_ranges(
            auto_publish=auto_publish,
        )
        self._calibration = calibration
        self.tracker = JobProgressTracker(
            stage_ranges=stage_ranges,
            weight_source=weight_source,
            calibration_samples=calibration_samples,
        )
        self._writer = ThrottledProgressWriter(self._persist)
        self._current_stage: str | None = None
        self._stage_started_at: float | None = None
        self._stage_durations: dict[str, float] = {}

    def _persist(self, progress: JobProgress) -> None:
        self._store.update(self._job_id, **progress_to_store_fields(progress))

    def _close_current_stage(self) -> None:
        if self._current_stage is None or self._stage_started_at is None:
            return
        elapsed = time.monotonic() - self._stage_started_at
        self._stage_durations[self._current_stage] = elapsed

    def begin_stage(
        self,
        stage: str,
        *,
        status: JobStatus,
        message: str | None = None,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
    ) -> None:
        self._close_current_stage()
        self._current_stage = stage
        self._stage_started_at = time.monotonic()

        progress = self.tracker.set_stage(
            stage,
            message=message,
            sub_percent=0.0,
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

    def update_subprogress(
        self,
        sub_percent: float,
        *,
        message: str | None = None,
        current_zoom: int | None = None,
    ) -> None:
        progress = self.tracker.update_subprogress(
            sub_percent,
            message=message,
            current_zoom=current_zoom,
        )
        self._writer.emit(progress)

    def complete(self, *, message: str = "Done") -> None:
        self._close_current_stage()
        self._calibration.record_job_durations(
            self._stage_durations,
            auto_publish=self._auto_publish,
        )
        progress = self.tracker.set_stage("done", message=message, sub_percent=100.0)
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
    reporter = _JobProgressReporter(job_id, auto_publish=auto_publish)

    try:
        reporter.begin_stage(
            "initializing",
            status=JobStatus.RUNNING,
            message="Initializing job",
        )

        if not request.input_path:
            raise ValueError("input_path is required for background processing")

        input_path = Path(request.input_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        reporter.begin_stage(
            "gdal_preprocess",
            status=JobStatus.PREPROCESSING,
            message="Running GDAL preprocess",
        )
        preprocessed = preprocess_dem(
            input_path=input_path,
            work_dir=preprocess_dir,
            options=request.preprocess,
            gdal_cachemax=settings.gdal_cachemax,
            on_subprogress=lambda pct, msg: reporter.update_subprogress(pct, message=msg),
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
            reporter.update_subprogress(
                sub_percent,
                message=message or "Generating terrain tiles",
                current_zoom=current_zoom,
            )

        run_ctb_tile(
            input_path=preprocessed,
            output_dir=output_dir,
            options=request.ctb_options,
            docker_image=settings.ctb_docker_image,
            workspace_dir=settings.workspace_dir,
            gdal_cachemax=settings.gdal_cachemax,
            host_workspace_dir=settings.host_workspace_dir,
            on_subprogress=_tile_progress,
        )

        result: dict[str, str | bool] = {
            "job_id": job_id,
            "status": JobStatus.COMPLETED.value,
            "output_dir": str(output_dir),
            "published": False,
        }

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
                completed_at=_utc_now_iso(),
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
                completed_at=_utc_now_iso(),
            )
            reporter.complete(message="Completed")

        return result

    except (PreprocessError, CtbError, PublishError, OSError, ValueError) as exc:
        logger.exception("Job %s failed", job_id)
        reporter._close_current_stage()
        failed_progress = reporter.tracker.snapshot()
        failed_progress.message = str(exc)
        failed_progress.phase = "failed"
        store.update(
            job_id,
            status=JobStatus.FAILED.value,
            stage="failed",
            error=str(exc),
            completed_at=_utc_now_iso(),
            **progress_to_store_fields(failed_progress),
        )
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
    store.create(
        job_id,
        {
            "input_path": str(input_dest),
            "output_dir": str(job_dir / "tiles"),
            "request": request_with_path.model_dump(),
        },
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

    store.create(
        job_id,
        {
            "input_path": str(input_path),
            "output_dir": str(settings.jobs_dir / job_id / "tiles"),
            "request": request.model_dump(),
        },
    )
    process_terrain_job.delay(job_id, request.model_dump())
    return job_id
