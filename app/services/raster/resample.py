"""Resampling helpers implemented in numpy / Pillow (no GDAL)."""

from __future__ import annotations

import numpy as np
from PIL import Image

from app.schemas import ResamplingMethod
from app.services.raster.nodata import nodata_mask

try:
    import cv2
except ImportError:  # pragma: no cover - optional acceleration
    cv2 = None

RESAMPLE_NEAREST = "nearest"
RESAMPLE_BILINEAR = "bilinear"
RESAMPLE_CUBIC = "cubic"
RESAMPLE_LANCZOS = "lanczos"
RESAMPLE_AVERAGE = "average"
RESAMPLE_MODE = "mode"

# Names that denote the same kernel, not approximations of a different kernel.
_METHOD_ALIASES = {
    "near": RESAMPLE_NEAREST,
    "nearest": RESAMPLE_NEAREST,
    "bilinear": RESAMPLE_BILINEAR,
    "cubic": RESAMPLE_CUBIC,
    "lanczos": RESAMPLE_LANCZOS,
    "average": RESAMPLE_AVERAGE,
    "mode": RESAMPLE_MODE,
    "box": RESAMPLE_AVERAGE,
}

PIL_RESAMPLING = {
    RESAMPLE_NEAREST: Image.Resampling.NEAREST,
    RESAMPLE_BILINEAR: Image.Resampling.BILINEAR,
    RESAMPLE_CUBIC: Image.Resampling.BICUBIC,
    RESAMPLE_LANCZOS: Image.Resampling.LANCZOS,
    RESAMPLE_AVERAGE: Image.Resampling.BOX,
}

_CV2_INTER = None if cv2 is None else {
    RESAMPLE_NEAREST: cv2.INTER_NEAREST,
    RESAMPLE_BILINEAR: cv2.INTER_LINEAR,
    RESAMPLE_CUBIC: cv2.INTER_CUBIC,
    RESAMPLE_LANCZOS: cv2.INTER_LANCZOS4,
}


def normalize_resampling(method: str | ResamplingMethod) -> str:
    key = method.value if isinstance(method, ResamplingMethod) else str(method)
    lower = key.lower()
    if lower == "cubicspline":
        raise ValueError("cubicspline is not cubic convolution; this engine does not implement it")
    if lower == "antialias":
        raise ValueError("antialias is not lanczos; this engine does not implement it")
    mapped = _METHOD_ALIASES.get(lower)
    if mapped is None:
        raise ValueError(f"unsupported resampling method: {key}")
    return mapped


def to_uint8(array: np.ndarray) -> np.ndarray:
    if array.dtype == np.uint8:
        return array
    if np.issubdtype(array.dtype, np.floating):
        scale = 255.0 if float(np.nanmax(array) if array.size else 0.0) <= 1.0 else 1.0
        return np.clip(np.rint(array * scale), 0, 255).astype(np.uint8)
    if array.dtype == np.uint16:
        return (array / 257).astype(np.uint8)
    return np.clip(array, 0, 255).astype(np.uint8)


def array_to_image(array: np.ndarray) -> Image.Image:
    array = to_uint8(array)
    if array.ndim == 2:
        return Image.fromarray(array, mode="L")
    bands = array.shape[2]
    if bands == 1:
        return Image.fromarray(array[:, :, 0], mode="L")
    if bands == 2:
        return Image.fromarray(array, mode="LA")
    if bands == 3:
        return Image.fromarray(array, mode="RGB")
    return Image.fromarray(array[:, :, :4], mode="RGBA")


def image_to_array(image: Image.Image) -> np.ndarray:
    array = np.asarray(image)
    if array.ndim == 2:
        return array[:, :, np.newaxis]
    return array


