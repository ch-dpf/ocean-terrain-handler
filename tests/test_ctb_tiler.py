"""CTB-compatible Python tiler tests."""

from __future__ import annotations

import gzip
import json
import math
import struct
from pathlib import Path

import numpy as np
import pytest

from app.schemas import CtbOptions, OutputFormat, Profile, ResamplingMethod
from app.services.ctb.constants import (
    GEODETIC_DEFAULT_TILE_SIZE,
    HEIGHTMAP_OFFSET_M,
    HEIGHTMAP_SCALE,
    HEIGHTMAP_TERRAIN_QUALITY,
    MERCATOR_MESH_DEFAULT_TILE_SIZE,
    SEMI_MAJOR_AXIS,
)
from app.services.ctb.encode import encode_heightmap, zigzag_encode
from app.services.ctb.grid import (
    CRSBounds,
    TileCoordinate,
    global_geodetic,
    iter_tile_coordinates,
    tile_coordinate_count,
)
from app.services.ctb.heightfield import HeightField, MeshBuilder
from app.services.ctb.mesh_encode import native_available
from app.services.ctb.tiler import (
    CtbError,
    geometric_error_for_zoom,
    level_zero_geometric_error,
    run_ctb_tile,
)
from app.services.ctb.sample import sanitize_tile_heights
from tests.raster_fixtures import write_dem_geotiff_4326

_SKIP_WITHOUT_NATIVE = pytest.mark.skipif(
    not native_available(), reason="CTB native extension not built"
)


def test_sanitize_tile_heights_replaces_nodata_with_ellipsoid():
    heights = np.array([[10.0, -9999.0], [np.nan, -32768.0]], dtype=np.float32)
    cleaned = sanitize_tile_heights(heights, -9999.0)
    np.testing.assert_array_equal(cleaned, np.array([[10.0, 0.0], [0.0, 0.0]], dtype=np.float32))


def test_geodetic_z0_eastern_hemisphere_bounds():
    grid = global_geodetic(65)
    bounds = grid.tile_bounds(TileCoordinate(0, 1, 0))
    assert bounds.minx == 0.0
    assert bounds.miny == -90.0
    assert bounds.maxx == 180.0
    assert bounds.maxy == 90.0


def test_tile_coordinates_stream_and_count_match():
    grid = global_geodetic(65)
    extent = CRSBounds(116.0, 39.0, 117.0, 40.0)
    coordinates = iter_tile_coordinates(grid, extent, 9, 7)
    assert not isinstance(coordinates, list)
    materialized = list(coordinates)
    assert len(materialized) == tile_coordinate_count(grid, extent, 9, 7)


def test_mercator_mesh_default_is_valid_btt_size():
    edge = MERCATOR_MESH_DEFAULT_TILE_SIZE - 1
    assert edge & (edge - 1) == 0


def test_mesh_rejects_non_btt_tile_size(tmp_path: Path):
    with pytest.raises(CtbError, match="Mesh tile_size"):
        run_ctb_tile(
            tmp_path / "unused.tif",
            tmp_path / "tiles",
            CtbOptions(tile_size=64),
        )


def test_level_zero_geometric_error_matches_ctb_formula():
    grid = global_geodetic(65)
    error = level_zero_geometric_error(grid, 65, 1.0)
    tiles_at_z0 = int(360.0 / (65 * grid.resolution(0)))
    expected = (SEMI_MAJOR_AXIS * 2 * math.pi * HEIGHTMAP_TERRAIN_QUALITY) / (65 * tiles_at_z0)
    assert error == expected
    assert geometric_error_for_zoom(grid, 3, 65, 1.0) == expected / 8.0


def test_flat_heightfield_smooth_small_zoom_activates_lattice():
    size = GEODETIC_DEFAULT_TILE_SIZE
    heights = np.full((size, size), 10.0, dtype=np.float32)
    field = HeightField(heights)
    field.apply_geometric_error(1.0, smooth_small_zooms=True)
    last = size - 1
    step = last // 16
    for x in range(0, last + 1, step):
        for y in range(0, last + 1, step):
            assert field.get_level(x, y) >= 0
    mesh = MeshBuilder(-180.0, -90.0, 0.0, 90.0, size)
    field.generate_mesh(mesh, 0)
    assert len(mesh.vertices) >= 4
    assert len(mesh.indices) >= 3
    assert len(mesh.indices) % 3 == 0


