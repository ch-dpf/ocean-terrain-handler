"""Core raster engine tests for DEM (float) GeoTIFFs."""

from pathlib import Path

import numpy as np
import pytest
from pyproj import CRS

from app.services.raster.affine import Affine
from app.services.raster.fillnodata import fill_nodata_array
from app.services.raster.geotiff import GeoTiffReader, write_geotiff_array
from app.services.raster.info import raster_info_json
from app.services.raster.nodata import nodata_mask
from app.services.raster.resample import sample_bilinear
from tests.raster_fixtures import write_dem_geotiff_4326


def test_geotiff_roundtrip_window_read(tmp_path: Path):
    path = tmp_path / "round.tif"
    data = np.arange(32 * 32, dtype=np.float32).reshape(32, 32)
    write_geotiff_array(
        path,
        data,
        affine=Affine.north_up(0.0, 10.0, 1.0, 1.0),
        crs=CRS.from_epsg(4326),
        block_size=16,
        nodata=-9999.0,
    )
    with GeoTiffReader(path) as src:
        assert src.width == 32
        assert src.height == 32
        assert src.samples == 1
        assert src.nodata == -9999.0
        window = src.read_window(8, 8, 10, 12)
        np.testing.assert_allclose(window[:, :, 0], data[8:18, 8:20])
        outside = src.read_window(-4, -4, 8, 8)
        assert outside.shape == (8, 8, 1)
        np.testing.assert_array_equal(outside[:4, :4, 0], -9999.0)
        np.testing.assert_allclose(outside[4:, 4:, 0], data[:4, :4])


def test_raster_info_json_wgs84_extent(tmp_path: Path):
    dataset = write_dem_geotiff_4326(
        tmp_path / "src.tif", width=20, height=10, west=1.0, north=2.0, pixel_deg=0.1
    )
    info = raster_info_json(dataset)
    assert info["size"] == [20, 10]
    assert info["coordinateSystem"]["epsg"] == 4326
    assert info["nodata"] == -9999.0
    ring = info["wgs84Extent"]["coordinates"][0]
    assert ring
    bounds = info["wgs84Bounds"]
    assert bounds[0] == 1.0
    assert bounds[3] == 2.0


def test_sample_bilinear_identity():
    src = np.array([[[10.0], [20.0]], [[30.0], [40.0]]], dtype=np.float32)
    rows = np.array([[0.0, 0.0], [1.0, 1.0]])
    cols = np.array([[0.0, 1.0], [0.0, 1.0]])
    out = sample_bilinear(src, rows, cols)
    np.testing.assert_allclose(out[:, :, 0], [[10, 20], [30, 40]], atol=1e-5)


def test_same_crs_warp_preserves_float_elevation(tmp_path: Path):
    from app.services.raster.warp import warp_window

    src_arr = np.linspace(0.0, 100.0, 16 * 16, dtype=np.float32).reshape(16, 16)
    path = tmp_path / "src.tif"
    affine = Affine.north_up(0.0, 16.0, 1.0, 1.0)
    write_geotiff_array(path, src_arr, affine=affine, crs=CRS.from_epsg(4326), block_size=16)
    with GeoTiffReader(path) as src:
        out = warp_window(src, affine, CRS.from_epsg(4326), 0, 0, 16, 16, "bilinear")
    assert out.shape == (16, 16, 1)
    np.testing.assert_allclose(out[:, :, 0], src_arr, atol=1e-3)


def test_fill_nodata_array_idw():
    data = np.array(
        [
            [1.0, 1.0, 1.0, 1.0],
            [1.0, -9999.0, -9999.0, 3.0],
            [1.0, -9999.0, -9999.0, 3.0],
            [5.0, 5.0, 5.0, 5.0],
        ],
        dtype=np.float32,
    )
    filled = fill_nodata_array(data, nodata=-9999.0, max_distance=10)
    hole = filled[1:3, 1:3]
    assert not np.any(hole == -9999.0)
    assert np.all(hole > 1.0) and np.all(hole < 5.0)


def test_ordered_parallel_map_preserves_order():
    from app.services.raster.parallel import ordered_parallel_map, run_unordered

    doubled = list(ordered_parallel_map(range(24), lambda value: value * 2, workers=4))
    assert doubled == [value * 2 for value in range(24)]

    seen: list[int] = []
    run_unordered(range(10), seen.append, workers=3)
    assert sorted(seen) == list(range(10))


def test_overviews_used_for_coarse_warp(tmp_path: Path):
    from app.services.raster.overviews import add_overviews
    from app.services.raster.warp import warp_window

    height = width = 64
    data = np.zeros((height, width), dtype=np.float32)
    data += np.arange(width, dtype=np.float32)[None, :]
    data += np.arange(height, dtype=np.float32)[:, None] * 0.01
    path = tmp_path / "src.tif"
    affine = Affine.north_up(0.0, float(height), 1.0, 1.0)
    write_geotiff_array(path, data, affine=affine, crs=CRS.from_epsg(4326), block_size=16)
    assert add_overviews(path, levels=(2, 4), block_size=16, workers=2) is not None

    with GeoTiffReader(path) as src:
        assert src.overview_scales == [2, 4]
        assert src.select_level(8, 8, 8, 8).scale == 1
        assert src.select_level(64, 64, 8, 8).scale == 4
        dst_affine = Affine.north_up(0.0, float(height), 8.0, 8.0)
        out = warp_window(src, dst_affine, CRS.from_epsg(4326), 0, 0, 8, 8, "bilinear")
        assert out.shape[:2] == (8, 8)
        assert out[0, -1, 0] > out[0, 0, 0]
        assert src.select_level(28, 28, 8, 8).scale == 2


