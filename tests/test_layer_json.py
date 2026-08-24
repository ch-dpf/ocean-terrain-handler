"""layer.json generation tests."""

import json
from pathlib import Path

import pytest

from app.schemas import OutputFormat, Profile
from app.services.layer_json import (
    LayerJsonError,
    build_available,
    build_layer_json,
    ensure_layer_json,
    scan_tile_extents,
)


def _make_tile(tiles_dir: Path, z: int, x: int, y: int) -> None:
    tile_path = tiles_dir / str(z) / str(x)
    tile_path.mkdir(parents=True, exist_ok=True)
    (tile_path / f"{y}.terrain").write_bytes(b"\x00")


def test_scan_tile_extents_with_tiles(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)
    _make_tile(tiles_dir, 0, 1, 0)
    _make_tile(tiles_dir, 1, 2, 4)

    levels = scan_tile_extents(tiles_dir)
    assert levels[0] == (0, 0, 1, 0)
    assert levels[1] == (2, 4, 2, 4)


def test_build_available():
    available = build_available({0: (0, 0, 1, 0), 2: (4, 8, 5, 9)})
    assert len(available) == 3
    assert available[0] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
    assert available[1] == []
    assert available[2] == [{"startX": 4, "startY": 8, "endX": 5, "endY": 9}]


def test_build_layer_json_mesh(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)

    layer = build_layer_json(tiles_dir, OutputFormat.MESH, Profile.GEODETIC)
    assert layer["format"] == "quantized-mesh-1.0"
    assert layer["projection"] == "EPSG:4326"
    assert len(layer["available"]) == 1


def test_build_layer_json_empty_raises(tmp_path: Path):
    with pytest.raises(LayerJsonError):
        build_layer_json(tmp_path / "empty", OutputFormat.TERRAIN, Profile.GEODETIC)


def test_ensure_layer_json_writes_file(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)

    layer_path = ensure_layer_json(tiles_dir, OutputFormat.TERRAIN, Profile.GEODETIC)
    assert layer_path.is_file()
    data = json.loads(layer_path.read_text(encoding="utf-8"))
    assert data["format"] == "heightmap-1.0"
    assert data["available"]


def test_ensure_layer_json_keeps_existing_with_available(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    tiles_dir.mkdir()
    existing = {
        "format": "quantized-mesh-1.0",
        "available": [[{"startX": 0, "startY": 0, "endX": 0, "endY": 0}]],
    }
    (tiles_dir / "layer.json").write_text(json.dumps(existing), encoding="utf-8")

    layer_path = ensure_layer_json(tiles_dir, OutputFormat.MESH, Profile.GEODETIC)
    data = json.loads(layer_path.read_text(encoding="utf-8"))
    assert data == existing
