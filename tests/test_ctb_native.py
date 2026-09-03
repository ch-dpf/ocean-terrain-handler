"""Native C++ meshing/encoding vs the Python reference implementation."""

from __future__ import annotations

import gzip

import numpy as np
import pytest

from app.services.ctb.encode import encode_heightmap, encode_quantized_mesh
from app.services.ctb.heightfield import HeightField, MeshBuilder
from app.services.ctb.mesh_encode import (
    aggregate_footprints_f32,
    box_average_f32,
    encode_heightmap_tile_bytes,
    encode_mesh_tile_bytes,
    native_available,
    remap_f32_hwc,
)

pytestmark = pytest.mark.skipif(not native_available(), reason="CTB native extension not built")


def test_native_extension_loads():
    assert native_available()


def test_native_heightmap_matches_python_payload():
    heights = np.array([[0.0, 20.0], [40.0, 60.0]], dtype=np.float32)
    native = encode_heightmap_tile_bytes(heights, 5)
    python = encode_heightmap(heights, 5)
    assert gzip.decompress(native) == gzip.decompress(python)


def test_native_mesh_matches_python_payload():
    size = 65
    rng = np.random.default_rng(0)
    heights = rng.random((size, size), dtype=np.float32) * 80.0
    kwargs = {
        "minx": -180.0,
        "miny": -90.0,
        "maxx": 0.0,
        "maxy": 90.0,
        "geometric_error": 1.0,
        "smooth_small_zooms": True,
        "neighbors": None,
        "write_vertex_normals": True,
    }
    native = encode_mesh_tile_bytes(heights, **kwargs)
    field = HeightField(heights)
    field.apply_geometric_error(1.0, True)
    mesh = MeshBuilder(-180.0, -90.0, 0.0, 90.0, size)
    field.generate_mesh(mesh, 0)
    python = encode_quantized_mesh(
        mesh.vertices,
        mesh.indices,
        write_vertex_normals=True,
    )
    assert gzip.decompress(native) == gzip.decompress(python)


def test_native_average_uses_source_pixel_overlap_area():
    source = np.array([[0.0, 10.0], [20.0, 30.0]], dtype=np.float32)
    # One destination pixel covers half of the two pixels in the first row.
    rows = np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float64)
    cols = np.array([[0.5, 1.5], [0.5, 1.5]], dtype=np.float64)
    sampled = aggregate_footprints_f32(source, rows, cols, 0, np.nan)
    np.testing.assert_allclose(sampled, [[5.0]], atol=1e-6)


def test_native_box_average_matches_area_weighted_blocks():
    source = np.array([[0.0, 10.0], [20.0, 30.0]], dtype=np.float32)
    sampled = box_average_f32(source, 1, 1, np.nan)
    np.testing.assert_allclose(sampled, [[15.0]], atol=1e-6)


def test_python_sample_footprint_uses_native_kernel():
    from app.services.raster.resample import sample_footprint

    source = np.array([[0.0, 10.0], [20.0, 30.0]], dtype=np.float32)
    rows = np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float64)
    cols = np.array([[0.5, 1.5], [0.5, 1.5]], dtype=np.float64)
    sampled = sample_footprint(source, rows, cols, "average")
    np.testing.assert_allclose(sampled, [[5.0]], atol=1e-6)


def test_native_sheared_average_uses_polygon_overlap():
    source = np.array([[100.0, 0.0], [0.0, 0.0]], dtype=np.float32)
    # Bottom edge shifts 0.5 px right. The unit parallelogram still overlaps
    # the left source pixel more (area 0.75 vs 0.25).
    rows = np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float64)
    cols = np.array([[0.0, 1.0], [0.5, 1.5]], dtype=np.float64)
    sampled = aggregate_footprints_f32(source, rows, cols, 0, np.nan)
    np.testing.assert_allclose(sampled, [[75.0]], atol=1e-4)


def test_native_average_renormalizes_at_raster_boundary():
    source = np.array([[10.0]], dtype=np.float32)
    rows = np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float64)
    cols = np.array([[-0.5, 0.5], [-0.5, 0.5]], dtype=np.float64)
    sampled = aggregate_footprints_f32(source, rows, cols, 0, np.nan)
    np.testing.assert_allclose(sampled, [[10.0]], atol=1e-6)


@pytest.mark.parametrize(
    ("method_code", "expected"),
    [
        (1, 1.0),  # mode: equal counts keep the first encountered value
        (2, 4.0),
        (3, 1.0),
        (4, 2.0),
        (5, 1.0),
        (6, 3.0),
    ],
)
def test_native_aggregate_methods(method_code: int, expected: float):
    source = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
    rows = np.array([[0.0, 0.0], [2.0, 2.0]], dtype=np.float64)
    cols = np.array([[0.0, 2.0], [0.0, 2.0]], dtype=np.float64)
    sampled = aggregate_footprints_f32(source, rows, cols, method_code, np.nan)
    np.testing.assert_allclose(sampled, [[expected]], atol=1e-6)


def test_native_mode_tie_keeps_first_encountered():
    source = np.array([[4.0, 1.0], [2.0, 3.0]], dtype=np.float32)
    rows = np.array([[0.0, 0.0], [2.0, 2.0]], dtype=np.float64)
    cols = np.array([[0.0, 2.0], [0.0, 2.0]], dtype=np.float64)
    sampled = aggregate_footprints_f32(source, rows, cols, 1, np.nan)
    assert sampled[0, 0] == 4.0


@pytest.mark.parametrize("method_code", range(5))
def test_native_remap_preserves_pixel_centers(method_code: int):
    source = np.arange(81, dtype=np.float32).reshape(9, 9, 1)
    rows, cols = np.meshgrid(
        np.arange(9, dtype=np.float64) + 0.5,
        np.arange(9, dtype=np.float64) + 0.5,
        indexing="ij",
    )
    sampled = remap_f32_hwc(source, cols, rows, method_code)
    # Higher-order kernels use zero outside the source. Compare the interior,
    # where every kernel has its complete support.
    margin = 0 if method_code <= 1 else (2 if method_code <= 3 else 3)
    if margin == 0:
        np.testing.assert_allclose(sampled, source, atol=1e-5)
    else:
        np.testing.assert_allclose(
            sampled[margin:-margin, margin:-margin],
            source[margin:-margin, margin:-margin],
            atol=1e-4,
        )


def test_native_bilinear_skips_nodata_and_renormalizes():
    source = np.array([[1.0, np.nan], [3.0, 5.0]], dtype=np.float32)
    sampled = remap_f32_hwc(
        source,
        np.array([[1.0]], dtype=np.float64),
        np.array([[1.0]], dtype=np.float64),
        1,
    )
    np.testing.assert_allclose(sampled[:, :, 0], [[3.0]], atol=1e-6)
