"""Synthetic GeoTIFF helpers for DEM raster tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pyproj import CRS

from app.services.raster.affine import Affine
from app.services.raster.geotiff import write_geotiff_array


def write_dem_geotiff_4326(
    path: Path,
    *,
    width: int = 32,
    height: int = 32,
    west: float = 116.0,
    north: float = 40.0,
    pixel_deg: float = 0.001,
    nodata: float | None = -9999.0,
    dtype: np.dtype | type = np.float32,
    hole: tuple[int, int, int, int] | None = None,
) -> Path:
    """Write a north-up single-band DEM in EPSG:4326.

    Elevation is a ramp plus a row/column gradient so resampling is observable.
    ``hole`` is ``(row0, col0, row1, col1)`` exclusive of ``row1``/``col1``.
    """
    rows = np.arange(height, dtype=np.float32)[:, None]
    cols = np.arange(width, dtype=np.float32)[None, :]
    data = (10.0 + rows * 0.5 + cols * 0.25).astype(dtype, copy=False)
    if hole is not None and nodata is not None:
        r0, c0, r1, c1 = hole
        data[r0:r1, c0:c1] = nodata
    affine = Affine.north_up(west, north, pixel_deg, pixel_deg)
    write_geotiff_array(
        path,
        data,
        affine=affine,
        crs=CRS.from_epsg(4326),
        compress="DEFLATE",
        block_size=16,
        nodata=nodata,
    )
    return path
