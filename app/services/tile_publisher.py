"""Register completed tilesets for nginx terrain-server."""

import logging
import os
import shutil
from pathlib import Path

from app.schemas import OutputFormat, Profile
from app.services.layer_json import LayerJsonError, ensure_layer_json

logger = logging.getLogger(__name__)

# OpenAPI/Swagger UI placeholder values that must not be used as tileset names.
_SWAGGER_PLACEHOLDERS = frozenset({"string", "example", "uuid", "name"})


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
