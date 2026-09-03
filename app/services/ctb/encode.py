"""quantized-mesh-1.0 and heightmap-1.0 writers (CTB MeshTile.cpp / TerrainTile.cpp)."""

from __future__ import annotations

import gzip
import math
import struct
from dataclasses import dataclass

import numpy as np

from app.services.ctb.constants import (
    BYTESPLIT,
    CHILD_NE,
    CHILD_NW,
    CHILD_SE,
    CHILD_SW,
    EXTENSION_OCT_VERTEX_NORMALS,
    GZIP_COMPRESSLEVEL,
    HEIGHTMAP_OFFSET_M,
    HEIGHTMAP_SCALE,
    RADIUS_X,
    RADIUS_Y,
    RADIUS_Z,
    SHORT_MAX,
    WGS84_E2,
)


def gzip_terrain(payload: bytes) -> bytes:
    """Gzip a terrain payload (stdlib; no native zlib). mtime=0 keeps headers stable."""
    return gzip.compress(payload, compresslevel=GZIP_COMPRESSLEVEL, mtime=0)


def cpp_round(value: float) -> int:
    """C++ ``std::round`` (half away from zero)."""
    if value >= 0.0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def zigzag_encode(n: int) -> int:
    """MeshTile.cpp: ``(n << 1) ^ (n >> 31)`` on signed 32-bit."""
    n32 = np.int32(n)
    return int(np.uint16((n32 << 1) ^ (n32 >> 31)))


def quantize_index(origin: float, factor: float, value: float) -> int:
    return cpp_round((value - origin) * factor)


@dataclass(slots=True)
class Vec3:
    x: float
    y: float
    z: float

    def __getitem__(self, index: int) -> float:
        if index == 0:
            return self.x
        if index == 1:
            return self.y
        return self.z

    def __add__(self, other: Vec3) -> Vec3:
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: Vec3) -> Vec3:
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, scalar: float) -> Vec3:
        return Vec3(self.x * scalar, self.y * scalar, self.z * scalar)

    def dot(self, other: Vec3) -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other: Vec3) -> Vec3:
        return Vec3(
            self.y * other.z - other.y * self.z,
            self.z * other.x - other.z * self.x,
            self.x * other.y - other.x * self.y,
        )

    def magnitude_squared(self) -> float:
        return self.x * self.x + self.y * self.y + self.z * self.z

    def magnitude(self) -> float:
        return math.sqrt(self.magnitude_squared())

    def normalize(self) -> Vec3:
        mag = self.magnitude()
        if mag == 0.0:
            return Vec3(0.0, 0.0, 0.0)
        return Vec3(self.x / mag, self.y / mag, self.z / mag)


def llh_ecef_n(lat_rad: float) -> float:
    snx = math.sin(lat_rad)
    return RADIUS_X / math.sqrt(1.0 - WGS84_E2 * (snx * snx))


def llh_to_ecef(lon_deg: float, lat_deg: float, alt: float) -> Vec3:
    lon = lon_deg * (math.pi / 180.0)
    lat = lat_deg * (math.pi / 180.0)
    n = llh_ecef_n(lat)
    x = (n + alt) * math.cos(lat) * math.cos(lon)
    y = (n + alt) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - WGS84_E2) + alt) * math.sin(lat)
    return Vec3(x, y, z)


def ocp_compute_magnitude(position: Vec3, sphere_center: Vec3) -> float:
    magnitude_squared = position.magnitude_squared()
    magnitude = math.sqrt(magnitude_squared)
    direction = position * (1.0 / magnitude)
    magnitude_squared = max(1.0, magnitude_squared)
    magnitude = max(1.0, magnitude)
    cos_alpha = direction.dot(sphere_center)
    sin_alpha = direction.cross(sphere_center).magnitude()
    cos_beta = 1.0 / magnitude
    sin_beta = math.sqrt(magnitude_squared - 1.0) * cos_beta
    denom = cos_alpha * cos_beta - sin_alpha * sin_beta
    if denom == 0.0:
        return math.inf
    return 1.0 / denom


