"""Register completed tilesets for nginx terrain-server."""

import json
import logging
import os
import re
import shutil
from pathlib import Path

from app.schemas import OutputFormat, Profile
from app.services.layer_json import LAYER_JSON, LayerJsonError, ensure_layer_json

logger = logging.getLogger(__name__)

# OpenAPI/Swagger UI placeholder values that must not be used as tileset names.
_SWAGGER_PLACEHOLDERS = frozenset({"string", "example", "uuid", "name"})
_JOB_TILES_RE = re.compile(
    r"(?:^|/)jobs/([^/]+)/tiles/?$",
    re.IGNORECASE,
)

_FORMAT_FROM_LAYER = {
    "quantized-mesh-1.0": OutputFormat.MESH,
    "heightmap-1.0": OutputFormat.TERRAIN,
}


class PublishError(RuntimeError):
    pass


def _resolve_tileset_name(job_id: str, tileset_name: str | None) -> str:
    if tileset_name is not None:
        candidate = tileset_name.strip()
        if candidate and candidate.lower() not in _SWAGGER_PLACEHOLDERS:
            name = candidate
        else:
            name = job_id
    else:
        name = job_id

    if not name:
        raise PublishError("tileset_name must not be empty")
    if "/" in name or "\\" in name or ".." in name:
        raise PublishError(f"Invalid tileset_name: {name}")
    return name


def _ensure_under_workspace(path: Path, workspace_dir: Path) -> Path:
    workspace = workspace_dir.resolve()
    target = path.resolve()
    if target != workspace and workspace not in target.parents:
        raise PublishError(f"Path is outside the workspace: {path}")
    return target


def infer_job_id_from_tiles_dir(tiles_dir: Path) -> str | None:
    """If tiles_dir looks like ``.../jobs/{job_id}/tiles``, return job_id."""
    normalized = tiles_dir.resolve().as_posix()
    match = _JOB_TILES_RE.search(normalized)
    return match.group(1) if match else None


def resolve_job_tiles_dir(jobs_dir: Path, job_id: str) -> Path:
    if "/" in job_id or "\\" in job_id or ".." in job_id:
        raise PublishError(f"Invalid job_id: {job_id}")
    tiles_dir = jobs_dir / job_id / "tiles"
    if not tiles_dir.is_dir():
        raise PublishError(f"Tiles directory not found for job: {job_id}")
    return tiles_dir.resolve()


def resolve_tiles_dir_path(
    tiles_dir: str,
    *,
    workspace_dir: Path,
    jobs_dir: Path,
) -> Path:
    _ = jobs_dir
    candidate = Path(tiles_dir)
    if candidate.is_absolute():
        resolved = _ensure_under_workspace(candidate, workspace_dir)
    else:
        resolved = _ensure_under_workspace(workspace_dir / tiles_dir, workspace_dir)
    if not resolved.is_dir():
        raise PublishError(f"Tiles directory not found: {tiles_dir}")
    return resolved


