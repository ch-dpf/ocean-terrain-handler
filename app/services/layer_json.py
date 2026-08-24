"""Generate or validate Cesium terrain layer.json from tile directories."""

import json
import logging
from pathlib import Path

from app.schemas import OutputFormat, Profile

logger = logging.getLogger(__name__)

LAYER_JSON = "layer.json"

FORMAT_MAP = {
    OutputFormat.TERRAIN: "heightmap-1.0",
    OutputFormat.MESH: "quantized-mesh-1.0",
}


class LayerJsonError(RuntimeError):
    pass


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
