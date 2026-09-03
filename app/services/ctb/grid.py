"""TMS Global Geodetic / Mercator grids as in CTB ``Grid`` / ``GlobalGeodetic`` / ``GlobalMercator``."""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

from pyproj import CRS

from app.schemas import Profile
from app.services.ctb.constants import (
    GEODETIC_DEFAULT_TILE_SIZE,
    MERCATOR_DEFAULT_TILE_SIZE,
    WEB_MERCATOR_ORIGIN,
)


@dataclass(frozen=True, slots=True)
class TileCoordinate:
    zoom: int
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class CRSBounds:
    minx: float
    miny: float
    maxx: float
    maxy: float

    @property
    def width(self) -> float:
        return self.maxx - self.minx

    @property
    def height(self) -> float:
        return self.maxy - self.miny

    def lower_left(self) -> tuple[float, float]:
        return self.minx, self.miny

    def upper_right(self) -> tuple[float, float]:
        return self.maxx, self.maxy

    def overlaps(self, other: CRSBounds) -> bool:
        return (
            self.minx < other.maxx
            and other.minx < self.maxx
            and self.miny < other.maxy
            and other.miny < self.maxy
        )

    def get_sw(self) -> CRSBounds:
        return CRSBounds(
            self.minx,
            self.miny,
            self.minx + self.width / 2.0,
            self.miny + self.height / 2.0,
        )

    def get_nw(self) -> CRSBounds:
        return CRSBounds(
            self.minx,
            self.maxy - self.height / 2.0,
            self.minx + self.width / 2.0,
            self.maxy,
        )

    def get_ne(self) -> CRSBounds:
        return CRSBounds(
            self.maxx - self.width / 2.0,
            self.maxy - self.height / 2.0,
            self.maxx,
            self.maxy,
        )

    def get_se(self) -> CRSBounds:
        return CRSBounds(
            self.maxx - self.width / 2.0,
            self.miny,
            self.maxx,
            self.miny + self.height / 2.0,
        )


@dataclass(frozen=True, slots=True)
class TileBounds:
    minx: int
    miny: int
    maxx: int
    maxy: int

    @property
    def width(self) -> int:
        return self.maxx - self.minx

    @property
    def height(self) -> int:
        return self.maxy - self.miny


def _to_upixel(value: float) -> int:
    """C++ conversion of a non-negative double to unsigned pixel index (trunc toward 0)."""
    if not math.isfinite(value) or value <= 0.0:
        return 0
    return int(value)


class Grid:
    """CTB ``ctb::Grid`` (gdal2tiles / TMS)."""

    def __init__(
        self,
        tile_size: int,
        extent: CRSBounds,
        crs: CRS,
        root_tiles: int = 1,
        zoom_factor: float = 2.0,
    ) -> None:
        self.tile_size = int(tile_size)
        self.extent = extent
        self.crs = crs
        self.zoom_factor = float(zoom_factor)
        self.initial_resolution = (extent.width / root_tiles) / self.tile_size
        self.x_origin_shift = extent.width / 2.0
        self.y_origin_shift = extent.height / 2.0

    def resolution(self, zoom: int) -> float:
        return self.initial_resolution / (self.zoom_factor**zoom)

    def zoom_for_resolution(self, resolution: float) -> int:
        # Grid.hpp: ceil(log(init)/log(z) - log(res)/log(z))
        return math.ceil(
            (math.log(self.initial_resolution) / math.log(self.zoom_factor))
            - (math.log(resolution) / math.log(self.zoom_factor))
        )

    def pixels_to_crs(self, pixel_x: float, pixel_y: float, zoom: int) -> tuple[float, float]:
        res = self.resolution(zoom)
        return (pixel_x * res) - self.x_origin_shift, (pixel_y * res) - self.y_origin_shift

    def crs_to_pixels(self, x: float, y: float, zoom: int) -> tuple[int, int]:
        res = self.resolution(zoom)
        px = _to_upixel((self.x_origin_shift + x) / res)
        py = _to_upixel((self.y_origin_shift + y) / res)
        return px, py

    def pixels_to_tile(self, pixel_x: int, pixel_y: int) -> tuple[int, int]:
        return pixel_x // self.tile_size, pixel_y // self.tile_size

    def crs_to_tile(self, x: float, y: float, zoom: int) -> TileCoordinate:
        px, py = self.crs_to_pixels(x, y, zoom)
        tx, ty = self.pixels_to_tile(px, py)
        return TileCoordinate(zoom, tx, ty)

    def tile_bounds(self, coord: TileCoordinate) -> CRSBounds:
        x0, y0 = coord.x * self.tile_size, coord.y * self.tile_size
        x1, y1 = (coord.x + 1) * self.tile_size, (coord.y + 1) * self.tile_size
        minx, miny = self.pixels_to_crs(x0, y0, coord.zoom)
        maxx, maxy = self.pixels_to_crs(x1, y1, coord.zoom)
        return CRSBounds(minx, miny, maxx, maxy)

    def tile_extent(self, zoom: int) -> TileBounds:
        ll = self.crs_to_tile(*self.extent.lower_left(), zoom)
        ur = self.crs_to_tile(*self.extent.upper_right(), zoom)
        return TileBounds(ll.x, ll.y, ur.x, ur.y)

    def tiles_for_extent(self, extent: CRSBounds, zoom: int) -> TileBounds:
        ll = self.crs_to_tile(*extent.lower_left(), zoom)
        ur = self.crs_to_tile(*extent.upper_right(), zoom)
        return TileBounds(ll.x, ll.y, ur.x, ur.y)


