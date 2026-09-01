"""Byte-budget job progress tests."""

from pathlib import Path

from app.schemas import CtbOptions, PreprocessOptions, Profile
from app.services.byte_progress import (
    ByteBudget,
    count_tiles_at_zoom,
    fraction_to_bytes,
    overview_bytes,
    plan_pipeline_bytes,
    raster_bytes,
)
from app.services.job_progress import JobProgressTracker
from app.services.raster.crsutil import parse_crs
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.reproject import plan_destination_grid
from tests.raster_fixtures import write_dem_geotiff_4326


def test_fraction_to_bytes_is_exact_at_100():
    assert fraction_to_bytes(1000, 0.0) == 0
    assert fraction_to_bytes(1000, 50.0) == 500
    assert fraction_to_bytes(1000, 100.0) == 1000
    assert fraction_to_bytes(0, 50.0) == 0


def test_overview_bytes_are_exact_level_sums():
    # 64x64 float32; levels 2 and 4 → 32x32x4 + 16x16x4
    assert overview_bytes(64, 64, 1, 4, levels=(2, 4)) == 32 * 32 * 4 + 16 * 16 * 4


def test_geodetic_z0_is_two_tiles_covering_the_world():
    world = [-180.0, -90.0, 180.0, 90.0]
    assert count_tiles_at_zoom(world, 0, Profile.GEODETIC) == 2


def test_plan_pipeline_bytes_counts_reproject_fill_overviews_and_tiles(tmp_path: Path):
    source = write_dem_geotiff_4326(tmp_path / "src.tif", width=32, height=32)
    budget = plan_pipeline_bytes(
        source,
        PreprocessOptions(
            target_crs="EPSG:4326",
            fill_nodata=True,
            build_overviews=True,
            block_size=16,
        ),
        CtbOptions(profile=Profile.GEODETIC, start_zoom=3, end_zoom=3, tile_size=65),
        cache_bytes=32 * 1024 * 1024,
    )
    with GeoTiffReader(source, preload=False) as src:
        _, width, height = plan_destination_grid(src, parse_crs("EPSG:4326"))
        itemsize = int(src.dtype.itemsize)
    assert budget.reproject == raster_bytes(width, height, 1, itemsize)
    assert budget.fill_nodata == budget.reproject
    assert budget.overviews == overview_bytes(width, height, 1, itemsize)
    assert budget.tiles > 0
    assert budget.total == budget.reproject + budget.fill_nodata + budget.overviews + budget.tiles
    assert budget.preprocess == budget.reproject + budget.fill_nodata + budget.overviews


def test_job_percent_equals_bytes_done_over_planned():
    budget = ByteBudget(reproject=100, fill_nodata=50, overviews=50, tiles=200)
    tracker = JobProgressTracker(bytes_planned=budget.total)
    tracker.set_bytes_done(budget.reproject)
    assert tracker.snapshot().percent == 25.0
    tracker.set_bytes_done(budget.preprocess)
    assert tracker.snapshot().percent == 50.0
    tracker.set_bytes_done(budget.total)
    assert tracker.snapshot().percent == 100.0
    assert raster_bytes(10, 10, 1, 4) == 400
