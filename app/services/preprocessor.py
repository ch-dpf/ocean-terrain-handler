"""GDAL preprocessing pipeline."""

import logging
import shutil
import subprocess
from pathlib import Path

from app.schemas import PreprocessOptions

logger = logging.getLogger(__name__)


class PreprocessError(RuntimeError):
    pass


def _run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise PreprocessError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def gdal_info(dataset: Path) -> str:
    result = subprocess.run(
        ["gdalinfo", str(dataset)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PreprocessError(f"gdalinfo failed: {result.stderr}")
    return result.stdout


def preprocess_dem(
    input_path: Path,
    work_dir: Path,
    options: PreprocessOptions,
    gdal_cachemax: int,
) -> Path:
    """Run GDAL preprocessing and return path to CTB-ready raster."""
    work_dir.mkdir(parents=True, exist_ok=True)
    env = {"GDAL_CACHEMAX": str(gdal_cachemax)}

    warped = work_dir / "warped.tif"
    filled = work_dir / "filled.tif"
    final = work_dir / "preprocessed.tif"

    _run(
        [
            "gdalwarp",
            "-t_srs",
            options.target_crs,
            "-r",
            "bilinear",
            "-co",
            "TILED=YES",
            "-co",
            f"BLOCKXSIZE={options.block_size}",
            "-co",
            f"BLOCKYSIZE={options.block_size}",
            "-co",
            "COMPRESS=DEFLATE",
            str(input_path),
            str(warped),
        ],
        env=env,
    )

    current = warped
    if options.fill_nodata:
        fill_cmd = ["gdal_fillnodata.py", "-md", "10", str(current), str(filled)]
        if options.nodata_value is not None:
            fill_cmd[1:1] = ["-nodata", str(options.nodata_value)]
        _run(fill_cmd, env=env)
        current = filled

    if options.build_overviews:
        _run(["gdaladdo", "-r", "average", str(current), "2", "4", "8", "16"], env=env)

    if current == warped:
        shutil.copy2(warped, final)
    else:
        shutil.copy2(current, final)

    return final