def global_geodetic(tile_size: int = GEODETIC_DEFAULT_TILE_SIZE, tms_compatible: bool = True) -> Grid:
    root = 2 if tms_compatible else 1
    return Grid(
        tile_size,
        CRSBounds(-180.0, -90.0, 180.0, 90.0),
        CRS.from_epsg(4326),
        root_tiles=root,
    )


def global_mercator(tile_size: int = MERCATOR_DEFAULT_TILE_SIZE) -> Grid:
    origin = WEB_MERCATOR_ORIGIN
    return Grid(
        tile_size,
        CRSBounds(-origin, -origin, origin, origin),
        CRS.from_epsg(3857),
        root_tiles=1,
    )


def grid_for_profile(profile: Profile, tile_size: int | None) -> Grid:
    if profile == Profile.MERCATOR:
        size = MERCATOR_DEFAULT_TILE_SIZE if tile_size is None else int(tile_size)
        return global_mercator(size)
    size = GEODETIC_DEFAULT_TILE_SIZE if tile_size is None else int(tile_size)
    return global_geodetic(size)


def neighbor_coord(grid: Grid, coord: TileCoordinate, border_index: int) -> TileCoordinate | None:
    """HeightFieldChunker.hpp: Left=0, Top=1, Right=2, Bottom=3."""
    extent = grid.tile_extent(coord.zoom)
    if border_index == 0:
        if coord.x <= 0:
            return None
        return TileCoordinate(coord.zoom, coord.x - 1, coord.y)
    if border_index == 1:
        if coord.y >= extent.maxy:
            return None
        return TileCoordinate(coord.zoom, coord.x, coord.y + 1)
    if border_index == 2:
        if coord.x >= extent.maxx:
            return None
        return TileCoordinate(coord.zoom, coord.x + 1, coord.y)
    if border_index == 3:
        if coord.y <= 0:
            return None
        return TileCoordinate(coord.zoom, coord.x, coord.y - 1)
    raise ValueError(f"Bad neighbor border index: {border_index}")


def iter_tile_coordinates(
    grid: Grid,
    extent: CRSBounds,
    start_zoom: int,
    end_zoom: int,
) -> Iterator[TileCoordinate]:
    """GridIterator.hpp: zoom high→low, x west→east, y south→north."""
    if start_zoom < end_zoom:
        raise ValueError("start_zoom must be >= end_zoom")
    for zoom in range(start_zoom, end_zoom - 1, -1):
        bounds = grid.tiles_for_extent(extent, zoom)
        for x in range(bounds.minx, bounds.maxx + 1):
            for y in range(bounds.miny, bounds.maxy + 1):
                yield TileCoordinate(zoom, x, y)


def tile_coordinate_count(
    grid: Grid,
    extent: CRSBounds,
    start_zoom: int,
    end_zoom: int,
) -> int:
    """Count the same coordinates without materializing the tile list."""
    if start_zoom < end_zoom:
        raise ValueError("start_zoom must be >= end_zoom")
    total = 0
    for zoom in range(start_zoom, end_zoom - 1, -1):
        bounds = grid.tiles_for_extent(extent, zoom)
        total += (bounds.maxx - bounds.minx + 1) * (bounds.maxy - bounds.miny + 1)
    return total