def resize_array(array: np.ndarray, out_h: int, out_w: int, method: str) -> np.ndarray:
    if out_h <= 0 or out_w <= 0:
        raise ValueError("output size must be positive")
    if array.shape[0] == out_h and array.shape[1] == out_w:
        return to_uint8(array)
    kind = normalize_resampling(method)
    src = to_uint8(array)
    if cv2 is not None and _CV2_INTER is not None:
        hwc = np.ascontiguousarray(src[:, :, np.newaxis] if src.ndim == 2 else src)
        down = out_h < hwc.shape[0] or out_w < hwc.shape[1]
        if down and kind in {RESAMPLE_AVERAGE, RESAMPLE_BILINEAR}:
            interp = cv2.INTER_AREA
        elif kind in _CV2_INTER:
            interp = _CV2_INTER[kind]
        else:
            interp = None
        if interp is None:
            image = array_to_image(src)
            if kind not in PIL_RESAMPLING:
                raise ValueError(f"unsupported resampling method: {kind}")
            resized = image.resize((out_w, out_h), PIL_RESAMPLING[kind])
            return image_to_array(resized)
        if hwc.shape[2] == 1:
            out = cv2.resize(hwc[:, :, 0], (out_w, out_h), interpolation=interp)
            return out if src.ndim == 2 else out[:, :, np.newaxis]
        return cv2.resize(hwc, (out_w, out_h), interpolation=interp)
    image = array_to_image(src)
    if kind not in PIL_RESAMPLING:
        raise ValueError(f"unsupported resampling method: {kind}")
    resized = image.resize((out_w, out_h), PIL_RESAMPLING[kind])
    return image_to_array(resized)


def _ensure_hwc(src: np.ndarray) -> np.ndarray:
    if src.ndim == 2:
        return src[:, :, np.newaxis]
    return src


