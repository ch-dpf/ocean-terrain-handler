"""Outside donor selection must be invariant to which tile owns the point."""

import numpy as np
import pytest

from app.services.ctb.grid import TileCoordinate, global_geodetic
from app.services.ctb.sample import sample_tile_heights
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.overviews import add_overviews
from tests.raster_fixtures import write_dem_geotiff_4326


@pytest.mark.parametrize("method", ["nearest", "bilinear", "average"])
@pytest.mark.parametrize("axis", ["north", "west", "south", "east"])
@pytest.mark.parametrize("overview", [False, True])
def test_outside_tile_uses_same_boundary_donor(tmp_path, method, axis, overview):
    path = write_dem_geotiff_4326(
        tmp_path / "dem.tif", width=200, height=200, west=0, north=0, pixel_deg=0.001
    )
    if overview:
        add_overviews(path, levels=(2,), block_size=16, workers=1)
    grid = global_geodetic(65)
    pairs = {
        "north": (TileCoordinate(10, 1024, 511), TileCoordinate(10, 1024, 512)),
        "west": (TileCoordinate(10, 1024, 511), TileCoordinate(10, 1023, 511)),
        "south": (TileCoordinate(10, 1024, 510), TileCoordinate(10, 1024, 509)),
        "east": (TileCoordinate(10, 1025, 511), TileCoordinate(10, 1026, 511)),
    }
    a, b = pairs[axis]
    with GeoTiffReader(path, preload=False) as src:
        inside = sample_tile_heights(src, grid, a, method)
        outside = sample_tile_heights(src, grid, b, method)
    x, y = {
        "north": (inside[0, :], outside[-1, :]),
        "south": (inside[-1, :], outside[0, :]),
        "west": (inside[:, 0], outside[:, -1]),
        "east": (inside[:, -1], outside[:, 0]),
    }[axis]
    assert np.all(x > 0)
    np.testing.assert_allclose(x, y, atol=1e-4, rtol=0)


def test_support_does_not_fill_source_holes(tmp_path):
    path = write_dem_geotiff_4326(
        tmp_path / "hole.tif",
        width=200,
        height=200,
        west=0,
        north=0,
        pixel_deg=0.001,
        hole=(60, 60, 120, 120),
    )
    with GeoTiffReader(path) as src:
        values = sample_tile_heights(
            src, global_geodetic(65), TileCoordinate(10, 1024, 511), "nearest"
        )
    assert values[35, 35] == 0
    assert values[5, 5] > 0
