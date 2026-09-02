"""Mesh + encode facade: Cython/C++ when built, Python reference otherwise."""

from __future__ import annotations

import logging
from typing import Mapping

import numpy as np

from app.services.ctb.encode import encode_heightmap, encode_quantized_mesh
from app.services.ctb.heightfield import HeightField, MeshBuilder

logger = logging.getLogger(__name__)

_native_module = None
_native_import_error: str | None = None
try:
    from app.services.ctb import _ctb_core as _native_module
except Exception as exc:  # pragma: no cover - depends on local compile
    _native_import_error = str(exc)
    _native_module = None

if _native_module is not None:
    logger.info("CTB meshing/encoding: Cython/C++ extension")
else:
    logger.warning(
        "CTB meshing/encoding: Python reference (%s). Build with: python setup.py build_ext --inplace",
        _native_import_error or "extension not built",
    )


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
