"""Inverse-map warp from a GeoTIFF onto a destination pixel grid."""

from __future__ import annotations

import numpy as np
from pyproj import CRS, Transformer

from app.services.raster.affine import Affine
from app.services.raster.crsutil import crs_equal, make_transformer, transform_xy
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.nodata import nodata_mask
from app.services.raster.resample import (
    AGGREGATE_RESAMPLING,
    RESAMPLE_CUBIC,
    RESAMPLE_CUBICSPLINE,
    RESAMPLE_LANCZOS,
    cast_sampled,
    normalize_resampling,
    remap_array,
    sample_footprint,
)


def _pad_for_method(method: str) -> int:
    kind = normalize_resampling(method)
    if kind == RESAMPLE_LANCZOS:
        return 3
    if kind in {RESAMPLE_CUBIC, RESAMPLE_CUBICSPLINE}:
        return 2
    return 1


def _north_up_colrow_maps(
    src_affine: Affine,
    dst_affine: Affine,
    dst_row0: int,
    dst_col0: int,
    dst_h: int,
    dst_w: int,
) -> tuple[np.ndarray, np.ndarray]:
    col_1d = (
        dst_affine.a * (np.arange(dst_w, dtype=np.float64) + dst_col0 + 0.5) + dst_affine.c - src_affine.c
    ) / src_affine.a
    row_1d = (
        dst_affine.e * (np.arange(dst_h, dtype=np.float64) + dst_row0 + 0.5) + dst_affine.f - src_affine.f
    ) / src_affine.e
    return np.meshgrid(col_1d, row_1d)


def _src_colrow_maps(
    src: GeoTiffReader,
    dst_affine: Affine,
    dst_crs: CRS,
    dst_row0: int,
    dst_col0: int,
    dst_h: int,
    dst_w: int,
    transformer: Transformer | None,
) -> tuple[np.ndarray, np.ndarray]:
    if crs_equal(src.crs, dst_crs) and src.affine.is_north_up() and dst_affine.is_north_up():
        return _north_up_colrow_maps(src.affine, dst_affine, dst_row0, dst_col0, dst_h, dst_w)

    rows, cols = np.meshgrid(
        np.arange(dst_h, dtype=np.float64) + dst_row0 + 0.5,
        np.arange(dst_w, dtype=np.float64) + dst_col0 + 0.5,
        indexing="ij",
    )

    dst_x, dst_y = dst_affine.xy(cols, rows)
    if crs_equal(src.crs, dst_crs):
        src_x = np.asarray(dst_x, dtype=np.float64)
        src_y = np.asarray(dst_y, dtype=np.float64)
    else:
        xform = transformer or make_transformer(dst_crs, src.crs)
        src_x, src_y = transform_xy(xform, np.asarray(dst_x, dtype=np.float64), np.asarray(dst_y, dtype=np.float64))

    src_cols, src_rows = src.affine.colrow(src_x, src_y)
    return np.asarray(src_cols, dtype=np.float64), np.asarray(src_rows, dtype=np.float64)


