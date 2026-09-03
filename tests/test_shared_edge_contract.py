"""Independent decoded-edge contract: interiors must not affect shared edges."""

import gzip
import struct

import numpy as np
import pytest

from app.services.ctb.mesh_encode import encode_mesh_tile_bytes


def edge_profile(payload, axis, side, points):
    raw = gzip.decompress(payload)
    low, high = struct.unpack_from("<ff", raw, 24)
    count = struct.unpack_from("<I", raw, 88)[0]
    delta = np.frombuffer(raw, "<u2", count * 3, 92).astype(np.int64).reshape(3, count)
    coordinates = np.cumsum((delta >> 1) ^ -(delta & 1), axis=1).T
    vertices = coordinates[coordinates[:, axis] == side * 32767]
    order = np.argsort(vertices[:, 1 - axis])
    vertices = vertices[order]
    return np.interp(
        points, vertices[:, 1 - axis] / 32767, low + vertices[:, 2] * (high - low) / 32767
    )


@pytest.mark.parametrize("axis", [0, 1])
def test_shared_edges_independent_of_tile_interior(axis):
    rng = np.random.default_rng(923)
    a = (rng.random((65, 65)) * 500 - 2000).astype("float32")
    b = (rng.random((65, 65)) * 700 - 2000).astype("float32")
    curve = (-1600 + 80 * np.sin(np.linspace(0, 3 * np.pi, 65))).astype("float32")
    if axis == 0:
        a[:, -1], b[:, 0] = curve, curve
    else:
        a[0, :], b[-1, :] = curve, curve
    original_a, original_b = a.copy(), b.copy()
    a.flags.writeable = b.flags.writeable = False
    options = {
        "geometric_error": 20,
        "smooth_small_zooms": False,
        "neighbors": None,
        "write_vertex_normals": True,
        "canonical_edges": True,
    }
    pa = encode_mesh_tile_bytes(a, 0, 0, 1, 1, **options)
    pb = encode_mesh_tile_bytes(
        b, axis == 0, axis == 1, 1 + (axis == 0), 1 + (axis == 1), **options
    )
    points = np.linspace(0, 1, 1001)
    va = edge_profile(pa, axis, 1, points)
    vb = edge_profile(pb, axis, 0, points)
    assert abs(va - vb).max() < 0.05
    reference = np.interp(points, np.linspace(0, 1, 65), curve[::-1] if axis == 0 else curve)
    assert abs(va - reference).max() <= 5.05  # one quarter of requested error + quantization
    np.testing.assert_array_equal(a, original_a)
    np.testing.assert_array_equal(b, original_b)
