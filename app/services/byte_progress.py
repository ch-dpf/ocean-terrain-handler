"""Job progress measured in uncompressed raster bytes (samples × dtype itemsize)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from pyproj import CRS

from app.schemas import CtbOptions, PreprocessOptions, Profile
from app.services.raster.affine import Affine
from app.services.raster.crsutil import (
    EARTH_HALF,
    WEB_MERCATOR_MAX_LAT,
    crs_epsg,
    dest_sample_tolerance,
    destination_pixel_size,
    parse_crs,
    wgs84_bounds_from_rect,
)
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.overviews import DEFAULT_LEVELS
from app.services.raster.reproject import plan_destination_grid

# ctb-tile default when ``-t`` / tile_size is omitted.
CTB_DEFAULT_TILE_SIZE = 65


def raster_bytes(width: int, height: int, samples: int = 1, itemsize: int = 1) -> int:
    return max(0, int(width) * int(height) * int(samples) * int(itemsize))


def overview_bytes(
    width: int,
    height: int,
    samples: int = 1,
    itemsize: int = 1,
    levels: tuple[int, ...] = DEFAULT_LEVELS,
) -> int:
    total = 0
    for level in levels:
        out_w = width // level
        out_h = height // level
        if out_w >= 1 and out_h >= 1:
            total += raster_bytes(out_w, out_h, samples, itemsize)
    return total


def fraction_to_bytes(span: int, percent: float) -> int:
    """Map a 0-100 fraction of ``span`` bytes; 100% is exactly ``span``."""
    if span <= 0:
        return 0
    if percent >= 100.0:
        return span
    if percent <= 0.0:
        return 0
    return min(span, int(span * percent / 100.0))


def ctb_tile_size(options: CtbOptions) -> int:
    return int(options.tile_size) if options.tile_size is not None else CTB_DEFAULT_TILE_SIZE


def auto_max_zoom_geodetic(pixel_size_deg: float, tile_size: int) -> int:
    if pixel_size_deg <= 0:
        return 0
    ratio = 180.0 / (pixel_size_deg * tile_size)
    if ratio <= 1.0:
        return 0
    return max(0, math.ceil(math.log2(ratio) - 1e-12))


def auto_max_zoom_mercator(pixel_size_m: float, tile_size: int) -> int:
    if pixel_size_m <= 0:
        return 0
    ratio = (2 * EARTH_HALF) / (pixel_size_m * tile_size)
    if ratio <= 1.0:
        return 0
    return max(0, math.ceil(math.log2(ratio) - 1e-12))


def _lonlat_to_mercator_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    lat = min(max(lat, -WEB_MERCATOR_MAX_LAT), WEB_MERCATOR_MAX_LAT)
    n = 2**z
    x = int(math.floor((lon + 180.0) / 360.0 * n))
    lat_rad = math.radians(lat)
    y = int(math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n))
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def _xyz_range_mercator(bounds_wgs84: list[float], z: int) -> tuple[int, int, int, int]:
    west, south, east, north = bounds_wgs84
    x0, y0 = _lonlat_to_mercator_tile(west, north, z)
    x1, y1 = _lonlat_to_mercator_tile(east, south, z)
    n = 2**z
    return max(0, min(x0, x1)), min(n - 1, max(x0, x1)), max(0, min(y0, y1)), min(n - 1, max(y0, y1))


def _xyz_range_geodetic(bounds_wgs84: list[float], z: int) -> tuple[int, int, int, int]:
    west, south, east, north = bounds_wgs84
    n_x = 2 ** (z + 1)
    n_y = 2**z
    x0 = int(math.floor((west + 180.0) / 360.0 * n_x))
    x1 = int(math.floor((east + 180.0) / 360.0 * n_x))
    y0 = int(math.floor((90.0 - north) / 180.0 * n_y))
    y1 = int(math.floor((90.0 - south) / 180.0 * n_y))
    return max(0, min(x0, x1)), min(n_x - 1, max(x0, x1)), max(0, min(y0, y1)), min(n_y - 1, max(y0, y1))


def count_tiles_at_zoom(bounds_wgs84: list[float], z: int, profile: Profile) -> int:
    if profile == Profile.MERCATOR:
        x0, x1, y0, y1 = _xyz_range_mercator(bounds_wgs84, z)
    else:
        x0, x1, y0, y1 = _xyz_range_geodetic(bounds_wgs84, z)
    return max(0, (x1 - x0 + 1) * (y1 - y0 + 1))


def count_terrain_tiles(
    bounds_wgs84: list[float],
    *,
    profile: Profile,
    min_zoom: int,
    max_zoom: int,
) -> int:
    if max_zoom < min_zoom:
        max_zoom = min_zoom
    return sum(count_tiles_at_zoom(bounds_wgs84, z, profile) for z in range(min_zoom, max_zoom + 1))


def _affine_bounds(affine: Affine, width: int, height: int) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for col, row in ((0, 0), (width, 0), (width, height), (0, height)):
        x, y = affine.xy(col, row)
        xs.append(float(x))
        ys.append(float(y))
    return min(xs), min(ys), max(xs), max(ys)


def _max_zoom_for_grid(
    dst_crs: CRS,
    affine: Affine,
    width: int,
    height: int,
    options: CtbOptions,
) -> int:
    if options.start_zoom is not None:
        return int(options.start_zoom)
    tile = ctb_tile_size(options)
    if options.profile == Profile.MERCATOR:
        merc = parse_crs("EPSG:3857")
        if crs_epsg(dst_crs) == 3857:
            px = min(affine.pixel_width, affine.pixel_height)
        else:
            px, py = destination_pixel_size(dst_crs, merc, affine, width, height)
            px = min(px, py)
        return auto_max_zoom_mercator(px, tile)
    wgs84 = parse_crs("EPSG:4326")
    if crs_epsg(dst_crs) == 4326:
        px = min(affine.pixel_width, affine.pixel_height)
    else:
        px, py = destination_pixel_size(dst_crs, wgs84, affine, width, height)
        px = min(px, py)
    return auto_max_zoom_geodetic(px, tile)


@dataclass(frozen=True, slots=True)
class ByteBudget:
    reproject: int
    fill_nodata: int
    overviews: int
    tiles: int

    @property
    def preprocess(self) -> int:
        return self.reproject + self.fill_nodata + self.overviews

    @property
    def total(self) -> int:
        return self.preprocess + self.tiles


def plan_pipeline_bytes(
    input_path: Path,
    preprocess: PreprocessOptions,
    ctb: CtbOptions,
    *,
    cache_bytes: int,
) -> ByteBudget:
    """Count uncompressed raster bytes the job will write."""
    with GeoTiffReader(input_path, cache_bytes=cache_bytes, preload=False) as src:
        dst_crs = parse_crs(preprocess.target_crs)
        affine, width, height = plan_destination_grid(src, dst_crs)
        samples = 1
        itemsize = int(src.dtype.itemsize)
        reproject = raster_bytes(width, height, samples, itemsize)
        fill_nodata = reproject if preprocess.fill_nodata else 0
        overviews = overview_bytes(width, height, samples, itemsize) if preprocess.build_overviews else 0
        dest_bounds = _affine_bounds(affine, width, height)
        wgs84 = parse_crs("EPSG:4326")
        wgs_px, wgs_py = destination_pixel_size(dst_crs, wgs84, affine, width, height)
        bounds_wgs84 = wgs84_bounds_from_rect(
            dst_crs, dest_bounds, dest_abs_tol=dest_sample_tolerance(wgs_px, wgs_py)
        )
        min_zoom = int(ctb.end_zoom) if ctb.end_zoom is not None else 0
        max_zoom = _max_zoom_for_grid(dst_crs, affine, width, height, ctb)
        n_tiles = 0 if ctb.layer_only else count_terrain_tiles(
            bounds_wgs84,
            profile=ctb.profile,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
        tiles = n_tiles * raster_bytes(ctb_tile_size(ctb), ctb_tile_size(ctb), samples, itemsize)
        return ByteBudget(
            reproject=reproject,
            fill_nodata=fill_nodata,
            overviews=overviews,
            tiles=tiles,
        )
