"""Durable job/tileset lineage manifests for source → tiles → publish tracing."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas import JobStatus, TerrainJobCreate

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
PROVENANCE_FILENAME = "provenance.json"

_MANIFEST_STATUS_ALIASES = {
    "preprocessing": JobStatus.PREPROCESSING.value,
    "tiling": JobStatus.TILING.value,
    "publishing": JobStatus.PUBLISHING.value,
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def manifest_path(job_dir: Path) -> Path:
    return job_dir / MANIFEST_FILENAME


def provenance_path(tiles_dir: Path) -> Path:
    return tiles_dir / PROVENANCE_FILENAME


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read lineage JSON %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str | None:
    """Stream SHA-256 of a file; return None if unreadable."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        logger.warning("Failed to hash source file %s: %s", path, exc)
        return None
    return digest.hexdigest()


def build_source_info(
    input_path: str | Path | None,
    *,
    compute_hash: bool = False,
) -> dict[str, Any]:
    """Describe the source DEM path for lineage records."""
    if input_path is None:
        return {
            "input_path": None,
            "file_name": None,
            "size_bytes": None,
            "sha256": None,
        }

    path = Path(input_path)
    info: dict[str, Any] = {
        "input_path": str(path),
        "file_name": path.name,
        "size_bytes": None,
        "sha256": None,
    }
    try:
        if path.is_file():
            info["size_bytes"] = path.stat().st_size
            if compute_hash:
                info["sha256"] = file_sha256(path)
    except OSError as exc:
        logger.warning("Failed to stat source file %s: %s", path, exc)
    return info


def _request_snapshot(request: TerrainJobCreate | dict[str, Any] | None) -> dict[str, Any] | None:
    if request is None:
        return None
    if isinstance(request, TerrainJobCreate):
        return request.model_dump(mode="json")
    if isinstance(request, dict):
        return request
    return None


def write_manifest_created(
    job_dir: Path,
    *,
    job_id: str,
    input_path: str | Path | None,
    output_dir: str | Path,
    request: TerrainJobCreate | dict[str, Any] | None = None,
) -> Path:
    """Write initial ``manifest.json`` when a job is queued."""
    now = _utc_now_iso()
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "source": build_source_info(input_path, compute_hash=False),
        "output_dir": str(output_dir),
        "request": _request_snapshot(request),
        "publish": {
            "published": False,
            "tileset_name": None,
            "terrain_url": None,
            "published_at": None,
        },
        "error": None,
    }
    path = manifest_path(job_dir)
    _atomic_write_json(path, data)
    logger.info("Wrote job manifest (created) %s", path)
    return path


def update_manifest(job_dir: Path, **fields: Any) -> Path | None:
    """Merge fields into an existing manifest (or create a minimal one)."""
    path = manifest_path(job_dir)
    data = read_json(path) or {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_dir.name,
        "created_at": _utc_now_iso(),
        "source": {},
        "publish": {
            "published": False,
            "tileset_name": None,
            "terrain_url": None,
            "published_at": None,
        },
        "error": None,
    }

    for key, value in fields.items():
        if key == "source" and isinstance(value, dict):
            existing = data.get("source") if isinstance(data.get("source"), dict) else {}
            data["source"] = {**existing, **value}
        elif key == "publish" and isinstance(value, dict):
            existing = data.get("publish") if isinstance(data.get("publish"), dict) else {}
            data["publish"] = {**existing, **value}
        else:
            data[key] = value

    data["updated_at"] = _utc_now_iso()
    if "schema_version" not in data:
        data["schema_version"] = SCHEMA_VERSION

    _atomic_write_json(path, data)
    logger.info("Updated job manifest %s", path)
    return path


def write_manifest_completed(
    job_dir: Path,
    *,
    output_dir: str | Path,
    published: bool = False,
    tileset_name: str | None = None,
    terrain_url: str | None = None,
    completed_at: str | None = None,
) -> Path | None:
    """Mark manifest completed after successful tiling (and optional publish)."""
    done_at = completed_at or _utc_now_iso()
    publish_fields: dict[str, Any] = {"published": published}
    if published:
        publish_fields["tileset_name"] = tileset_name
        publish_fields["terrain_url"] = terrain_url
        publish_fields["published_at"] = done_at
    return update_manifest(
        job_dir,
        status="completed",
        completed_at=done_at,
        output_dir=str(output_dir),
        error=None,
        publish=publish_fields,
    )


def write_manifest_failed(
    job_dir: Path,
    *,
    error: str,
    completed_at: str | None = None,
) -> Path | None:
    """Mark manifest failed."""
    return update_manifest(
        job_dir,
        status="failed",
        completed_at=completed_at or _utc_now_iso(),
        error=error,
    )


def write_manifest_published(
    job_dir: Path,
    *,
    tileset_name: str,
    terrain_url: str,
    published_at: str | None = None,
) -> Path | None:
    """Record publish metadata on the job manifest."""
    when = published_at or _utc_now_iso()
    return update_manifest(
        job_dir,
        publish={
            "published": True,
            "tileset_name": tileset_name,
            "terrain_url": terrain_url,
            "published_at": when,
        },
    )


