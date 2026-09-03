"""Native resampling, meshing, and terrain encoding facade."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import numpy as np

from app.services.ctb.encode import gzip_terrain

logger = logging.getLogger(__name__)

_NATIVE_REQUIRED = (
    "CTB C++ extension is required for tiling (Python meshing cannot handle large DEM jobs). "
    "Install the wheel matching this OS/CPU/Python ABI; source developers may use "
    "`python -m pip install -e .`."
)

_native_module = None
_native_import_error: str | None = None
try:
    from app.services.ctb import _ctb_core as _native_module
except (ImportError, OSError) as exc:  # pragma: no cover - depends on image/local compile
    _native_import_error = str(exc)
    _native_module = None

if _native_module is not None:
    logger.info("CTB meshing/encoding: Cython/C++ extension")
else:
    logger.error("%s (%s)", _NATIVE_REQUIRED, _native_import_error or "extension not built")


def native_available() -> bool:
    return _native_module is not None


def native_import_error() -> str | None:
    return _native_import_error


def require_native() -> None:
    if _native_module is None:
        detail = _native_import_error or "extension not built"
        raise RuntimeError(f"{_NATIVE_REQUIRED} ({detail})")


def fill_nodata_f32(source: np.ndarray, radius: int) -> np.ndarray:
    require_native()
    if not hasattr(_native_module, "fill_nodata_f32"):
        raise RuntimeError("CTB extension is outdated; rebuild the native wheel for preprocessing")
    return _native_module.fill_nodata_f32(source, radius)


def aggregate_footprints_f32(
    source: np.ndarray,
    corner_rows: np.ndarray,
    corner_cols: np.ndarray,
    method_code: int,
    fill: float,
) -> np.ndarray:
    """Run the native CTB/GDAL-style aggregate sampling kernel."""
    require_native()
    return _native_module.aggregate_footprints_f32(
        source,
        corner_rows,
        corner_cols,
        int(method_code),
        float(fill),
    )


def box_average_f32(source: np.ndarray, dst_h: int, dst_w: int, fill: float) -> np.ndarray:
    """Run the native area-weighted box-average kernel for overview pyramids."""
    require_native()
    return _native_module.box_average_f32(source, int(dst_h), int(dst_w), float(fill))


def remap_f32_hwc(
    source: np.ndarray,
    map_x: np.ndarray,
    map_y: np.ndarray,
    method_code: int,
) -> np.ndarray:
    """Run the native inverse-map interpolation kernel."""
    require_native()
    return _native_module.remap_f32_hwc(
        source,
        map_x,
        map_y,
        int(method_code),
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
    web_mercator: bool = False,
    canonical_edges: bool = False,
) -> bytes:
    """Encode one quantized-mesh tile in C++."""
    require_native()
    mapping = neighbors or {}
    raw = _native_module.encode_mesh_tile_bytes(
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
        web_mercator,
        canonical_edges,
    )
    return gzip_terrain(raw)


def encode_heightmap_tile_bytes(
    heights: np.ndarray,
    children: int,
) -> bytes:
    require_native()
    return gzip_terrain(_native_module.encode_heightmap_tile_bytes(heights, int(children)))
