"""Tests for GDAL unified CLI command construction."""

from pathlib import Path

import pytest

from app.schemas import PreprocessOptions
from app.services.preprocessor import (
    _fill_nodata_cmd,
    _overview_add_cmd,
    _reproject_cmd,
)


def test_reproject_cmd_defaults() -> None:
    options = PreprocessOptions()
    cmd = _reproject_cmd(Path("in.tif"), Path("out.tif"), options)

    assert cmd[:4] == ["gdal", "raster", "reproject", "--dst-crs"]
    assert "EPSG:4326" in cmd
    assert "-r" in cmd and "bilinear" in cmd
    assert "--co" in cmd and "TILED=YES" in cmd
    assert "BLOCKXSIZE=256" in cmd
    assert "BLOCKYSIZE=256" in cmd
    assert "COMPRESS=DEFLATE" in cmd
    assert "--overwrite" in cmd
    assert cmd[-2:] == ["in.tif", "out.tif"]


def test_reproject_cmd_with_nodata() -> None:
    options = PreprocessOptions(nodata_value=-9999.0)
    cmd = _reproject_cmd(Path("in.tif"), Path("out.tif"), options)

    idx = cmd.index("--input-nodata")
    assert cmd[idx + 1] == "-9999.0"
    idx = cmd.index("--output-nodata")
    assert cmd[idx + 1] == "-9999.0"


def test_fill_nodata_cmd() -> None:
    cmd = _fill_nodata_cmd(Path("warped.tif"), Path("filled.tif"))

    assert cmd == [
        "gdal",
        "raster",
        "fill-nodata",
        "--max-distance",
        "10",
        "--overwrite",
        "warped.tif",
        "filled.tif",
    ]


def test_overview_add_cmd() -> None:
    cmd = _overview_add_cmd(Path("filled.tif"))

    assert cmd == [
        "gdal",
        "raster",
        "overview",
        "add",
        "-r",
        "average",
        "--levels",
        "2,4,8,16",
        "filled.tif",
    ]


def test_reproject_cmd_rejects_invalid_block_size() -> None:
    with pytest.raises(ValueError, match="multiple of 16"):
        PreprocessOptions(block_size=65)