def bounding_sphere_from_points(points: list[Vec3]) -> tuple[Vec3, float]:
    """Ritter vs naive sphere; keep CTB's comparison direction (MeshTile BoundingSphere.hpp)."""
    inf = math.inf
    min_x = Vec3(inf, inf, inf)
    min_y = Vec3(inf, inf, inf)
    min_z = Vec3(inf, inf, inf)
    max_x = Vec3(-inf, -inf, -inf)
    max_y = Vec3(-inf, -inf, -inf)
    max_z = Vec3(-inf, -inf, -inf)
    for point in points:
        if point.x < min_x.x:
            min_x = point
        if point.y < min_y.y:
            min_y = point
        if point.z < min_z.z:
            min_z = point
        if point.x > max_x.x:
            max_x = point
        if point.y > max_y.y:
            max_y = point
        if point.z > max_z.z:
            max_z = point
    x_span = (max_x - min_x).magnitude_squared()
    y_span = (max_y - min_y).magnitude_squared()
    z_span = (max_z - min_z).magnitude_squared()
    diameter1, diameter2, max_span = min_x, max_x, x_span
    if y_span > max_span:
        diameter1, diameter2, max_span = min_y, max_y, y_span
    if z_span > max_span:
        diameter1, diameter2, max_span = min_z, max_z, z_span
    ritter_center = Vec3(
        (diameter1.x + diameter2.x) * 0.5,
        (diameter1.y + diameter2.y) * 0.5,
        (diameter1.z + diameter2.z) * 0.5,
    )
    radius_squared = (diameter2 - ritter_center).magnitude_squared()
    ritter_radius = math.sqrt(radius_squared)
    min_box = Vec3(min_x.x, min_y.y, min_z.z)
    max_box = Vec3(max_x.x, max_y.y, max_z.z)
    naive_center = (min_box + max_box) * 0.5
    naive_radius = 0.0
    for point in points:
        radius = (point - naive_center).magnitude()
        if radius > naive_radius:
            naive_radius = radius
        old_center_to_point_sq = (point - ritter_center).magnitude_squared()
        if old_center_to_point_sq > radius_squared:
            old_center_to_point = math.sqrt(old_center_to_point_sq)
            ritter_radius = (ritter_radius + old_center_to_point) * 0.5
            old_to_new = old_center_to_point - ritter_radius
            ritter_center = Vec3(
                (ritter_radius * ritter_center.x + old_to_new * point.x) / old_center_to_point,
                (ritter_radius * ritter_center.y + old_to_new * point.y) / old_center_to_point,
                (ritter_radius * ritter_center.z + old_to_new * point.z) / old_center_to_point,
            )
            radius_squared = ritter_radius * ritter_radius
    # CTB comment says "keep naive if smaller" but the branches are swapped.
    if naive_radius < ritter_radius:
        return ritter_center, ritter_radius
    return naive_center, naive_radius


def bounding_box_from_points(points: list[tuple[float, float, float]]) -> tuple[Vec3, Vec3]:
    min_v = Vec3(math.inf, math.inf, math.inf)
    max_v = Vec3(-math.inf, -math.inf, -math.inf)
    for x, y, z in points:
        if x < min_v.x:
            min_v.x = x
        if y < min_v.y:
            min_v.y = y
        if z < min_v.z:
            min_v.z = z
        if x > max_v.x:
            max_v.x = x
        if y > max_v.y:
            max_v.y = y
        if z > max_v.z:
            max_v.z = z
    return min_v, max_v


def ocp_from_points(points: list[Vec3], sphere_center: Vec3) -> Vec3:
    rx, ry, rz = 1.0 / RADIUS_X, 1.0 / RADIUS_Y, 1.0 / RADIUS_Z
    scaled_center = Vec3(sphere_center.x * rx, sphere_center.y * ry, sphere_center.z * rz)
    max_magnitude = -math.inf
    for point in points:
        scaled = Vec3(point.x * rx, point.y * ry, point.z * rz)
        magnitude = ocp_compute_magnitude(scaled, scaled_center)
        if magnitude > max_magnitude:
            max_magnitude = magnitude
    return scaled_center * max_magnitude