def _src_colrow_corners(
    src: GeoTiffReader,
    dst_affine: Affine,
    dst_crs: CRS,
    dst_row0: int,
    dst_col0: int,
    dst_h: int,
    dst_w: int,
    transformer: Transformer | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Source PixelIsArea coordinates of destination-pixel corners (H+1, W+1)."""
    rows, cols = np.meshgrid(
        np.arange(dst_h + 1, dtype=np.float64) + dst_row0,
        np.arange(dst_w + 1, dtype=np.float64) + dst_col0,
        indexing="ij",
    )
    dst_x, dst_y = dst_affine.xy(cols, rows)
    if crs_equal(src.crs, dst_crs):
        src_x = np.asarray(dst_x, dtype=np.float64)
        src_y = np.asarray(dst_y, dtype=np.float64)
    else:
        xform = transformer or make_transformer(dst_crs, src.crs)
        src_x, src_y = transform_xy(xform, np.asarray(dst_x, dtype=np.float64), np.asarray(dst_y, dtype=np.float64))
    src_cols, src_rows = src.affine.colrow(src_x, src_y)
    return np.asarray(src_cols, dtype=np.float64), np.asarray(src_rows, dtype=np.float64)


def _prepare_source_window(
    src: GeoTiffReader,
    dst_affine: Affine,
    dst_crs: CRS,
    dst_row0: int,
    dst_col0: int,
    dst_h: int,
    dst_w: int,
    resampling: str,
    transformer: Transformer | None,
    band: int,
    effective_nodata: float | None,
    *,
    extra_rows: int = 0,
    extra_cols: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float, float, int, int] | None:
    src_cols, src_rows = _src_colrow_maps(
        src, dst_affine, dst_crs, dst_row0, dst_col0, dst_h + extra_rows, dst_w + extra_cols, transformer
    )
    finite = np.isfinite(src_cols) & np.isfinite(src_rows)
    if not np.any(finite):
        return None
    pad = _pad_for_method(resampling)
    finite_rows = src_rows[finite]
    finite_cols = src_cols[finite]
    level = src.select_level(
        float(finite_cols.max() - finite_cols.min()),
        float(finite_rows.max() - finite_rows.min()),
        dst_w,
        dst_h,
    )
    if level.width != src.width or level.height != src.height:
        sx = level.width / src.width
        sy = level.height / src.height
        src_cols = src_cols * sx
        src_rows = src_rows * sy
        finite = np.isfinite(src_cols) & np.isfinite(src_rows)
        finite_rows = src_rows[finite]
        finite_cols = src_cols[finite]
    rmin = int(np.floor(finite_rows.min())) - pad
    rmax = int(np.ceil(finite_rows.max())) + pad + 1
    cmin = int(np.floor(finite_cols.min())) - pad
    cmax = int(np.ceil(finite_cols.max())) + pad + 1
    win_h = max(1, rmax - rmin)
    win_w = max(1, cmax - cmin)
    window = src.read_window(rmin, cmin, win_h, win_w, level=level)
    band_i = min(max(band, 0), window.shape[2] - 1)
    elevation = window[:, :, band_i].astype(np.float32, copy=True)
    invalid = nodata_mask(elevation, effective_nodata)
    elevation[invalid] = np.nan
    return elevation, src_cols, src_rows, finite, float(cmin), float(rmin), level.width, level.height


def warp_window(
    src: GeoTiffReader,
    dst_affine: Affine,
    dst_crs: CRS,
    dst_row0: int,
    dst_col0: int,
    dst_h: int,
    dst_w: int,
    resampling: str,
    *,
    nodata: float | None = None,
    transformer: Transformer | None = None,
    band: int = 0,
) -> np.ndarray:
    """Warp a destination window to HWC, preserving elevation dtype semantics.

    Out-of-bounds and source NODATA pixels are written as ``nodata`` (or 0).
    """
    effective_nodata = src.nodata if nodata is None else nodata
    empty = np.zeros((dst_h, dst_w, 1), dtype=np.float32)
    if effective_nodata is not None:
        empty[:] = effective_nodata
    kind = normalize_resampling(resampling)

    if kind in AGGREGATE_RESAMPLING:
        src_cols, src_rows = _src_colrow_corners(
            src, dst_affine, dst_crs, dst_row0, dst_col0, dst_h, dst_w, transformer
        )
        finite = np.isfinite(src_cols) & np.isfinite(src_rows)
        if not np.any(finite):
            return cast_sampled(empty, src.dtype, nodata=effective_nodata)
        finite_rows = src_rows[finite]
        finite_cols = src_cols[finite]
        level = src.select_level(
            float(finite_cols.max() - finite_cols.min()),
            float(finite_rows.max() - finite_rows.min()),
            dst_w,
            dst_h,
        )
        if level.width != src.width or level.height != src.height:
            src_cols = src_cols * (level.width / src.width)
            src_rows = src_rows * (level.height / src.height)
            finite = np.isfinite(src_cols) & np.isfinite(src_rows)
            finite_rows = src_rows[finite]
            finite_cols = src_cols[finite]
        pad = _pad_for_method(resampling)
        rmin = int(np.floor(finite_rows.min())) - pad
        rmax = int(np.ceil(finite_rows.max())) + pad + 1
        cmin = int(np.floor(finite_cols.min())) - pad
        cmax = int(np.ceil(finite_cols.max())) + pad + 1
        window = src.read_window(rmin, cmin, max(1, rmax - rmin), max(1, cmax - cmin), level=level)
        band_i = min(max(band, 0), window.shape[2] - 1)
        elevation = window[:, :, band_i].astype(np.float32, copy=True)
        elevation[nodata_mask(elevation, effective_nodata)] = np.nan
        sampled = sample_footprint(
            elevation,
            src_rows - rmin,
            src_cols - cmin,
            kind,
            nodata=np.nan,
        )
        valid = np.isfinite(sampled)
        if effective_nodata is None:
            sampled = np.where(valid, sampled, 0.0)
        else:
            sampled = np.where(valid, sampled, np.float32(effective_nodata))
        return cast_sampled(sampled[:, :, np.newaxis], src.dtype, nodata=effective_nodata)

    src_cols, src_rows = _src_colrow_maps(
        src, dst_affine, dst_crs, dst_row0, dst_col0, dst_h, dst_w, transformer
    )
    finite = np.isfinite(src_cols) & np.isfinite(src_rows)
    inside = finite & (src_cols >= 0) & (src_cols < src.width) & (src_rows >= 0) & (src_rows < src.height)
    if not np.any(inside):
        return cast_sampled(empty, src.dtype, nodata=effective_nodata)

    prepared = _prepare_source_window(
        src,
        dst_affine,
        dst_crs,
        dst_row0,
        dst_col0,
        dst_h,
        dst_w,
        resampling,
        transformer,
        band,
        effective_nodata,
    )
    if prepared is None:
        return cast_sampled(empty, src.dtype, nodata=effective_nodata)
    elevation, src_cols, src_rows, finite, cmin, rmin, level_w, level_h = prepared
    inside = finite & (src_cols >= 0) & (src_cols < level_w) & (src_rows >= 0) & (src_rows < level_h)
    rel_x = src_cols - cmin
    rel_y = src_rows - rmin
    sampled = remap_array(elevation[:, :, np.newaxis], rel_x, rel_y, resampling)[:, :, 0]
    valid = inside & np.isfinite(sampled)
    if effective_nodata is None:
        sampled = np.where(valid, sampled, 0.0)
    else:
        sampled = np.where(valid, sampled, np.float32(effective_nodata))
    return cast_sampled(sampled[:, :, np.newaxis], src.dtype, nodata=effective_nodata)
