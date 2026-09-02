"""Native C++ meshing/encoding vs the Python reference implementation."""

from __future__ import annotations

import gzip

import numpy as np
import pytest

from app.services.ctb.mesh_encode import (
    encode_heightmap_tile_bytes,
    encode_mesh_tile_bytes,
    native_available,
)

pytestmark = pytest.mark.skipif(not native_available(), reason="CTB native extension not built")


def test_native_extension_loads():
    assert native_available()


def test_native_heightmap_matches_python_payload():
    heights = np.array([[0.0, 20.0], [40.0, 60.0]], dtype=np.float32)
    native = encode_heightmap_tile_bytes(heights, 5, use_native=True)
    python = encode_heightmap_tile_bytes(heights, 5, use_native=False)
    assert gzip.decompress(native) == gzip.decompress(python)


def test_native_mesh_matches_python_payload():
    size = 65
    rng = np.random.default_rng(0)
    heights = rng.random((size, size), dtype=np.float32) * 80.0
    kwargs = dict(
        minx=-180.0,
        miny=-90.0,
        maxx=0.0,
        maxy=90.0,
        geometric_error=1.0,
        smooth_small_zooms=True,
        neighbors=None,
        write_vertex_normals=True,
    )
    native = encode_mesh_tile_bytes(heights, **kwargs, use_native=True)
    python = encode_mesh_tile_bytes(heights, **kwargs, use_native=False)
    assert gzip.decompress(native) == gzip.decompress(python)