def clamp_value(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def snorm_value(value: float, range_max: float = 255.0) -> int:
    return int(cpp_round((clamp_value(value, -1.0, 1.0) * 0.5 + 0.5) * range_max)) & 0xFF


def oct_encode(vector: Vec3, range_max: float = 255.0) -> tuple[int, int]:
    llnorm = abs(vector.x) + abs(vector.y) + abs(vector.z)
    if llnorm == 0.0:
        return snorm_value(0.0, range_max), snorm_value(0.0, range_max)
    temp_x = vector.x / llnorm
    temp_y = vector.y / llnorm
    if vector.z < 0:
        x, y = temp_x, temp_y
        temp_x = (1.0 - abs(y)) * (-1.0 if x < 0.0 else 1.0)
        temp_y = (1.0 - abs(x)) * (-1.0 if y < 0.0 else 1.0)
    return snorm_value(temp_x, range_max), snorm_value(temp_y, range_max)


def triangle_area(a: Vec3, b: Vec3) -> float:
    i = (a[1] * b[2] - a[2] * b[1]) ** 2
    j = (a[2] * b[0] - a[0] * b[2]) ** 2
    k = (a[0] * b[1] - a[1] * b[0]) ** 2
    return 0.5 * math.sqrt(i + j + k)


def _write_edge_indices(buf: bytearray, vertices: list[tuple[float, float, float]], indices: list[int], edge_coord: float, component: int, wide: bool) -> None:
    seen: dict[int, int] = {}
    edge: list[int] = []
    for i, indice in enumerate(indices):
        val = vertices[indice][component]
        if val == edge_coord and indice not in seen:
            seen[indice] = i
            edge.append(indice)
    buf.extend(struct.pack("<i", len(edge)))
    fmt = "<I" if wide else "<H"
    for indice in edge:
        buf.extend(struct.pack(fmt, indice if wide else indice & 0xFFFF))


def encode_quantized_mesh(
    vertices: list[tuple[float, float, float]],
    indices: list[int],
    *,
    write_vertex_normals: bool,
) -> bytes:
    cartesian = [llh_to_ecef(v[0], v[1], v[2]) for v in vertices]
    sphere_center, sphere_radius = bounding_sphere_from_points(cartesian)
    cart_min, cart_max = bounding_box_from_points([(v.x, v.y, v.z) for v in cartesian])
    bounds_min, bounds_max = bounding_box_from_points(vertices)
    buf = bytearray()
    center_x = cart_min.x + 0.5 * (cart_max.x - cart_min.x)
    center_y = cart_min.y + 0.5 * (cart_max.y - cart_min.y)
    center_z = cart_min.z + 0.5 * (cart_max.z - cart_min.z)
    buf.extend(struct.pack("<ddd", center_x, center_y, center_z))
    buf.extend(struct.pack("<ff", float(bounds_min.z), float(bounds_max.z)))
    buf.extend(struct.pack("<dddd", sphere_center.x, sphere_center.y, sphere_center.z, sphere_radius))
    horizon = ocp_from_points(cartesian, sphere_center)
    buf.extend(struct.pack("<ddd", horizon.x, horizon.y, horizon.z))

    vertex_count = len(vertices)
    buf.extend(struct.pack("<i", vertex_count))
    origin = (bounds_min.x, bounds_min.y, bounds_min.z)
    span = (
        bounds_max.x - bounds_min.x,
        bounds_max.y - bounds_min.y,
        bounds_max.z - bounds_min.z,
    )
    for component in range(3):
        factor = SHORT_MAX / span[component] if span[component] > 0 else 0.0
        u0 = quantize_index(origin[component], factor, vertices[0][component])
        buf.extend(struct.pack("<H", zigzag_encode(u0)))
        for i in range(1, vertex_count):
            u1 = quantize_index(origin[component], factor, vertices[i][component])
            buf.extend(struct.pack("<H", zigzag_encode(u1 - u0)))
            u0 = u1

    triangle_count = len(indices) // 3
    buf.extend(struct.pack("<i", triangle_count))
    wide = vertex_count > BYTESPLIT
    if wide:
        highest = 0
        for indice in indices:
            code = (highest - indice) & 0xFFFFFFFF
            buf.extend(struct.pack("<I", code))
            if code == 0:
                highest += 1
        _write_edge_indices(buf, vertices, indices, bounds_min.x, 0, True)
        _write_edge_indices(buf, vertices, indices, bounds_min.y, 1, True)
        _write_edge_indices(buf, vertices, indices, bounds_max.x, 0, True)
        _write_edge_indices(buf, vertices, indices, bounds_max.y, 1, True)
    else:
        highest = 0
        for indice in indices:
            code = (highest - indice) & 0xFFFF
            buf.extend(struct.pack("<H", code))
            if code == 0:
                highest += 1
        _write_edge_indices(buf, vertices, indices, bounds_min.x, 0, False)
        _write_edge_indices(buf, vertices, indices, bounds_min.y, 1, False)
        _write_edge_indices(buf, vertices, indices, bounds_max.x, 0, False)
        _write_edge_indices(buf, vertices, indices, bounds_max.y, 1, False)

    if write_vertex_normals and triangle_count > 0:
        buf.extend(struct.pack("<B", EXTENSION_OCT_VERTEX_NORMALS))
        buf.extend(struct.pack("<i", 2 * vertex_count))
        normals_vertex = [Vec3(0.0, 0.0, 0.0) for _ in range(vertex_count)]
        for j in range(triangle_count):
            i0 = indices[j * 3]
            i1 = indices[j * 3 + 1]
            i2 = indices[j * 3 + 2]
            v0, v1, v2 = cartesian[i0], cartesian[i1], cartesian[i2]
            normal = (v1 - v0).cross(v2 - v0)
            area = triangle_area(v0, v1)
            weighted = normal * area
            normals_vertex[i0] = normals_vertex[i0] + weighted
            normals_vertex[i1] = normals_vertex[i1] + weighted
            normals_vertex[i2] = normals_vertex[i2] + weighted
        for normal in normals_vertex:
            ox, oy = oct_encode(normal.normalize())
            buf.extend(struct.pack("<BB", ox, oy))
    return gzip_terrain(bytes(buf))


def encode_heightmap(heights: np.ndarray, children: int) -> bytes:
    """heightmap-1.0: uint16 heights + child flags + 1-byte land mask, gzipped."""
    flat = np.asarray(heights, dtype=np.float32).reshape(-1)
    values = (flat.astype(np.float64) + HEIGHTMAP_OFFSET_M) * HEIGHTMAP_SCALE
    if not np.isfinite(values).all() or np.any((values < 0) | (values > 65535)):
        raise ValueError("Heightmap elevation outside [-1000, 12107] metres; use Mesh")
    encoded = np.trunc(values).astype(np.uint16)
    buf = bytearray(encoded.astype("<u2").tobytes())
    buf.append(children & 0xFF)
    buf.append(0)  # land mask (Terrain::setIsLand)
    return gzip_terrain(bytes(buf))


def child_flags(
    dataset_bounds,
    tile_bounds,
    *,
    is_max_zoom: bool,
) -> int:
    if is_max_zoom:
        return 0
    if not dataset_bounds.overlaps(tile_bounds):
        return 0
    flags = 0
    if dataset_bounds.overlaps(tile_bounds.get_sw()):
        flags |= CHILD_SW
    if dataset_bounds.overlaps(tile_bounds.get_nw()):
        flags |= CHILD_NW
    if dataset_bounds.overlaps(tile_bounds.get_ne()):
        flags |= CHILD_NE
    if dataset_bounds.overlaps(tile_bounds.get_se()):
        flags |= CHILD_SE
    return flags
