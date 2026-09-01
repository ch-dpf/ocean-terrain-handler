"""NODATA helpers shared by GeoTIFF IO, warp, fill, and overviews."""

from __future__ import annotations

import numpy as np


def parse_nodata_value(raw: object) -> float | None:
    """Parse a GDAL_NODATA tag payload into a float (NaN allowed)."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        text = raw.decode("ascii", errors="replace")
    else:
        text = str(raw)
    text = text.strip().strip("\x00").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"nan", "+nan", "-nan", "n/a"}:
        return float("nan")
    try:
        return float(text)
    except ValueError:
        return None


def format_nodata_tag(nodata: float) -> str:
    if isinstance(nodata, float) and np.isnan(nodata):
        return "nan"
    if float(nodata) == int(nodata) and abs(nodata) < 1e15:
        return str(int(nodata))
    return repr(float(nodata))


def nodata_mask(array: np.ndarray, nodata: float | None) -> np.ndarray:
    """Return True where pixels should be treated as NODATA."""
    if np.issubdtype(array.dtype, np.floating):
        invalid = ~np.isfinite(array)
    else:
        invalid = np.zeros(array.shape, dtype=bool)
    if nodata is None:
        return invalid
    if isinstance(nodata, float) and np.isnan(nodata):
        return invalid
    return invalid | (array == nodata)


def json_nodata(value: float | None) -> float | str | None:
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return "nan"
    return float(value)
