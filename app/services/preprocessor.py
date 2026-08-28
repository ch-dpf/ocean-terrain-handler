"""GDAL preprocessing pipeline (GDAL 3.12+ unified CLI)."""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from app.schemas import PreprocessOptions

logger = logging.getLogger(__name__)

_FILL_NODATA_MAX_DISTANCE = 10
_OVERVIEW_LEVELS = "2,4,8,16"


class PreprocessError(RuntimeError):
    pass


def _build_env(gdal_cachemax: int) -> dict[str, str]:
    env = os.environ.copy()
    env["GDAL_CACHEMAX"] = str(gdal_cachemax)
    return env


def _run(cmd: list[str], *, gdal_cachemax: int) -> None:
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_build_env(gdal_cachemax),
        check=False,
    )
    if result.returncode != 0:
        raise PreprocessError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def gdal_info(dataset: Path, *, gdal_cachemax: int = 512) -> str:
    result = subprocess.run(
        ["gdal", "raster", "info", "--format=text", str(dataset)],
        capture_output=True,
        text=True,
        env=_build_env(gdal_cachemax),
        check=False,
    )
    if result.returncode != 0:
        raise PreprocessError(f"gdal raster info failed: {result.stderr}")
    return result.stdout


def _reproject_cmd(
    input_path: Path,
    output_path: Path,
    options: PreprocessOptions,
) -> list[str]:
    cmd = [
        "gdal",
        "raster",
        "reproject",
        "--dst-crs",
        options.target_crs,
        "-r",
        "bilinear",
        "--co",
        "TILED=YES",
        "--co",
        f"BLOCKXSIZE={options.block_size}",
        "--co",
        f"BLOCKYSIZE={options.block_size}",
        "--co",
        "COMPRESS=DEFLATE",
        "--overwrite",
    ]
    if options.nodata_value is not None:
        nodata = str(options.nodata_value)
        cmd.extend(["--input-nodata", nodata, "--output-nodata", nodata])
    cmd.extend([str(input_path), str(output_path)])
    return cmd


def _fill_nodata_cmd(input_path: Path, output_path: Path) -> list[str]:
    return [
        "gdal",
        "raster",
        "fill-nodata",
        "--max-distance",
        str(_FILL_NODATA_MAX_DISTANCE),
        "--overwrite",
        str(input_path),
        str(output_path),
    ]


def _overview_add_cmd(dataset: Path) -> list[str]:
    return [
        "gdal",
        "raster",
        "overview",
        "add",
        "-r",
        "average",
        "--levels",
        _OVERVIEW_LEVELS,
        str(dataset),
    ]


def preprocess_dem(
    input_path: Path,
    work_dir: Path,
    options: PreprocessOptions,
    gdal_cachemax: int,
) -> Path:
    """Run GDAL preprocessing and return path to CTB-ready raster."""
    work_dir.mkdir(parents=True, exist_ok=True)

    gdal_info(input_path, gdal_cachemax=gdal_cachemax)

    warped = work_dir / "warped.tif"
    filled = work_dir / "filled.tif"
    final = work_dir / "preprocessed.tif"

    _run(_reproject_cmd(input_path, warped, options), gdal_cachemax=gdal_cachemax)

    current = warped
    if options.fill_nodata:
        _run(_fill_nodata_cmd(current, filled), gdal_cachemax=gdal_cachemax)
        current = filled

    if options.build_overviews:
        _run(_overview_add_cmd(current), gdal_cachemax=gdal_cachemax)

    if current == warped:
        shutil.copy2(warped, final)
    else:
        shutil.copy2(current, final)

    return final
