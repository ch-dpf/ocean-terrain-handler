"""Generate or validate Cesium terrain layer.json from tile directories."""

import json
import logging
from pathlib import Path
from typing import TypedDict

from app.schemas import OutputFormat, Profile

logger = logging.getLogger(__name__)

LAYER_JSON = "layer.json"

FORMAT_MAP = {
    OutputFormat.TERRAIN: "heightmap-1.0",
    OutputFormat.MESH: "quantized-mesh-1.0",
}


class LayerJsonError(RuntimeError):
    pass


class LayerDisplayMeta(TypedDict):
    format: str | None
    format_label: str | None
    projection: str | None
    crs: str | None
    min_zoom: int | None
    max_zoom: int | None


def scan_tile_extents(tiles_dir: Path) -> dict[int, tuple[int, int, int, int]]:
    """Scan {z}/{x}/{y}.terrain layout and return zoom -> (startX, startY, endX, endY)."""
    levels: dict[int, tuple[int, int, int, int]] = {}

    if not tiles_dir.is_dir():
        return levels

    for z_path in sorted(tiles_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1):
        if not z_path.is_dir() or not z_path.name.isdigit():
            continue

        zoom = int(z_path.name)
        xs: list[int] = []
        ys: list[int] = []

        for x_path in z_path.iterdir():
            if not x_path.is_dir() or not x_path.name.isdigit():
                continue
            x = int(x_path.name)
            for tile_file in x_path.iterdir():
                if tile_file.suffix == ".terrain" and tile_file.stem.isdigit():
                    xs.append(x)
                    ys.append(int(tile_file.stem))

        if xs and ys:
            levels[zoom] = (min(xs), min(ys), max(xs), max(ys))

    return levels


def build_available(levels: dict[int, tuple[int, int, int, int]]) -> list[list[dict[str, int]]]:
    """Build Cesium ``available`` array indexed by zoom level."""
    if not levels:
        return []

    max_zoom = max(levels)
    available: list[list[dict[str, int]]] = []

    for zoom in range(max_zoom + 1):
        if zoom in levels:
            start_x, start_y, end_x, end_y = levels[zoom]
            available.append(
                [
                    {
                        "startX": start_x,
                        "startY": start_y,
                        "endX": end_x,
                        "endY": end_y,
                    }
                ]
            )
        else:
            available.append([])

    return available


def build_layer_json(
    tiles_dir: Path,
    output_format: OutputFormat,
    profile: Profile,
) -> dict:
    """Build layer.json content from scanned tiles."""
    levels = scan_tile_extents(tiles_dir)
    if not levels:
        raise LayerJsonError(f"No terrain tiles found under {tiles_dir}")

    scheme = "tms" if profile == Profile.GEODETIC else "tms"
    projection = "EPSG:4326" if profile == Profile.GEODETIC else "EPSG:3857"

    return {
        "tilejson": "2.1.0",
        "format": FORMAT_MAP[output_format],
        "version": "1.0.0",
        "scheme": scheme,
        "tiles": ["{z}/{x}/{y}.terrain?v={version}"],
        "projection": projection,
        "bounds": [-180.0, -90.0, 180.0, 90.0],
        "available": build_available(levels),
    }


def ensure_layer_json(
    tiles_dir: Path,
    output_format: OutputFormat,
    profile: Profile,
) -> Path:
    """Ensure layer.json exists in tiles_dir; generate if missing."""
    layer_path = tiles_dir / LAYER_JSON

    if layer_path.is_file():
        try:
            data = json.loads(layer_path.read_text(encoding="utf-8"))
            if data.get("available"):
                logger.info("Using existing layer.json at %s", layer_path)
                return layer_path
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Invalid layer.json at %s, regenerating: %s", layer_path, exc)

    content = build_layer_json(tiles_dir, output_format, profile)
    layer_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    logger.info("Wrote layer.json to %s", layer_path)
    return layer_path


def format_label_for_format(fmt: str | None) -> str | None:
    """Return a short display label for a Cesium terrain format string."""
    if not fmt:
        return None
    normalized = fmt.strip().lower()
    if normalized.startswith("quantized-mesh"):
        return "量化网格 (Mesh)"
    if normalized.startswith("heightmap"):
        return "高度图 (Terrain)"
    return fmt


def crs_label_for_projection(projection: str | None) -> str | None:
    """Return a human-readable CRS label for a layer.json projection code."""
    if not projection:
        return None
    code = projection.strip().upper()
    if code in {"EPSG:4326", "WGS84"}:
        return "EPSG:4326 (WGS84 / 地理)"
    if code in {"EPSG:3857", "EPSG:900913"}:
        return "EPSG:3857 (Web 墨卡托)"
    return projection


def zoom_range_from_available(available: object) -> tuple[int | None, int | None]:
    """Derive min/max zoom from a Cesium ``available`` array."""
    if not isinstance(available, list) or not available:
        return None, None

    zooms: list[int] = []
    for zoom, ranges in enumerate(available):
        if isinstance(ranges, list) and ranges:
            zooms.append(zoom)

    if not zooms:
        return None, None
    return min(zooms), max(zooms)


def read_layer_metadata(tiles_dir: Path) -> LayerDisplayMeta:
    """Read display metadata from ``layer.json`` under a published tileset directory.

    Missing or unreadable files yield None values without raising.
    """
    empty: LayerDisplayMeta = {
        "format": None,
        "format_label": None,
        "projection": None,
        "crs": None,
        "min_zoom": None,
        "max_zoom": None,
    }

    layer_path = tiles_dir / LAYER_JSON
    if not layer_path.is_file():
        return empty

    try:
        data = json.loads(layer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty

    if not isinstance(data, dict):
        return empty

    fmt = data.get("format")
    fmt_str = fmt if isinstance(fmt, str) else None
    projection = data.get("projection")
    projection_str = projection if isinstance(projection, str) else None
    min_zoom, max_zoom = zoom_range_from_available(data.get("available"))

    return {
        "format": fmt_str,
        "format_label": format_label_for_format(fmt_str),
        "projection": projection_str,
        "crs": crs_label_for_projection(projection_str),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
    }
