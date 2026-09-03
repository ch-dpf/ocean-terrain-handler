"""GDAL-compatible quadrant NODATA interpolation with bounded halo windows."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from app.services.ctb.mesh_encode import fill_nodata_f32
from app.services.raster.geotiff import GeoTiffReader, write_geotiff_tiled
from app.services.raster.nodata import nodata_mask
from app.services.raster.resample import cast_sampled

ProgressFn = Callable[[float, str | None], None]
DEFAULT_MAX_DISTANCE = 10


def fill_nodata_array(
    data: np.ndarray, *, nodata: float | None, max_distance: int = DEFAULT_MAX_DISTANCE
) -> np.ndarray:
    """Use original donors, quadrant search and inverse distance weights."""
    if data.ndim != 2:
        raise ValueError("fill_nodata_array expects a 2D array")
    if max_distance < 1:
        raise ValueError("max_distance must be positive")
    invalid = nodata_mask(data, nodata)
    if not np.any(invalid):
        return data.copy()
    values = np.asarray(data, dtype=np.float32).copy()
    values[invalid] = np.nan
    filled = fill_nodata_f32(values, int(max_distance))
    result = data.astype(np.result_type(data.dtype, np.float32), copy=True)
    result[invalid] = filled[invalid]
    remaining = invalid & ~np.isfinite(filled)
    result[remaining] = np.nan if nodata is None else nodata
    return result


def fill_nodata_geotiff(
    input_path: Path,
    output_path: Path,
    *,
    max_distance: int = DEFAULT_MAX_DISTANCE,
    block_size: int = 256,
    compress: str = "DEFLATE",
    cache_bytes: int = 512 * 1024 * 1024,
    nodata: float | None = None,
    on_progress: ProgressFn | None = None,
) -> None:
    """Stream blocks with a radius halo; never use filled pixels as donors."""
    if max_distance < 1:
        raise ValueError("max_distance must be positive")
    pad = int(max_distance)
    with GeoTiffReader(input_path, cache_bytes=max(1, cache_bytes // 2), preload=False) as src:
        effective_nodata = src.nodata if nodata is None else nodata
        tile = block_size
        # One output TIFF block plus the minimum radius halo are unavoidable.
        edge_budget = max(1, int(np.sqrt(max(cache_bytes // 2, 1) / 40)) - 2 * pad)
        compute_edge = min(tile, edge_budget)
        planned = max(1, src.width * src.height)

        def tiles() -> Iterator[np.ndarray]:
            done = 0
            for r0 in range(0, src.height, tile):
                for c0 in range(0, src.width, tile):
                    h, w = min(tile, src.height - r0), min(tile, src.width - c0)
                    full = np.full(
                        (tile, tile),
                        0 if effective_nodata is None else effective_nodata,
                        dtype=src.dtype,
                    )
                    for r in range(r0, r0 + h, compute_edge):
                        for c in range(c0, c0 + w, compute_edge):
                            sh, sw = min(compute_edge, r0 + h - r), min(compute_edge, c0 + w - c)
                            # Outside padding must never become a zero-valued donor.
                            rr, cc = max(0, r - pad), max(0, c - pad)
                            rh, cw = (
                                min(src.height, r + sh + pad) - rr,
                                min(src.width, c + sw + pad) - cc,
                            )
                            window = src.read_window(rr, cc, rh, cw)[:, :, 0]
                            filled = fill_nodata_array(
                                window, nodata=effective_nodata, max_distance=pad
                            )
                            interior = filled[r - rr : r - rr + sh, c - cc : c - cc + sw]
                            full[r - r0 : r - r0 + sh, c - c0 : c - c0 + sw] = cast_sampled(
                                interior, src.dtype, nodata=effective_nodata
                            )
                    done += h * w
                    if on_progress is not None:
                        on_progress(100.0 * done / planned, "fill-nodata")
                    yield full

        write_geotiff_tiled(
            output_path,
            tiles(),
            shape=(src.height, src.width, 1),
            affine=src.affine,
            crs=src.crs,
            compress=compress,
            block_size=tile,
            dtype=src.dtype,
            nodata=effective_nodata,
        )
    if on_progress is not None:
        on_progress(100.0, "fill-nodata complete")
