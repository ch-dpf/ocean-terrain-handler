"""Sample a CTB terrain/mesh tile height grid (TerrainTiler overlap + warp)."""

from __future__ import annotations

import numpy as np

from app.services.ctb.constants import DEFAULT_WARP_NODATA
from app.services.ctb.grid import CRSBounds, Grid, TileCoordinate
from app.services.raster.affine import Affine
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.nodata import nodata_mask
from app.services.raster.resample import AGGREGATE_RESAMPLING, normalize_resampling
from app.services.raster.warp import warp_window

# These formats have no NODATA flag. After source-edge support is extended,
# remaining invalid samples retain the legacy ellipsoid fallback. Interior
# holes and non-rectangular coverage still require explicit acceptance tests.
_ELLIPSOID_HEIGHT_M = 0.0


def extend_outer_support(
    heights: np.ndarray, inside: np.ndarray, nodata: float | None
) -> np.ndarray:
    """Extend rectangular source-edge support without filling interior holes.

    Mesh vertices span the tile, including overlap samples just outside the
    source. Zeroing those samples creates an artificial slope inside the DEM.
    Outer support uses the closest sampled source edge; this is extrapolation
    outside coverage, not newly observed terrain. Interior NODATA is unchanged.
    """
    if inside.all():
        return heights
    rows, cols = np.nonzero(inside)
    if not len(rows):
        return heights
    r0, r1, c0, c1 = rows.min(), rows.max(), cols.min(), cols.max()
    if not inside[r0 : r1 + 1, c0 : c1 + 1].all():
        return heights  # non-rectangular/projected coverage needs a separate policy
    rr, cc = np.indices(heights.shape)
    donors = heights[np.clip(rr, r0, r1), np.clip(cc, c0, c1)]
    replace = ~inside & nodata_mask(heights, nodata) & ~nodata_mask(donors, nodata)
    out = heights.copy()
    out[replace] = donors[replace]
    return out


def terrain_overlap_affine(bounds: CRSBounds, tile_size: int) -> Affine:
    """TerrainTiler::terrainTileBounds then north-up geotransform.

    Extends one pixel west and north at resolution = tileWidth / (tileSize - 1).
    """
    resolution = bounds.width / float(tile_size - 1)
    return Affine.north_up(
        bounds.minx - resolution, bounds.maxy + resolution, resolution, resolution
    )


def _extend_global_support(src, grid, affine, heights, resampling, nodata):
    """Use source-supported points on the global zoom lattice as donors.

    Donors may lie outside this tile. Tile-local clipping cannot supply an
    entirely outside edge tile and gives inconsistent shared-edge heights.
    Source interior holes are deliberately retained.
    """
    size = grid.tile_size
    col0 = (affine.c + 0.5 * affine.a - src.affine.c) / src.affine.a
    row0 = (affine.f + 0.5 * affine.e - src.affine.f) / src.affine.e
    dx, dy = affine.a / src.affine.a, affine.e / src.affine.e
    cols, rows = col0 + np.arange(size) * dx, row0 + np.arange(size) * dy
    inside = ((rows >= 0) & (rows < src.height))[:, None] & ((cols >= 0) & (cols < src.width))[
        None, :
    ]
    missing = ~inside & nodata_mask(heights, nodata)
    if not missing.any():
        return heights
    c0, c1 = int(np.ceil(-col0 / dx)), int(np.ceil((src.width - col0) / dx)) - 1
    r0, r1 = int(np.ceil(-row0 / dy)), int(np.ceil((src.height - row0) / dy)) - 1
    if c0 > c1 or r0 > r1:
        return heights  # no supported lattice point at this coarse zoom
    cc = np.clip(np.arange(size), c0, c1)
    rr = np.clip(np.arange(size), r0, r1)
    left, top = int(cc.min()), int(rr.min())
    right, bottom = int(cc.max()), int(rr.max())
    if 0 <= left <= right < size and 0 <= top <= bottom < size:
        donors = heights[rr[:, None], cc[None, :]]
    else:
        span = size if normalize_resampling(resampling) in AGGREGATE_RESAMPLING else size - 1
        level = src.select_level(span * dx, span * dy, size, size)

        class PinnedSource:
            # A narrow donor strip must retain the original tile's overview.
            def select_level(self, *_args):
                return level

            def __getattr__(self, name):
                return getattr(src, name)

        donor_affine = Affine.north_up(
            affine.c + left * affine.a,
            affine.f + top * affine.e,
            affine.a,
            -affine.e,
        )
        sampled = warp_window(
            PinnedSource(),
            donor_affine,
            grid.crs,
            0,
            0,
            bottom - top + 1,
            right - left + 1,
            resampling,
            nodata=nodata,
            band=0,
        )[:, :, 0]
        donors = sampled[(rr - top)[:, None], (cc - left)[None, :]]
    replace = missing & ~nodata_mask(donors, nodata)
    out = heights.copy()
    out[replace] = donors[replace]
    return out


def sanitize_tile_heights(heights: np.ndarray, nodata: float | None) -> np.ndarray:
    """Replace NODATA / non-finite samples with ellipsoid height (0 m)."""
    out = np.asarray(heights, dtype=np.float32, copy=True)
    mask = nodata_mask(out, nodata)
    # Also catch warp fill when src.nodata differs from the requested fill.
    if nodata is None or (
        isinstance(nodata, float) and not np.isnan(nodata) and nodata != DEFAULT_WARP_NODATA
    ):
        mask = mask | nodata_mask(out, DEFAULT_WARP_NODATA)
    if np.any(mask):
        out[mask] = np.float32(_ELLIPSOID_HEIGHT_M)
    return out


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
    heights = np.asarray(warped[:, :, 0], dtype=np.float32)
    # Scope edge extrapolation to the verified north-up, same-CRS path.
    if (
        src.crs.equals(grid.crs)
        and src.affine.is_north_up()
        and src.affine.a > 0
        and src.affine.e < 0
    ):
        heights = _extend_global_support(src, grid, dst_affine, heights, resampling, nodata)
    return sanitize_tile_heights(heights, nodata)


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