def _normalize_manifest_status(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return JobStatus.QUEUED.value
    value = raw.strip().lower()
    value = _MANIFEST_STATUS_ALIASES.get(value, value)
    try:
        return JobStatus(value).value
    except ValueError:
        return JobStatus.RUNNING.value


def _progress_from_disk_status(status: str, *, error: str | None = None) -> dict[str, Any]:
    """Synthesize a progress snapshot when only durable disk metadata remains."""
    if status == JobStatus.COMPLETED.value:
        return {
            "percent": 100.0,
            "phase": "done",
            "message": "Completed",
            "current_zoom": None,
            "min_zoom": None,
            "max_zoom": None,
        }
    if status == JobStatus.FAILED.value:
        return {
            "percent": 0.0,
            "phase": "failed",
            "message": error or "Failed",
            "current_zoom": None,
            "min_zoom": None,
            "max_zoom": None,
        }
    if status == JobStatus.QUEUED.value:
        return {
            "percent": 0.0,
            "phase": "queued",
            "message": "Queued",
            "current_zoom": None,
            "min_zoom": None,
            "max_zoom": None,
        }
    return {
        "percent": 0.0,
        "phase": status,
        "message": "Restored from disk (Redis job record expired)",
        "current_zoom": None,
        "min_zoom": None,
        "max_zoom": None,
    }


def job_dict_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Convert durable ``manifest.json`` into a JobStore-shaped document."""
    status = _normalize_manifest_status(manifest.get("status"))
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    publish = manifest.get("publish") if isinstance(manifest.get("publish"), dict) else {}
    error = manifest.get("error")
    error_text = error if isinstance(error, str) else None

    stage = {
        JobStatus.QUEUED.value: "queued",
        JobStatus.COMPLETED.value: "done",
        JobStatus.FAILED.value: "failed",
        JobStatus.PREPROCESSING.value: "gdal_preprocess",
        JobStatus.TILING.value: "ctb_tile",
        JobStatus.PUBLISHING.value: "register_tileset",
        JobStatus.RUNNING.value: "running",
    }.get(status, status)

    return {
        "job_id": str(manifest.get("job_id") or ""),
        "status": status,
        "stage": stage,
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "completed_at": manifest.get("completed_at"),
        "input_path": source.get("input_path"),
        "output_dir": manifest.get("output_dir"),
        "published": bool(publish.get("published")),
        "tileset_name": publish.get("tileset_name"),
        "terrain_url": publish.get("terrain_url"),
        "error": error_text,
        "progress": _progress_from_disk_status(status, error=error_text),
        "request": manifest.get("request"),
        "from_disk": True,
    }


def load_job_from_disk(jobs_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Load job metadata from ``jobs/{job_id}/manifest.json`` (Redis TTL fallback).

    If the manifest is missing but a tiles directory exists, synthesize a completed
    snapshot so finished jobs remain queryable after Redis expiry.
    """
    job_dir = jobs_dir / job_id
    manifest = read_json(manifest_path(job_dir))
    if manifest is not None:
        data = job_dict_from_manifest(manifest)
        if not data.get("job_id"):
            data["job_id"] = job_id
        if not data.get("output_dir"):
            data["output_dir"] = str(job_dir / "tiles")
        return data

    tiles_dir = job_dir / "tiles"
    if not tiles_dir.is_dir():
        return None

    try:
        has_tiles = any(tiles_dir.iterdir())
    except OSError:
        return None
    if not has_tiles:
        return None

    now = _utc_now_iso()
    return {
        "job_id": job_id,
        "status": JobStatus.COMPLETED.value,
        "stage": "done",
        "created_at": None,
        "updated_at": now,
        "completed_at": None,
        "input_path": None,
        "output_dir": str(tiles_dir),
        "published": False,
        "tileset_name": None,
        "terrain_url": None,
        "error": None,
        "progress": _progress_from_disk_status(JobStatus.COMPLETED.value),
        "from_disk": True,
    }


def write_tiles_provenance(
    tiles_dir: Path,
    *,
    job_id: str,
    tileset_name: str,
    terrain_url: str,
    job_dir: Path | None = None,
    published_at: str | None = None,
) -> Path:
    """Write ``provenance.json`` beside tiles so published layers remain traceable."""
    when = published_at or _utc_now_iso()
    resolved_job_dir = job_dir
    if resolved_job_dir is None:
        # Convention: .../jobs/{job_id}/tiles
        parent = tiles_dir.resolve().parent
        if parent.name == job_id or (parent / MANIFEST_FILENAME).is_file():
            resolved_job_dir = parent

    manifest: dict[str, Any] | None = None
    if resolved_job_dir is not None:
        manifest = read_json(manifest_path(resolved_job_dir))

    source = (manifest or {}).get("source") if isinstance((manifest or {}).get("source"), dict) else {}
    request = (manifest or {}).get("request")

    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "tileset_name": tileset_name,
        "terrain_url": terrain_url,
        "published_at": when,
        "tiles_dir": str(tiles_dir.resolve()),
        "source": source
        or {
            "input_path": None,
            "file_name": None,
            "size_bytes": None,
            "sha256": None,
        },
        "request": request,
        "manifest_path": str(manifest_path(resolved_job_dir)) if resolved_job_dir else None,
    }

    path = provenance_path(tiles_dir)
    _atomic_write_json(path, data)
    logger.info("Wrote tiles provenance %s", path)

    if resolved_job_dir is not None:
        write_manifest_published(
            resolved_job_dir,
            tileset_name=tileset_name,
            terrain_url=terrain_url,
            published_at=when,
        )

    return path
