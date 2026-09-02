"""Mesh + encode facade: Cython/C++ when available, Python reference otherwise."""

from __future__ import annotations

from functools import lru_cache
import gzip
import logging
import struct
import time
from typing import Mapping

import numpy as np

from app.services.ctb.encode import encode_heightmap, encode_quantized_mesh
from app.services.ctb.heightfield import HeightField, MeshBuilder

logger = logging.getLogger(__name__)

# One 65×65 mesh+gzip must finish within this budget or native is treated as unusable.
NATIVE_TILE_BUDGET_S = 0.1

_native_module = None
_native_import_error: str | None = None
try:
    from app.services.ctb import _ctb_core as _native_module
except Exception as exc:  # pragma: no cover - depends on local compile
    _native_import_error = str(exc)
    _native_module = None


def native_available() -> bool:
    return _native_module is not None


def native_import_error() -> str | None:
    return _native_import_error


def _python_mesh_encode(
    heights: np.ndarray,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    geometric_error: float,
    smooth_small_zooms: bool,
    neighbors: Mapping[int, np.ndarray] | None,
    write_vertex_normals: bool,
) -> bytes:
    field = HeightField(heights)
    field.apply_geometric_error(geometric_error, smooth_small_zooms)
    if neighbors:
        for border, neighbor in neighbors.items():
            other = HeightField(neighbor)
            other.apply_geometric_error(geometric_error, False)
            field.apply_border_activation_state(other, int(border))
    tile_size = int(heights.shape[0])
    mesh = MeshBuilder(minx, miny, maxx, maxy, tile_size)
    field.generate_mesh(mesh, 0)
    if not mesh.vertices:
        raise RuntimeError("Mesh generation produced no vertices")
    return encode_quantized_mesh(
        mesh.vertices,
        mesh.indices,
        write_vertex_normals=write_vertex_normals,
    )


def encode_mesh_tile_bytes(
    heights: np.ndarray,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    geometric_error: float,
    smooth_small_zooms: bool,
    neighbors: Mapping[int, np.ndarray] | None,
    write_vertex_normals: bool,
    *,
    use_native: bool | None = None,
) -> bytes:
    """Encode one quantized-mesh tile. Default uses C++ when the extension loaded."""
    prefer_native = native_available() if use_native is None else bool(use_native)
    if prefer_native:
        if _native_module is None:
            raise RuntimeError("CTB native extension is not available")
        mapping = neighbors or {}
        return _native_module.encode_mesh_tile_bytes(
            heights,
            minx,
            miny,
            maxx,
            maxy,
            geometric_error,
            smooth_small_zooms,
            mapping.get(0),
            mapping.get(1),
            mapping.get(2),
            mapping.get(3),
            write_vertex_normals,
        )
    return _python_mesh_encode(
        heights,
        minx,
        miny,
        maxx,
        maxy,
        geometric_error,
        smooth_small_zooms,
        neighbors,
        write_vertex_normals,
    )


def encode_heightmap_tile_bytes(
    heights: np.ndarray,
    children: int,
    *,
    use_native: bool | None = None,
) -> bytes:
    prefer_native = native_available() if use_native is None else bool(use_native)
    if prefer_native:
        if _native_module is None:
            raise RuntimeError("CTB native extension is not available")
        return _native_module.encode_heightmap_tile_bytes(heights, int(children))
    return encode_heightmap(heights, children)


@lru_cache(maxsize=1)
def native_meets_bar() -> tuple[bool, str]:
    """Functional + latency gate for the C++ meshing/encoding path."""
    if _native_module is None:
        return False, _native_import_error or "native extension not built"
    size = 65
    heights = np.linspace(0.0, 120.0, size * size, dtype=np.float32).reshape(size, size)
    started = time.perf_counter()
    try:
        blob = encode_mesh_tile_bytes(
            heights,
            -180.0,
            -90.0,
            0.0,
            90.0,
            1.0,
            True,
            None,
            True,
            use_native=True,
        )
    except Exception as exc:
        return False, f"native encode failed: {exc}"
    elapsed = time.perf_counter() - started
    if blob[:2] != b"\x1f\x8b":
        return False, "native output is not gzip"
    try:
        raw = gzip.decompress(blob)
        vertex_count = struct.unpack_from("<i", raw, 88)[0]
    except Exception as exc:
        return False, f"native gzip/mesh header invalid: {exc}"
    if vertex_count <= 0:
        return False, "native mesh has no vertices"
    if elapsed > NATIVE_TILE_BUDGET_S:
        return False, f"native mesh+encode {elapsed:.3f}s exceeds {NATIVE_TILE_BUDGET_S:.3f}s"
    return True, f"native mesh+encode {elapsed:.4f}s vertices={vertex_count}"