def test_destination_grid_is_formula_based():
    from app.services.raster.crsutil import destination_pixel_size, grid_dimension

    affine = Affine.north_up(0.0, 10.0, 0.1, 0.1)
    px, py = destination_pixel_size(CRS.from_epsg(4326), CRS.from_epsg(3857), affine, 20, 10)
    assert px > 0 and py > 0
    assert grid_dimension(10.0, 2.0) == 5
    assert grid_dimension(10.0, 3.0) == 4


def test_north_up_requires_exact_zero_shear():
    assert Affine.north_up(0.0, 1.0, 1.0, 1.0).is_north_up()
    tilted = Affine(a=1.0, b=1e-12, c=0.0, d=0.0, e=-1.0, f=1.0)
    assert not tilted.is_north_up()


def test_nodata_mask_treats_nan_as_invalid():
    array = np.array([[1.0, np.nan], [2.0, 3.0]], dtype=np.float32)
    mask = nodata_mask(array, None)
    assert mask.tolist() == [[False, True], [False, False]]


def test_utm_bounds_sampling_is_cheap_and_covers_corners():
    import time

    from app.services.raster.crsutil import (
        dest_sample_tolerance,
        destination_pixel_size,
        make_transformer,
        transform_bounds,
        transform_ring,
        transform_xy,
    )

    utm = CRS.from_epsg(32650)
    wgs84 = CRS.from_epsg(4326)
    bounds = (350000.0, 3388560.0, 411440.0, 3450000.0)
    affine = Affine.north_up(bounds[0], bounds[3], 30.0, 30.0)
    width = int(round((bounds[2] - bounds[0]) / 30.0))
    height = int(round((bounds[3] - bounds[1]) / 30.0))
    px, py = destination_pixel_size(utm, wgs84, affine, width, height)
    dest_abs_tol = dest_sample_tolerance(px, py)
    started = time.perf_counter()
    left, bottom, right, top = transform_bounds(utm, wgs84, bounds, dest_abs_tol=dest_abs_tol)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2
    ring = transform_ring(utm, wgs84, bounds, dest_abs_tol=dest_abs_tol)
    assert ring[0] == ring[-1]
    assert len(ring) >= 5

    transformer = make_transformer(utm, wgs84)
    corners_x = np.array([bounds[0], bounds[2], bounds[2], bounds[0]], dtype=np.float64)
    corners_y = np.array([bounds[1], bounds[1], bounds[3], bounds[3]], dtype=np.float64)
    cx, cy = transform_xy(transformer, corners_x, corners_y)
    assert left <= float(cx.min()) + 1e-9
    assert right >= float(cx.max()) - 1e-9
    assert bottom <= float(cy.min()) + 1e-9
    assert top >= float(cy.max()) - 1e-9


def test_bounds_densify_requires_dest_abs_tol():
    from app.services.raster.crsutil import transform_bounds
    from app.services.raster.errors import RasterError

    utm = CRS.from_epsg(32650)
    wgs84 = CRS.from_epsg(4326)
    bounds = (350000.0, 3388560.0, 411440.0, 3450000.0)
    with pytest.raises(RasterError, match="dest_abs_tol is required"):
        transform_bounds(utm, wgs84, bounds)


def test_bounds_densify_point_count_follows_dest_tolerance():
    from app.services.raster.crsutil import transform_ring

    utm = CRS.from_epsg(32650)
    wgs84 = CRS.from_epsg(4326)
    bounds = (350000.0, 3388560.0, 411440.0, 3450000.0)
    coarse = transform_ring(utm, wgs84, bounds, dest_abs_tol=1.0)
    fine = transform_ring(utm, wgs84, bounds, dest_abs_tol=1e-5)
    assert len(fine) > len(coarse)


def test_mercator_bounds_use_four_corners_only():
    from app.services.raster.crsutil import transform_bounds, transform_ring

    bounds = (116.0, 39.0, 117.0, 40.0)
    ring = transform_ring(CRS.from_epsg(4326), CRS.from_epsg(3857), bounds)
    assert len(ring) == 5
    left, bottom, right, top = transform_bounds(CRS.from_epsg(4326), CRS.from_epsg(3857), bounds)
    assert right > left and top > bottom


def test_unknown_resampling_is_rejected():
    from app.services.raster.resample import normalize_resampling

    with pytest.raises(ValueError, match="unsupported resampling method"):
        normalize_resampling("not-a-kernel")


def test_cubicspline_is_not_aliased_to_cubic():
    from app.services.raster.resample import RESAMPLE_CUBIC, RESAMPLE_CUBICSPLINE, normalize_resampling

    assert normalize_resampling("cubicspline") == RESAMPLE_CUBICSPLINE
    assert normalize_resampling("cubicspline") != RESAMPLE_CUBIC


def test_geotiff_write_preserves_nonzero_shear(tmp_path: Path):
    affine = Affine(a=1.0, b=1e-13, c=0.0, d=0.0, e=-1.0, f=10.0)
    assert not affine.is_north_up()
    path = tmp_path / "shear.tif"
    write_geotiff_array(
        path,
        np.zeros((4, 4), dtype=np.float32),
        affine=affine,
        crs=CRS.from_epsg(4326),
        block_size=16,
    )
    with GeoTiffReader(path) as src:
        assert src.affine.b == pytest.approx(1e-13)
        assert not src.affine.is_north_up()
