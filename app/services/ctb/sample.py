"""Sample a CTB terrain/mesh tile height grid (TerrainTiler overlap + warp)."""

from __future__ import annotations

import numpy as np

from app.services.ctb.constants import DEFAULT_WARP_NODATA
from app.services.ctb.grid import CRSBounds, Grid, TileCoordinate
from app.services.raster.affine import Affine
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.warp import warp_window


def terrain_overlap_affine(bounds: CRSBounds, tile_size: int) -> Affine:
    """TerrainTiler::terrainTileBounds then north-up geotransform.

    Extends one pixel west and north at resolution = tileWidth / (tileSize - 1).
    """
    resolution = bounds.width / float(tile_size - 1)
    return Affine.north_up(bounds.minx - resolution, bounds.maxy + resolution, resolution, resolution)


def sample_tile_heights(
    src: GeoTiffReader,
    grid: Grid,
    coord: TileCoordinate,
    resampling: str,
) -> np.ndarray:
    """Return float32 ``(tile_size, tile_size)`` row-major, north-up (GDAL RasterIO)."""
    tile_size = grid.tile_size
    bounds = grid.tile_bounds(coord)
    dst_affine = terrain_overlap_affine(bounds, tile_size)
    nodata = src.nodata if src.nodata is not None else DEFAULT_WARP_NODATA
    warped = warp_window(
        src,
        dst_affine,
        grid.crs,
        0,
        0,
        tile_size,
        tile_size,
        resampling,
        nodata=nodata,
        band=0,
    )
    return np.asarray(warped[:, :, 0], dtype=np.float32)


def dataset_bounds_in_grid_crs(src: GeoTiffReader, grid: Grid) -> CRSBounds:
    """GDALTiler constructor: native bounds, or 4-corner transform into the grid CRS."""
    minx, miny, maxx, maxy = src.bounds
    from app.services.raster.crsutil import crs_equal, make_transformer, transform_xy

    if crs_equal(src.crs, grid.crs):
        return CRSBounds(minx, miny, maxx, maxy)
    transformer = make_transformer(src.crs, grid.crs)
    xs = np.array([minx, maxx, maxx, minx], dtype=np.float64)
    ys = np.array([miny, miny, maxy, maxy], dtype=np.float64)
    tx, ty = transform_xy(transformer, xs, ys)
    return CRSBounds(float(np.min(tx)), float(np.min(ty)), float(np.max(tx)), float(np.max(ty)))


def dataset_resolution(src: GeoTiffReader, grid: Grid, bounds: CRSBounds) -> float:
    """GDALTiler: ``abs(gt[1])`` if CRS matches, else transformed width / raster X size."""
    from app.services.raster.crsutil import crs_equal

    if crs_equal(src.crs, grid.crs):
        return abs(float(src.affine.a))
    return bounds.width / float(src.width)
