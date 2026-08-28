"""GDAL preprocessing pipeline (GDAL 3.12+ unified CLI)."""

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from app.schemas import PreprocessOptions
from app.services.job_progress import gdal_progress_flag_unsupported, run_gdal_command

logger = logging.getLogger(__name__)

_FILL_NODATA_MAX_DISTANCE = 10
_OVERVIEW_LEVELS = "2,4,8,16"


class PreprocessError(RuntimeError):
    pass


def _build_env(gdal_cachemax: int) -> dict[str, str]:
    env = os.environ.copy()
    env["GDAL_CACHEMAX"] = str(gdal_cachemax)
    return env


def _reproject_cmd(
    input_path: Path,
    output_path: Path,
    options: PreprocessOptions,
    *,
    show_progress: bool = False,
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
    if show_progress:
        cmd.append("--progress")
    cmd.extend([str(input_path), str(output_path)])
    return cmd


def _fill_nodata_cmd(
    input_path: Path,
    output_path: Path,
    *,
    show_progress: bool = False,
) -> list[str]:
    cmd = [
        "gdal",
        "raster",
        "fill-nodata",
        "--max-distance",
        str(_FILL_NODATA_MAX_DISTANCE),
        "--overwrite",
    ]
    if show_progress:
        cmd.append("--progress")
    cmd.extend([str(input_path), str(output_path)])
    return cmd


def _overview_add_cmd(dataset: Path, *, show_progress: bool = False) -> list[str]:
    cmd = [
        "gdal",
        "raster",
        "overview",
        "add",
        "-r",
        "average",
        "--levels",
        _OVERVIEW_LEVELS,
    ]
    if show_progress:
        cmd.append("--progress")
    cmd.append(str(dataset))
    return cmd


def _run_gdal(
    cmd: list[str],
    *,
    gdal_cachemax: int,
    on_subprogress: Callable[[float, str | None], None] | None = None,
    quiet_cmd: list[str] | None = None,
) -> None:
    """Run a GDAL raster subcommand, streaming --progress output when enabled."""
    logger.info("Running: %s", " ".join(cmd))
    env = _build_env(gdal_cachemax)
    show_progress = on_subprogress is not None and "--progress" in cmd
    try:
        run_gdal_command(cmd, env=env, on_subprogress=on_subprogress)
    except subprocess.CalledProcessError as exc:
        if show_progress and quiet_cmd is not None and gdal_progress_flag_unsupported(exc.stderr or ""):
            logger.warning(
                "GDAL command does not support --progress on this build; retrying without progress"
            )
            if on_subprogress is not None:
                on_subprogress(0.0, None)
            try:
                run_gdal_command(quiet_cmd, env=env, on_subprogress=on_subprogress)
            except subprocess.CalledProcessError as fallback_exc:
                raise PreprocessError(
                    f"Command failed ({fallback_exc.returncode}): {' '.join(quiet_cmd)}\n"
                    f"stdout: {fallback_exc.output}\nstderr: {fallback_exc.stderr}"
                ) from fallback_exc
            return
        raise PreprocessError(
            f"Command failed ({exc.returncode}): {' '.join(cmd)}\n"
            f"stdout: {exc.output}\nstderr: {exc.stderr}"
        ) from exc


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


def _step_weights(options: PreprocessOptions) -> tuple[float, float, float]:
    """Return (reproject, fill_nodata, overview) weights that sum to 1.0."""
    fill = 0.15 if options.fill_nodata else 0.0
    overview = 0.15 if options.build_overviews else 0.0
    reproject = 1.0 - fill - overview
    return reproject, fill, overview


def preprocess_dem(
    input_path: Path,
    work_dir: Path,
    options: PreprocessOptions,
    gdal_cachemax: int,
    *,
    on_subprogress: Callable[[float, str | None], None] | None = None,
) -> Path:
    """Run GDAL preprocessing and return path to CTB-ready raster."""
    work_dir.mkdir(parents=True, exist_ok=True)

    gdal_info(input_path, gdal_cachemax=gdal_cachemax)

    warped = work_dir / "warped.tif"
    filled = work_dir / "filled.tif"
    final = work_dir / "preprocessed.tif"

    show_progress = on_subprogress is not None
    reproject_w, fill_w, overview_w = _step_weights(options)
    completed = 0.0

    def _emit_step(weight: float, base: float, sub_percent: float, message: str) -> None:
        if on_subprogress is None:
            return
        scaled = base + sub_percent * weight
        on_subprogress(min(scaled, 100.0), message)

    reproject_cmd = _reproject_cmd(input_path, warped, options, show_progress=show_progress)
    reproject_quiet = _reproject_cmd(input_path, warped, options)
    _run_gdal(
        reproject_cmd,
        gdal_cachemax=gdal_cachemax,
        on_subprogress=(
            (lambda pct, msg: _emit_step(reproject_w, completed, pct, msg or "gdal raster reproject"))
            if show_progress
            else None
        ),
        quiet_cmd=reproject_quiet if show_progress else None,
    )
    completed += reproject_w * 100.0

    current = warped
    if options.fill_nodata:
        fill_base = completed
        fill_cmd = _fill_nodata_cmd(current, filled, show_progress=show_progress)
        fill_quiet = _fill_nodata_cmd(current, filled)
        _run_gdal(
            fill_cmd,
            gdal_cachemax=gdal_cachemax,
            on_subprogress=(
                (lambda pct, msg: _emit_step(fill_w, fill_base, pct, msg or "gdal raster fill-nodata"))
                if show_progress
                else None
            ),
            quiet_cmd=fill_quiet if show_progress else None,
        )
        completed += fill_w * 100.0
        current = filled

    if options.build_overviews:
        overview_base = completed
        overview_cmd = _overview_add_cmd(current, show_progress=show_progress)
        overview_quiet = _overview_add_cmd(current)
        _run_gdal(
            overview_cmd,
            gdal_cachemax=gdal_cachemax,
            on_subprogress=(
                (
                    lambda pct, msg: _emit_step(
                        overview_w, overview_base, pct, msg or "gdal raster overview add"
                    )
                )
                if show_progress
                else None
            ),
            quiet_cmd=overview_quiet if show_progress else None,
        )

    if on_subprogress is not None:
        on_subprogress(100.0, "preprocess complete")

    if current == warped:
        shutil.copy2(warped, final)
    else:
        shutil.copy2(current, final)

    return final
