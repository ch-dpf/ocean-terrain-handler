"""CRS parsing and bound transforms via pyproj (not GDAL)."""

from __future__ import annotations

import math

import numpy as np
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError

from app.services.raster.affine import Affine
from app.services.raster.errors import RasterError

# Web Mercator (EPSG:3857) uses spherical formulas with WGS84 semi-major axis.
WGS84_A = 6378137.0
EARTH_HALF = WGS84_A * math.pi
WEB_MERCATOR_MAX_LAT = math.degrees(math.atan(math.sinh(math.pi)))

# Samples per rectangle edge when densifying projected bounds (including endpoints).
# 1e-12 chord refine used to explode to 2^16 points/edge; DEM grid sizing does not
# need that. 21 samples (~5% of edge) is enough for UTM/TM AOIs of typical DEMs.
_EDGE_SAMPLES = 21


def parse_crs(value: str | CRS) -> CRS:
    if isinstance(value, CRS):
        return value
    try:
        return CRS.from_user_input(value)
    except CRSError as exc:
        raise RasterError(f"Unrecognized CRS: {value}") from exc


def crs_epsg(crs: CRS) -> int | None:
    code = crs.to_epsg()
    return int(code) if code is not None else None


def crs_equal(left: CRS, right: CRS) -> bool:
    if left.equals(right):
        return True
    a = crs_epsg(left)
    b = crs_epsg(right)
    return a is not None and a == b


def make_transformer(source: CRS, target: CRS) -> Transformer:
    return Transformer.from_crs(source, target, always_xy=True)


def transform_xy(
    transformer: Transformer,
    xs: np.ndarray,
    ys: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    try:
        out_x, out_y = transformer.transform(xs, ys, errcheck=False)
    except ProjError:
        out_x = np.full_like(xs, np.nan, dtype=np.float64)
        out_y = np.full_like(ys, np.nan, dtype=np.float64)
        return out_x, out_y
    out_x = np.asarray(out_x, dtype=np.float64)
    out_y = np.asarray(out_y, dtype=np.float64)
    return out_x, out_y


def _rect_edge_xy(
    left: float,
    bottom: float,
    right: float,
    top: float,
    samples: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Coordinates along the four edges of an axis-aligned rectangle (duplicates at corners)."""
    count = max(2, int(samples))
    t = np.linspace(0.0, 1.0, count, dtype=np.float64)
    xs = np.concatenate(
        [
            left + t * (right - left),
            np.full(count, right, dtype=np.float64),
            right + t * (left - right),
            np.full(count, left, dtype=np.float64),
        ]
    )
    ys = np.concatenate(
        [
            np.full(count, bottom, dtype=np.float64),
            bottom + t * (top - bottom),
            np.full(count, top, dtype=np.float64),
            top + t * (bottom - top),
        ]
    )
    return xs, ys


def _aabb_from_xy(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float, float]:
    finite = np.isfinite(xs) & np.isfinite(ys)
    if not np.any(finite):
        raise RasterError("Failed to transform raster bounds between CRS")
    return (
        float(np.min(xs[finite])),
        float(np.min(ys[finite])),
        float(np.max(xs[finite])),
        float(np.max(ys[finite])),
    )


def _mercator_pair(source: CRS, target: CRS) -> bool:
    a, b = crs_epsg(source), crs_epsg(target)
    return a in {4326, 3857} and b in {4326, 3857} and a != b


def transform_bounds(
    source: CRS,
    target: CRS,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Return (left, bottom, right, top) in ``target`` CRS.

    Axis-aligned rectangles in EPSG:4326 or EPSG:3857 map to each other with
    extrema at the four corners (x is linear in longitude, y is monotonic in
    latitude). Other CRS pairs sample each edge a fixed number of times.
    """
    left, bottom, right, top = bounds
    if crs_equal(source, target):
        return left, bottom, right, top
    transformer = make_transformer(source, target)
    if _mercator_pair(source, target):
        xs, ys = _rect_edge_xy(left, bottom, right, top, 2)
    else:
        xs, ys = _rect_edge_xy(left, bottom, right, top, _EDGE_SAMPLES)
    tx, ty = transform_xy(transformer, xs, ys)
    return _aabb_from_xy(tx, ty)


def transform_ring(
    source: CRS,
    target: CRS,
    bounds: tuple[float, float, float, float],
) -> list[list[float]]:
    """Closed boundary ring in ``target`` CRS."""
    left, bottom, right, top = bounds
    if crs_equal(source, target):
        return [[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]
    transformer = make_transformer(source, target)
    samples = 2 if _mercator_pair(source, target) else _EDGE_SAMPLES
    xs, ys = _rect_edge_xy(left, bottom, right, top, samples)
    tx, ty = transform_xy(transformer, xs, ys)
    ring = [[float(x), float(y)] for x, y in zip(tx, ty) if math.isfinite(x) and math.isfinite(y)]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def clip_mercator_lat(lat: float) -> float:
    return min(max(lat, -WEB_MERCATOR_MAX_LAT), WEB_MERCATOR_MAX_LAT)


def destination_pixel_size(
    src_crs: CRS,
    dst_crs: CRS,
    src_affine: Affine,
    width: int,
    height: int,
) -> tuple[float, float]:
    """Dest-CRS size of one source pixel, taking the finest of the four corners.

    An affine maps a rectangle to a parallelogram, so latitude/longitude extrema
    (and typically GSD extrema under common map projections) occur at vertices.
    Using the minimum avoids undersampling; it is not a center-pixel guess.
    """
    corners = (
        (0.0, 0.0),
        (float(width), 0.0),
        (float(width), float(height)),
        (0.0, float(height)),
    )
    dxs: list[float] = []
    dys: list[float] = []
    transformer = None if crs_equal(src_crs, dst_crs) else make_transformer(src_crs, dst_crs)
    for col, row in corners:
        x0, y0 = src_affine.xy(col, row)
        x1, y1 = src_affine.xy(col + 1.0, row)
        x2, y2 = src_affine.xy(col, row + 1.0)
        xs = np.array([x0, x1, x2], dtype=np.float64)
        ys = np.array([y0, y1, y2], dtype=np.float64)
        if transformer is None:
            tx, ty = xs, ys
        else:
            tx, ty = transform_xy(transformer, xs, ys)
        if not np.all(np.isfinite(tx) & np.isfinite(ty)):
            continue
        dxs.append(math.hypot(float(tx[1] - tx[0]), float(ty[1] - ty[0])))
        dys.append(math.hypot(float(tx[2] - tx[0]), float(ty[2] - ty[0])))
    if not dxs or not dys:
        raise RasterError("Failed to measure destination pixel size")
    return min(dxs), min(dys)


def grid_dimension(span: float, pixel_size: float) -> int:
    """Pixel count covering ``span`` at ``pixel_size`` without undersampling."""
    if span <= 0 or pixel_size <= 0:
        raise RasterError("Destination span and pixel size must be positive")
    return max(1, math.ceil(span / pixel_size - 1e-12))


def wgs84_bounds_from_rect(
    crs: CRS,
    bounds: tuple[float, float, float, float],
) -> list[float]:
    left, bottom, right, top = transform_bounds(crs, parse_crs("EPSG:4326"), bounds)
    west = min(max(left, -180.0), 180.0)
    east = min(max(right, -180.0), 180.0)
    south = min(max(bottom, -90.0), 90.0)
    north = min(max(top, -90.0), 90.0)
    return [west, south, east, north]
