"""Fill NODATA holes (replacement for ``gdal raster fill-nodata``).

Uses GDAL's 4-direction inverse-distance weighted interpolation: for each
invalid pixel, the nearest valid samples up/down/left/right within
``max_distance`` are combined as ``sum(v/d) / sum(1/d)``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from app.services.raster.geotiff import GeoTiffReader, write_geotiff_array, write_geotiff_tiled
from app.services.raster.nodata import nodata_mask
from app.services.raster.resample import cast_sampled

ProgressFn = Callable[[float, str | None], None]
DEFAULT_MAX_DISTANCE = 10


def _scan_neighbors(
    values: np.ndarray,
    invalid: np.ndarray,
    max_distance: int,
    *,
    axis: int,
    reverse: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """Nearest valid pixel along one axis; returns (distance, value)."""
    if reverse:
        values_w = np.flip(values, axis=axis)
        invalid_w = np.flip(invalid, axis=axis)
    else:
        values_w = values
        invalid_w = invalid

    length = values_w.shape[axis]
    positions = np.arange(length, dtype=np.int32)
    if axis == 1:
        valid_pos = np.where(~invalid_w, positions[None, :], np.int32(-1))
        last_idx = np.maximum.accumulate(valid_pos, axis=1)
        rows = np.arange(values_w.shape[0])[:, None]
        last_val = values_w[rows, np.clip(last_idx, 0, length - 1)]
        dist = positions[None, :].astype(np.float64) - last_idx.astype(np.float64)
    else:
        valid_pos = np.where(~invalid_w, positions[:, None], np.int32(-1))
        last_idx = np.maximum.accumulate(valid_pos, axis=0)
        cols = np.arange(values_w.shape[1])[None, :]
        last_val = values_w[np.clip(last_idx, 0, length - 1), cols]
        dist = positions[:, None].astype(np.float64) - last_idx.astype(np.float64)

    dist = np.where(last_idx >= 0, dist, np.inf)
    dist[dist > max_distance] = np.inf
    dist[dist <= 0] = np.inf
    if reverse:
        dist = np.flip(dist, axis=axis)
        last_val = np.flip(last_val, axis=axis)
    return dist, last_val


def fill_nodata_array(
    data: np.ndarray,
    *,
    nodata: float | None,
    max_distance: int = DEFAULT_MAX_DISTANCE,
) -> np.ndarray:
    """Return a copy of ``data`` with NODATA holes filled in-place semantics."""
    if data.ndim != 2:
        raise ValueError("fill_nodata_array expects a 2D array")
    values = data.astype(np.float64, copy=True)
    invalid = nodata_mask(values, nodata)
    if not np.any(invalid):
        return data.copy()

    neighbors = [
        _scan_neighbors(values, invalid, max_distance, axis=1, reverse=False),
        _scan_neighbors(values, invalid, max_distance, axis=1, reverse=True),
        _scan_neighbors(values, invalid, max_distance, axis=0, reverse=False),
        _scan_neighbors(values, invalid, max_distance, axis=0, reverse=True),
    ]
    weight_sum = np.zeros(values.shape, dtype=np.float64)
    weighted = np.zeros(values.shape, dtype=np.float64)
    for dist, val in neighbors:
        weight = np.zeros(values.shape, dtype=np.float64)
        usable = np.isfinite(dist)
        weight[usable] = 1.0 / dist[usable]
        weight_sum += weight
        weighted += val * weight
    fillable = invalid & (weight_sum > 0)
    filled = values.copy()
    filled[fillable] = weighted[fillable] / weight_sum[fillable]
    remaining = invalid & ~fillable
    if nodata is None:
        filled[remaining] = np.nan
    else:
        filled[remaining] = nodata
    return filled


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
    """Fill NODATA in a single-band GeoTIFF using overlapping windows."""
    pad = max(1, int(max_distance))
    with GeoTiffReader(input_path, cache_bytes=cache_bytes) as src:
        effective_nodata = src.nodata if nodata is None else nodata
        working_bytes = int(src.height) * int(src.width) * 8
        if working_bytes <= cache_bytes:
            data = src.read_window(0, 0, src.height, src.width)[:, :, 0]
            filled = fill_nodata_array(data, nodata=effective_nodata, max_distance=pad)
            typed = cast_sampled(filled[:, :, np.newaxis].astype(np.float32, copy=False), src.dtype, nodata=effective_nodata)
            write_geotiff_array(
                output_path,
                typed,
                affine=src.affine,
                crs=src.crs,
                compress=compress,
                block_size=block_size,
                nodata=effective_nodata,
            )
            if on_progress is not None:
                on_progress(100.0, "fill-nodata complete")
            return
        tile = block_size
        n_ty = (src.height + tile - 1) // tile
        n_tx = (src.width + tile - 1) // tile
        coords = [(ty, tx) for ty in range(n_ty) for tx in range(n_tx)]
        planned = max(1, src.width * src.height)
        done = 0

        def _compute_tile(coord: tuple[int, int]) -> np.ndarray:
            ty, tx = coord
            r0 = ty * tile
            c0 = tx * tile
            sl_h = min(tile, src.height - r0)
            sl_w = min(tile, src.width - c0)
            read_r0 = r0 - pad
            read_c0 = c0 - pad
            read_h = sl_h + 2 * pad
            read_w = sl_w + 2 * pad
            window = src.read_window(read_r0, read_c0, read_h, read_w)[:, :, 0]
            filled = fill_nodata_array(window, nodata=effective_nodata, max_distance=pad)
            interior = filled[pad : pad + sl_h, pad : pad + sl_w]
            hwc = interior[:, :, np.newaxis]
            typed = cast_sampled(hwc.astype(np.float32, copy=False), src.dtype, nodata=effective_nodata)
            full = np.zeros((tile, tile, 1), dtype=src.dtype)
            if effective_nodata is not None:
                full[:] = effective_nodata
            full[:sl_h, :sl_w] = typed
            return full

        def tiles() -> Iterator[np.ndarray]:
            nonlocal done
            # Sequential: overlapping reads share the tile cache; fill is memory-bound.
            for coord in coords:
                full = _compute_tile(coord)
                ty, tx = coord
                sl_h = min(tile, src.height - ty * tile)
                sl_w = min(tile, src.width - tx * tile)
                done += sl_h * sl_w
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
            block_size=block_size,
            dtype=src.dtype,
            nodata=effective_nodata,
        )
    if on_progress is not None:
        on_progress(100.0, "fill-nodata complete")
