"""Reproject a GeoTIFF to a target CRS (replacement for ``gdal raster reproject``)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
from pyproj import CRS

from app.services.raster.affine import Affine
from app.services.raster.crsutil import (
    WEB_MERCATOR_MAX_LAT,
    crs_epsg,
    dest_sample_tolerance,
    destination_pixel_size,
    grid_dimension,
    make_transformer,
    parse_crs,
    transform_bounds,
)
from app.services.raster.errors import RasterError
from app.services.raster.geotiff import GeoTiffReader, write_geotiff_tiled
from app.services.raster.parallel import default_workers, ordered_parallel_map
from app.services.raster.warp import warp_window

ProgressFn = Callable[[float, str | None], None]


def plan_destination_grid(
    src: GeoTiffReader,
    dst_crs: CRS,
) -> tuple[Affine, int, int]:
    src_bounds = src.bounds
    px, py = destination_pixel_size(src.crs, dst_crs, src.affine, src.width, src.height)
    if crs_epsg(dst_crs) == 3857:
        wgs84 = parse_crs("EPSG:4326")
        wgs_px, wgs_py = destination_pixel_size(src.crs, wgs84, src.affine, src.width, src.height)
        west, south, east, north = transform_bounds(
            src.crs, wgs84, src_bounds, dest_abs_tol=dest_sample_tolerance(wgs_px, wgs_py)
        )
        south = max(south, -WEB_MERCATOR_MAX_LAT)
        north = min(north, WEB_MERCATOR_MAX_LAT)
        left, bottom, right, top = transform_bounds(wgs84, dst_crs, (west, south, east, north))
    else:
        left, bottom, right, top = transform_bounds(
            src.crs, dst_crs, src_bounds, dest_abs_tol=dest_sample_tolerance(px, py)
        )
    if not np.isfinite([left, bottom, right, top]).all() or right <= left or top <= bottom:
        raise RasterError("Destination extent is empty after reprojection")
    width = grid_dimension(right - left, px)
    height = grid_dimension(top - bottom, py)
    if width > 2_000_000 or height > 2_000_000:
        raise RasterError(f"Destination raster too large: {width}x{height}")
    affine = Affine.north_up(left, top, (right - left) / width, (top - bottom) / height)
    return affine, width, height


def reproject_geotiff(
    input_path: Path,
    output_path: Path,
    *,
    dst_crs: str | CRS,
    compress: str = "DEFLATE",
    block_size: int = 256,
    cache_bytes: int = 512 * 1024 * 1024,
    resampling: str = "bilinear",
    nodata: float | None = None,
    workers: int | None = None,
    on_progress: ProgressFn | None = None,
) -> None:
    target = parse_crs(dst_crs)
    thread_count = default_workers() if workers is None else max(1, int(workers))
    with GeoTiffReader(input_path, cache_bytes=cache_bytes) as src:
        dst_affine, dst_w, dst_h = plan_destination_grid(src, target)
        transformer = None if src.crs.equals(target) else make_transformer(target, src.crs)
        effective_nodata = src.nodata if nodata is None else nodata
        out_dtype = src.dtype

        tile = block_size
        n_ty = (dst_h + tile - 1) // tile
        n_tx = (dst_w + tile - 1) // tile
        coords = [(ty, tx) for ty in range(n_ty) for tx in range(n_tx)]
        planned = max(1, dst_w * dst_h)
        done = 0

        def _compute_tile(coord: tuple[int, int]) -> np.ndarray:
            ty, tx = coord
            r0 = ty * tile
            c0 = tx * tile
            sl_h = min(tile, dst_h - r0)
            sl_w = min(tile, dst_w - c0)
            warped = warp_window(
                src,
                dst_affine,
                target,
                r0,
                c0,
                sl_h,
                sl_w,
                resampling,
                nodata=effective_nodata,
                transformer=transformer,
                band=0,
            )
            full = np.zeros((tile, tile, 1), dtype=out_dtype)
            if effective_nodata is not None:
                full[:] = effective_nodata
            full[:sl_h, :sl_w] = warped
            return full

        def tiles() -> Iterator[np.ndarray]:
            nonlocal done
            for coord, full in zip(coords, ordered_parallel_map(coords, _compute_tile, workers=thread_count)):
                ty, tx = coord
                sl_h = min(tile, dst_h - ty * tile)
                sl_w = min(tile, dst_w - tx * tile)
                done += sl_h * sl_w
                if on_progress is not None:
                    on_progress(100.0 * done / planned, "reproject")
                yield full

        write_geotiff_tiled(
            output_path,
            tiles(),
            shape=(dst_h, dst_w, 1),
            affine=dst_affine,
            crs=target,
            compress=compress,
            block_size=block_size,
            dtype=out_dtype,
            nodata=effective_nodata,
        )
    if on_progress is not None:
        on_progress(100.0, "reproject complete")
