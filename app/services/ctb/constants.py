"""Compile-time and Cesium/CTB constants from cesium-terrain-builder."""

from __future__ import annotations

import math

# CMake TERRAIN_TILE_SIZE / TERRAIN_MASK_SIZE (src/config.hpp.in).
TERRAIN_TILE_SIZE = 65
TERRAIN_MASK_SIZE = 256

# ctb-tile CLI defaults when -t is omitted (tools/ctb-tile.cpp).
GEODETIC_DEFAULT_TILE_SIZE = 65
MERCATOR_DEFAULT_TILE_SIZE = 256

# MeshTiler.cpp / TerrainProvider.js
HEIGHTMAP_TERRAIN_QUALITY = 0.25
SEMI_MAJOR_AXIS = 6378137.0

# MeshTile.cpp horizon / ECEF
RADIUS_X = 6378137.0
RADIUS_Y = 6378137.0
RADIUS_Z = 6356752.3142451793
WGS84_E2 = 0.0066943799901975848
SHORT_MAX = 32767.0
# CTB spelling: 65636, not 65536.
BYTESPLIT = 65636

# GDALTiler.cpp: dest NODATA when the source band has none.
DEFAULT_WARP_NODATA = -32768.0

# Heightmap-1.0: (meters + 1000) * 5 as uint16.
HEIGHTMAP_OFFSET_M = 1000.0
HEIGHTMAP_SCALE = 5.0

# HeightFieldChunker.hpp: extra lattice for zoom <= 6 (CTB compatibility).
SMOOTH_SMALL_ZOOM_MAX = 6
SMOOTH_SMALL_ZOOM_DIVISOR = 16

CHILD_SW = 1
CHILD_SE = 2
CHILD_NW = 4
CHILD_NE = 8

# MeshTile.cpp quantized-mesh vertex-normal extension.
EXTENSION_OCT_VERTEX_NORMALS = 1

GZIP_COMPRESSLEVEL = 6  # zlib Z_DEFAULT_COMPRESSION used by gzopen("wb")

WEB_MERCATOR_ORIGIN = math.pi * SEMI_MAJOR_AXIS