def test_heightmap_encoding_quantizes_and_gzips():
    heights = np.array([[0.0, 20.0], [40.0, 60.0]], dtype=np.float32)
    payload = encode_heightmap(heights, children=5)
    raw = gzip.decompress(payload)
    values = np.frombuffer(raw[:8], dtype="<u2")
    expected = np.trunc((heights.reshape(-1) + HEIGHTMAP_OFFSET_M) * HEIGHTMAP_SCALE).astype(np.uint16)
    np.testing.assert_array_equal(values, expected)
    assert raw[8] == 5
    assert raw[9] == 0


def test_zigzag_matches_ctb_int32_shift():
    assert zigzag_encode(0) == 0
    assert zigzag_encode(1) == 2
    assert zigzag_encode(-1) == 1
    assert zigzag_encode(-2) == 3


@_SKIP_WITHOUT_NATIVE
def test_run_ctb_tile_mesh_writes_gzip_terrain_and_layer_json(tmp_path: Path):
    source = write_dem_geotiff_4326(
        tmp_path / "dem.tif",
        width=32,
        height=32,
        west=116.0,
        north=40.0,
        pixel_deg=0.01,
    )
    output = tmp_path / "tiles"
    run_ctb_tile(
        source,
        output,
        CtbOptions(
            output_format=OutputFormat.MESH,
            profile=Profile.GEODETIC,
            start_zoom=0,
            end_zoom=0,
            thread_count=1,
            resampling_method=ResamplingMethod.BILINEAR,
            cesium_friendly=True,
            vertex_normals=True,
            layer_only=False,
        ),
        cache_bytes=8 * 1024 * 1024,
    )
    eastern = output / "0" / "1" / "0.terrain"
    western = output / "0" / "0" / "0.terrain"
    assert eastern.is_file()
    assert western.is_file()
    blob = eastern.read_bytes()
    assert blob[:2] == b"\x1f\x8b"
    raw = gzip.decompress(blob)
    vertex_count = struct.unpack_from("<i", raw, 88)[0]
    assert vertex_count > 0
    layer = json.loads((output / "layer.json").read_text(encoding="utf-8"))
    assert layer["format"] == "quantized-mesh-1.0"
    assert layer["scheme"] == "tms"
    assert layer["extensions"] == ["octvertexnormals"]
    assert layer["available"][0][0]["startX"] == 0
    assert layer["available"][0][0]["endX"] == 1
    # DEM extent (32 * 0.01°), not the z0 eastern-hemisphere tile rectangle.
    assert layer["bounds"] == pytest.approx([116.0, 39.68, 116.32, 40.0])
    # Quantized-mesh header: center(3*f64) + min/max height (2*f32) at offset 24.
    min_h, max_h = struct.unpack_from("<ff", raw, 24)
    assert min_h >= -1.0
    assert max_h < 500.0

@_SKIP_WITHOUT_NATIVE
def test_run_ctb_tile_heightmap_and_layer_only(tmp_path: Path):
    source = write_dem_geotiff_4326(
        tmp_path / "dem.tif",
        width=16,
        height=16,
        west=116.0,
        north=40.0,
        pixel_deg=0.01,
    )
    tiled = tmp_path / "hm"
    run_ctb_tile(
        source,
        tiled,
        CtbOptions(
            output_format=OutputFormat.TERRAIN,
            start_zoom=0,
            end_zoom=0,
            thread_count=1,
            resampling_method=ResamplingMethod.NEAREST,
            cesium_friendly=False,
            vertex_normals=False,
        ),
        cache_bytes=4 * 1024 * 1024,
    )
    tile = tiled / "0" / "1" / "0.terrain"
    assert tile.is_file()
    raw = gzip.decompress(tile.read_bytes())
    assert len(raw) == 65 * 65 * 2 + 2

    only = tmp_path / "meta"
    run_ctb_tile(
        source,
        only,
        CtbOptions(
            output_format=OutputFormat.MESH,
            start_zoom=0,
            end_zoom=0,
            thread_count=1,
            layer_only=True,
            cesium_friendly=True,
        ),
        cache_bytes=4 * 1024 * 1024,
    )
    assert (only / "layer.json").is_file()
    assert not any(only.rglob("*.terrain"))
