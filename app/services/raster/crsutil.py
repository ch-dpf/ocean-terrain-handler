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

# IEEE-754 binary64 has 52 fraction bits. Midpoint bisection of a source-space
# edge cannot refine a float64 coordinate past this depth.
_MAX_EDGE_DEPTH = 53


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


def dest_sample_tolerance(pixel_x: float, pixel_y: float) -> float:
    """Dest-space chord error that cannot skip a destination sample.

    ``grid_dimension`` uses ``ceil(span / pixel)``. Error below half the dest
    pixel cannot change the covered pixel count, so densify stops there.
    """
    size = min(float(pixel_x), float(pixel_y))
    if size <= 0:
        raise RasterError("destination pixel size must be positive")
    return 0.5 * size


def _finite_pair(x: float, y: float) -> bool:
    return math.isfinite(x) and math.isfinite(y)


def _axis_aligned_corners(
    left: float,
    bottom: float,
    right: float,
    top: float,
) -> list[tuple[float, float]]:
    return [(left, bottom), (right, bottom), (right, top), (left, top)]


def _require_finite(tx: np.ndarray, ty: np.ndarray) -> None:
    if not np.all(np.isfinite(tx) & np.isfinite(ty)):
        raise RasterError("coordinate transform produced a non-finite point")


def _transform_pairs(
    transformer: Transformer,
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    xs = np.array([p[0] for p in points], dtype=np.float64)
    ys = np.array([p[1] for p in points], dtype=np.float64)
    tx, ty = transform_xy(transformer, xs, ys)
    _require_finite(tx, ty)
    return [(float(x), float(y)) for x, y in zip(tx.tolist(), ty.tolist())]


def _mercator_pair(source: CRS, target: CRS) -> bool:
    a, b = crs_epsg(source), crs_epsg(target)
    return a in {4326, 3857} and b in {4326, 3857} and a != b


def _refine_edges(
    transformer: Transformer,
    x0: np.ndarray,
    y0: np.ndarray,
    x1: np.ndarray,
    y1: np.ndarray,
    dest_abs_tol: float,
    depth: int,
) -> list[list[tuple[float, float]]]:
    """Split source-space edges until dest-space midpoint-to-chord error ≤ ``dest_abs_tol``."""
    count = int(x0.shape[0])
    if count == 0:
        return []
    mx = (x0 + x1) * 0.5
    my = (y0 + y1) * 0.5
    xs = np.concatenate([x0, x1, mx])
    ys = np.concatenate([y0, y1, my])
    tx, ty = transform_xy(transformer, xs, ys)
    _require_finite(tx, ty)
    t0x, t1x, tmx = tx[:count], tx[count : 2 * count], tx[2 * count :]
    t0y, t1y, tmy = ty[:count], ty[count : 2 * count], ty[2 * count :]
    dist = np.hypot(tmx - 0.5 * (t0x + t1x), tmy - 0.5 * (t0y + t1y))
    done = dist <= dest_abs_tol
    if depth >= _MAX_EDGE_DEPTH and not bool(np.all(done)):
        raise RasterError(
            "CRS boundary densify exceeded float64 bisection without meeting dest pixel tolerance"
        )

    out: list[list[tuple[float, float]]] = [[] for _ in range(count)]
    for i in np.flatnonzero(done).tolist():
        out[i] = [
            (float(t0x[i]), float(t0y[i])),
            (float(t1x[i]), float(t1y[i])),
        ]
    need = np.flatnonzero(~done)
    if need.size:
        left = _refine_edges(
            transformer,
            x0[need],
            y0[need],
            mx[need],
            my[need],
            dest_abs_tol,
            depth + 1,
        )
        right = _refine_edges(
            transformer,
            mx[need],
            my[need],
            x1[need],
            y1[need],
            dest_abs_tol,
            depth + 1,
        )
        for local, edge_i in enumerate(need.tolist()):
            out[edge_i] = left[local][:-1] + right[local]
    return out


def _densify_ring(
    transformer: Transformer,
    corners: list[tuple[float, float]],
    dest_abs_tol: float,
) -> list[tuple[float, float]]:
    if dest_abs_tol <= 0:
        raise RasterError("dest_abs_tol must be positive")
    closed = corners + [corners[0]]
    x0 = np.array([p[0] for p in closed[:-1]], dtype=np.float64)
    y0 = np.array([p[1] for p in closed[:-1]], dtype=np.float64)
    x1 = np.array([p[0] for p in closed[1:]], dtype=np.float64)
    y1 = np.array([p[1] for p in closed[1:]], dtype=np.float64)
    edges = _refine_edges(transformer, x0, y0, x1, y1, dest_abs_tol, 0)
    points: list[tuple[float, float]] = []
    for i, edge in enumerate(edges):
        if i:
            edge = edge[1:]
        points.extend(edge)
    if points and points[0] != points[-1]:
        points.append(points[0])
    return points


def _transformed_boundary(
    source: CRS,
    target: CRS,
    bounds: tuple[float, float, float, float],
    *,
    dest_abs_tol: float | None,
) -> list[tuple[float, float]]:
    left, bottom, right, top = bounds
    corners = _axis_aligned_corners(left, bottom, right, top)
    if crs_equal(source, target):
        return corners + [corners[0]]
    transformer = make_transformer(source, target)
    closed = corners + [corners[0]]
    if _mercator_pair(source, target):
        return _transform_pairs(transformer, closed)
    if dest_abs_tol is None:
        raise RasterError("dest_abs_tol is required when densifying bounds between CRS")
    return _densify_ring(transformer, corners, dest_abs_tol)


def transform_bounds(
    source: CRS,
    target: CRS,
    bounds: tuple[float, float, float, float],
    *,
    dest_abs_tol: float | None = None,
) -> tuple[float, float, float, float]:
    """Return (left, bottom, right, top) in ``target`` CRS.

    Axis-aligned rectangles in EPSG:4326 or EPSG:3857 map to each other with
    extrema at the four corners (x is linear in longitude, y is monotonic in
    latitude). Other CRS pairs split each edge until dest-space midpoint-to-chord
    error is at most ``dest_abs_tol`` (half a destination pixel).
    """
    if crs_equal(source, target):
        left, bottom, right, top = bounds
        return left, bottom, right, top
    points = _transformed_boundary(source, target, bounds, dest_abs_tol=dest_abs_tol)
    finite = [(x, y) for x, y in points if _finite_pair(x, y)]
    if not finite:
        raise RasterError("Failed to transform raster bounds between CRS")
    xs = [p[0] for p in finite]
    ys = [p[1] for p in finite]
    return min(xs), min(ys), max(xs), max(ys)


def transform_ring(
    source: CRS,
    target: CRS,
    bounds: tuple[float, float, float, float],
    *,
    dest_abs_tol: float | None = None,
) -> list[list[float]]:
    """Closed boundary ring in ``target`` CRS."""
    points = _transformed_boundary(source, target, bounds, dest_abs_tol=dest_abs_tol)
    ring = [[x, y] for x, y in points if _finite_pair(x, y)]
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
    of that parallelogram occur at vertices. Using the minimum avoids undersampling.
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
    *,
    dest_abs_tol: float | None = None,
) -> list[float]:
    left, bottom, right, top = transform_bounds(
        crs, parse_crs("EPSG:4326"), bounds, dest_abs_tol=dest_abs_tol
    )
    west = min(max(left, -180.0), 180.0)
    east = min(max(right, -180.0), 180.0)
    south = min(max(bottom, -90.0), 90.0)
    north = min(max(top, -90.0), 90.0)
    return [west, south, east, north]
