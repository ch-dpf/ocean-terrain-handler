"""Python raster preprocess tests (no GDAL CLI)."""

from pathlib import Path

import numpy as np
import pytest

from app.schemas import PreprocessOptions
from app.services.preprocessor import PreprocessError, gdal_info, preprocess_dem
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.nodata import nodata_mask
from tests.raster_fixtures import write_dem_geotiff_4326


def test_gdal_info_text_contains_size(tmp_path: Path):
    dataset = write_dem_geotiff_4326(tmp_path / "src.tif", width=32, height=16)
    text = gdal_info(dataset)
    assert "Size is 32, 16" in text
    assert "EPSG:4326" in text
    assert "NoData Value=-9999" in text


def test_gdal_info_rejects_missing_file(tmp_path: Path):
    with pytest.raises(PreprocessError):
        gdal_info(tmp_path / "missing.tif")


def test_preprocess_reprojects_fills_and_adds_overviews(tmp_path: Path):
    source = write_dem_geotiff_4326(
        tmp_path / "src.tif",
        width=32,
        height=32,
        hole=(12, 12, 16, 16),
    )
    work = tmp_path / "work"
    events: list[tuple[float, str | None]] = []
    output = preprocess_dem(
        source,
        work,
        PreprocessOptions(
            target_crs="EPSG:4326",
            fill_nodata=True,
            build_overviews=True,
            block_size=16,
            nodata_value=-9999.0,
        ),
        gdal_cachemax=64,
        on_subprogress=lambda pct, msg: events.append((pct, msg)),
    )
    assert output.name == "preprocessed.tif"
    assert output.is_file()
    assert Path(str(output) + ".ovr").is_file()
    assert events
    assert events[-1][0] == 100.0

    with GeoTiffReader(output) as dst:
        assert dst.crs.to_epsg() == 4326
        assert dst.samples == 1
        assert dst.width > 0 and dst.height > 0
        assert dst.nodata == -9999.0
        window = dst.read_window(12, 12, 4, 4)[:, :, 0]
        assert not np.any(nodata_mask(window, dst.nodata))
        assert dst.overview_scales == [2, 4, 8, 16][: len(dst.overview_scales)]
        assert 2 in dst.overview_scales


def test_preprocess_reprojects_to_3857(tmp_path: Path):
    source = write_dem_geotiff_4326(tmp_path / "src.tif", width=24, height=24)
    output = preprocess_dem(
        source,
        tmp_path / "work",
        PreprocessOptions(
            target_crs="EPSG:3857",
            fill_nodata=False,
            build_overviews=False,
            block_size=16,
        ),
        gdal_cachemax=32,
    )
    with GeoTiffReader(output) as dst:
        assert dst.crs.to_epsg() == 3857
        assert dst.samples == 1
        assert dst.width > 0 and dst.height > 0


def test_preprocess_rejects_invalid_block_size():
    with pytest.raises(ValueError, match="multiple of 16"):
        PreprocessOptions(block_size=65)