def read_layer_json_publish_hints(tiles_dir: Path) -> dict:
    """Load publish parameters from an existing layer.json if present."""
    layer_path = tiles_dir / LAYER_JSON
    if not layer_path.is_file():
        return {}

    try:
        data = json.loads(layer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(data, dict):
        return {}

    hints: dict = {}
    fmt = data.get("format")
    if isinstance(fmt, str):
        mapped = _FORMAT_FROM_LAYER.get(fmt.strip().lower())
        if mapped is not None:
            hints["output_format"] = mapped

    projection = data.get("projection")
    if isinstance(projection, str):
        code = projection.strip().upper()
        if code in {"EPSG:4326", "WGS84"}:
            hints["profile"] = Profile.GEODETIC
        elif code in {"EPSG:3857", "EPSG:900913"}:
            hints["profile"] = Profile.MERCATOR

    return hints


def _register_tileset_link(tilesets_dir: Path, name: str, tiles_dir: Path) -> Path:
    """Expose tiles_dir under tilesets_dir/name via symlink or directory junction."""
    tilesets_dir.mkdir(parents=True, exist_ok=True)
    link_path = tilesets_dir / name

    if link_path.is_symlink() or link_path.exists():
        if link_path.is_symlink():
            link_path.unlink()
        elif link_path.is_dir():
            shutil.rmtree(link_path)
        else:
            link_path.unlink()

    tiles_dir = tiles_dir.resolve()
    relative_target = os.path.relpath(tiles_dir, tilesets_dir.resolve())

    try:
        link_path.symlink_to(relative_target, target_is_directory=True)
        logger.info("Registered tileset symlink %s -> %s", link_path, relative_target)
        return link_path
    except OSError as exc:
        raise PublishError(
            f"Failed to create symlink for tileset '{name}': {exc}. "
            "Ensure the worker has permission to create symlinks."
        ) from exc


def publish_tileset(
    job_id: str,
    tiles_dir: Path,
    tilesets_dir: Path,
    public_url: str,
    base_path: str,
    output_format: OutputFormat,
    profile: Profile,
    tileset_name: str | None = None,
) -> tuple[str, str]:
    """
    Prepare layer.json and register tiles for terrain-server (nginx).

    Returns (terrain_url, resolved_tileset_name).
    """
    if not tiles_dir.is_dir():
        raise PublishError(f"Tiles directory not found: {tiles_dir}")

    name = _resolve_tileset_name(job_id, tileset_name)

    try:
        ensure_layer_json(tiles_dir, output_format, profile)
    except LayerJsonError as exc:
        raise PublishError(str(exc)) from exc

    _register_tileset_link(tilesets_dir, name, tiles_dir)

    base = public_url.rstrip("/")
    path = base_path.rstrip("/")
    terrain_url = f"{base}{path}/{name}"
    return terrain_url, name


def publish_from_disk(
    *,
    jobs_dir: Path,
    workspace_dir: Path,
    tilesets_dir: Path,
    public_url: str,
    base_path: str,
    job_id: str | None = None,
    tiles_dir: str | Path | None = None,
    tileset_name: str | None = None,
    output_format: OutputFormat | None = None,
    profile: Profile | None = None,
) -> tuple[str, str, Path]:
    """
    Publish tiles from disk without requiring Redis job metadata.

    Prefer existing layer.json hints; optional request fields override them.
    Returns (terrain_url, tileset_name, resolved_tiles_dir).
    """
    if job_id and tiles_dir:
        raise PublishError("Provide either job_id or tiles_dir, not both")
    if not job_id and not tiles_dir:
        raise PublishError("Either job_id or tiles_dir is required")

    if job_id:
        resolved_tiles = resolve_job_tiles_dir(jobs_dir, job_id)
        resolved_job_id = job_id
    else:
        resolved_tiles = resolve_tiles_dir_path(
            str(tiles_dir),
            workspace_dir=workspace_dir,
            jobs_dir=jobs_dir,
        )
        resolved_job_id = infer_job_id_from_tiles_dir(resolved_tiles) or (
            tileset_name.strip() if tileset_name and tileset_name.strip() else None
        )
        if not resolved_job_id:
            raise PublishError(
                "tileset_name is required when tiles_dir is not under jobs/{job_id}/tiles"
            )

    hints = read_layer_json_publish_hints(resolved_tiles)
    resolved_format = output_format or hints.get("output_format") or OutputFormat.MESH
    resolved_profile = profile or hints.get("profile") or Profile.GEODETIC

    terrain_url, name = publish_tileset(
        job_id=resolved_job_id,
        tiles_dir=resolved_tiles,
        tilesets_dir=tilesets_dir,
        public_url=public_url,
        base_path=base_path,
        output_format=resolved_format,
        profile=resolved_profile,
        tileset_name=tileset_name,
    )
    return terrain_url, name, resolved_tiles


def unpublish_tileset(tilesets_dir: Path, tileset_name: str) -> None:
    """Remove a registered tileset symlink."""
    name = _resolve_tileset_name(tileset_name, tileset_name)
    link_path = tilesets_dir / name

    if not link_path.exists() and not link_path.is_symlink():
        raise PublishError(f"Tileset not published: {name}")

    if link_path.is_symlink():
        link_path.unlink()
    elif link_path.is_dir():
        shutil.rmtree(link_path)
    else:
        link_path.unlink()

    logger.info("Unpublished tileset %s", name)


def list_published_tilesets(tilesets_dir: Path) -> list[str]:
    """List tileset names registered under tilesets_dir."""
    if not tilesets_dir.is_dir():
        return []

    names: list[str] = []
    for entry in sorted(tilesets_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir() or entry.is_symlink():
            names.append(entry.name)
    return names