def sample_nearest(src: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    src = _ensure_hwc(src)
    height, width, bands = src.shape
    ri = np.rint(rows).astype(np.int32)
    ci = np.rint(cols).astype(np.int32)
    valid = (ri >= 0) & (ri < height) & (ci >= 0) & (ci < width)
    ri = np.clip(ri, 0, max(height - 1, 0))
    ci = np.clip(ci, 0, max(width - 1, 0))
    out = src[ri, ci].astype(np.float32, copy=True)
    out[~valid] = 0
    return out


def sample_bilinear(src: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    src = _ensure_hwc(src).astype(np.float32, copy=False)
    height, width, _bands = src.shape
    if height == 0 or width == 0:
        return np.zeros(rows.shape + (src.shape[2],), dtype=np.float32)
    r0 = np.floor(rows)
    c0 = np.floor(cols)
    dr = (rows - r0).astype(np.float32)
    dc = (cols - c0).astype(np.float32)
    r0i = r0.astype(np.int32)
    c0i = c0.astype(np.int32)
    r1i = r0i + 1
    c1i = c0i + 1
    valid = (rows >= -0.5) & (rows <= height - 0.5) & (cols >= -0.5) & (cols <= width - 0.5)
    r0c = np.clip(r0i, 0, height - 1)
    r1c = np.clip(r1i, 0, height - 1)
    c0c = np.clip(c0i, 0, width - 1)
    c1c = np.clip(c1i, 0, width - 1)
    s00 = src[r0c, c0c]
    s01 = src[r0c, c1c]
    s10 = src[r1c, c0c]
    s11 = src[r1c, c1c]
    drg = dr[..., np.newaxis]
    dcg = dc[..., np.newaxis]
    out = s00 * (1.0 - drg) * (1.0 - dcg) + s01 * (1.0 - drg) * dcg + s10 * drg * (1.0 - dcg) + s11 * drg * dcg
    out[~valid] = 0
    return out


def _cubic(x: np.ndarray) -> np.ndarray:
    ax = np.abs(x)
    ax2 = ax * ax
    ax3 = ax2 * ax
    a = -0.5
    p1 = ((a + 2.0) * ax3) - ((a + 3.0) * ax2) + 1.0
    p2 = (a * ax3) - (5.0 * a * ax2) + (8.0 * a * ax) - (4.0 * a)
    return np.where(ax <= 1.0, p1, np.where(ax < 2.0, p2, 0.0))


def sample_bicubic(src: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    src = _ensure_hwc(src).astype(np.float32, copy=False)
    height, width, bands = src.shape
    valid = (rows >= 0) & (rows <= height - 1) & (cols >= 0) & (cols <= width - 1)
    r_base = np.floor(rows).astype(np.int32)
    c_base = np.floor(cols).astype(np.int32)
    acc = np.zeros(rows.shape + (bands,), dtype=np.float32)
    for i in range(-1, 3):
        wr = _cubic(rows - (r_base + i).astype(np.float32))
        ri = np.clip(r_base + i, 0, height - 1)
        for j in range(-1, 3):
            wc = _cubic(cols - (c_base + j).astype(np.float32))
            ci = np.clip(c_base + j, 0, width - 1)
            weight = (wr * wc)[..., np.newaxis]
            acc += src[ri, ci] * weight
    acc[~valid] = 0
    return acc


def _lanczos3(x: np.ndarray) -> np.ndarray:
    ax = np.abs(x)
    out = np.zeros_like(ax, dtype=np.float32)
    inside = ax < 3.0
    px = ax[inside] * np.pi
    sinc = np.ones_like(px)
    nonzero = px != 0
    sinc[nonzero] = np.sin(px[nonzero]) / px[nonzero]
    px3 = px / 3.0
    sinc3 = np.ones_like(px3)
    nonzero3 = px3 != 0
    sinc3[nonzero3] = np.sin(px3[nonzero3]) / px3[nonzero3]
    out[inside] = sinc * sinc3
    return out


def sample_lanczos(src: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    src = _ensure_hwc(src).astype(np.float32, copy=False)
    height, width, bands = src.shape
    valid = (rows >= 0) & (rows <= height - 1) & (cols >= 0) & (cols <= width - 1)
    r_base = np.floor(rows).astype(np.int32)
    c_base = np.floor(cols).astype(np.int32)
    acc = np.zeros(rows.shape + (bands,), dtype=np.float32)
    wsum = np.zeros(rows.shape + (1,), dtype=np.float32)
    for i in range(-2, 4):
        wr = _lanczos3(rows - (r_base + i).astype(np.float32))
        ri = np.clip(r_base + i, 0, height - 1)
        for j in range(-2, 4):
            wc = _lanczos3(cols - (c_base + j).astype(np.float32))
            ci = np.clip(c_base + j, 0, width - 1)
            weight = (wr * wc)[..., np.newaxis]
            acc += src[ri, ci] * weight
            wsum += weight
    acc = np.divide(acc, wsum, out=np.zeros_like(acc), where=wsum > 0)
    acc[~valid] = 0
    return acc


def sample_image(src: np.ndarray, rows: np.ndarray, cols: np.ndarray, method: str) -> np.ndarray:
    kind = normalize_resampling(method)
    if kind == RESAMPLE_NEAREST:
        return sample_nearest(src, rows, cols)
    if kind == RESAMPLE_CUBIC:
        return sample_bicubic(src, rows, cols)
    if kind == RESAMPLE_LANCZOS:
        return sample_lanczos(src, rows, cols)
    if kind == RESAMPLE_BILINEAR:
        return sample_bilinear(src, rows, cols)
    raise ValueError(f"resampling {kind!r} is not implemented for inverse-map sampling")


def upsample2d(array: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """Bilinear-upsample a 2D float grid to (out_h, out_w)."""
    src = np.ascontiguousarray(array.astype(np.float32, copy=False))
    if src.shape[0] == out_h and src.shape[1] == out_w:
        return src
    if cv2 is not None:
        return cv2.resize(src, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    ys = (np.arange(out_h, dtype=np.float64) + 0.5) * src.shape[0] / out_h - 0.5
    xs = (np.arange(out_w, dtype=np.float64) + 0.5) * src.shape[1] / out_w - 0.5
    rows, cols = np.meshgrid(ys, xs, indexing="ij")
    sampled = sample_bilinear(src[:, :, np.newaxis], rows, cols)
    return sampled[:, :, 0]


def remap_array(src: np.ndarray, map_x: np.ndarray, map_y: np.ndarray, method: str) -> np.ndarray:
    """Sample ``src`` (HWC) at floating PixelIsArea coordinates; returns float32 HWC."""
    src_f = _ensure_hwc(src).astype(np.float32, copy=False)
    map_x32 = np.ascontiguousarray(map_x.astype(np.float32, copy=False))
    map_y32 = np.ascontiguousarray(map_y.astype(np.float32, copy=False))
    kind = normalize_resampling(method)
    if cv2 is not None and _CV2_INTER is not None and kind in _CV2_INTER and np.isfinite(src_f).all():
        map_x_cv = map_x32 - 0.5
        map_y_cv = map_y32 - 0.5
        interp = _CV2_INTER[kind]
        if src_f.shape[2] == 1:
            out = cv2.remap(
                src_f[:, :, 0],
                map_x_cv,
                map_y_cv,
                interpolation=interp,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            return out[:, :, np.newaxis]
        return cv2.remap(
            src_f,
            map_x_cv,
            map_y_cv,
            interpolation=interp,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    return sample_image(src_f, map_y32, map_x32, kind)


def average_downsample(
    array: np.ndarray,
    out_h: int,
    out_w: int,
    *,
    nodata: float | None = None,
) -> np.ndarray:
    """Block-average a 2D/HWC array to ``out_h``×``out_w``, masking NODATA."""
    src = _ensure_hwc(array).astype(np.float32, copy=True)
    if out_h <= 0 or out_w <= 0:
        raise ValueError("output size must be positive")
    if src.shape[0] == out_h and src.shape[1] == out_w:
        return src
    src_h, src_w, samples = src.shape
    factor_y = max(1, src_h // out_h)
    factor_x = max(1, src_w // out_w)
    for band in range(samples):
        src[:, :, band][nodata_mask(src[:, :, band], nodata)] = np.nan
    padded = np.full((out_h * factor_y, out_w * factor_x, samples), np.nan, dtype=np.float32)
    copy_h = min(src_h, padded.shape[0])
    copy_w = min(src_w, padded.shape[1])
    padded[:copy_h, :copy_w] = src[:copy_h, :copy_w]
    blocks = padded.reshape(out_h, factor_y, out_w, factor_x, samples)
    valid = np.isfinite(blocks)
    counts = valid.sum(axis=(1, 3), dtype=np.float32)
    sums = np.where(valid, blocks, 0.0).sum(axis=(1, 3), dtype=np.float32)
    fill = (
        np.float32("nan")
        if nodata is None or (isinstance(nodata, float) and np.isnan(nodata))
        else np.float32(nodata)
    )
    out = np.full(counts.shape, fill, dtype=np.float32)
    np.divide(sums, counts, out=out, where=counts > 0)
    return out


def cast_sampled(sampled: np.ndarray, dtype: np.dtype, *, nodata: float | None) -> np.ndarray:
    """Convert float samples back to ``dtype``, restoring NODATA."""
    invalid = ~np.isfinite(sampled)
    if nodata is not None and not (isinstance(nodata, float) and np.isnan(nodata)):
        invalid = invalid | nodata_mask(sampled, nodata)
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        fill = 0 if nodata is None or (isinstance(nodata, float) and np.isnan(nodata)) else nodata
        filled = np.where(invalid, fill, sampled)
        out = np.rint(filled).clip(info.min, info.max).astype(dtype)
        if nodata is not None and not (isinstance(nodata, float) and np.isnan(nodata)):
            out[invalid] = dtype.type(int(nodata))
        return out
    out = sampled.astype(dtype, copy=True)
    if nodata is not None:
        out[invalid] = nodata
    elif np.issubdtype(dtype, np.floating):
        out[invalid] = np.nan
    return out


def remap_image(src: np.ndarray, map_x: np.ndarray, map_y: np.ndarray, method: str) -> np.ndarray:
    """Sample ``src`` (HWC) at floating pixel coordinates (map_x, map_y) as uint8."""
    src_u8 = to_uint8(_ensure_hwc(src))
    map_x32 = np.ascontiguousarray(map_x.astype(np.float32, copy=False))
    map_y32 = np.ascontiguousarray(map_y.astype(np.float32, copy=False))
    kind = normalize_resampling(method)
    if cv2 is not None and _CV2_INTER is not None and kind in _CV2_INTER:
        # OpenCV treats (0, 0) as the center of the first pixel; our maps use PixelIsArea
        # coordinates where (0, 0) is the upper-left corner.
        map_x32 = map_x32 - 0.5
        map_y32 = map_y32 - 0.5
        interp = _CV2_INTER[kind]
        if src_u8.shape[2] == 1:
            out = cv2.remap(
                src_u8[:, :, 0],
                map_x32,
                map_y32,
                interpolation=interp,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            return out[:, :, np.newaxis]
        return cv2.remap(
            src_u8,
            map_x32,
            map_y32,
            interpolation=interp,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    sampled = sample_image(src_u8, map_y32, map_x32, kind)
    return np.clip(np.rint(sampled), 0, 255).astype(np.uint8)
