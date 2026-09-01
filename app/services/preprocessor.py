"""DEM preprocessing pipeline (Python raster engine, no GDAL CLI)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.schemas import PreprocessOptions
from app.services.raster.errors import RasterError
from app.services.raster.fillnodata import DEFAULT_MAX_DISTANCE, fill_nodata_geotiff
from app.services.raster.info import raster_info_text
from app.services.raster.overviews import add_overviews
from app.services.raster.reproject import reproject_geotiff

ProgressFn = Callable[[float, str | None], None]


class PreprocessError(RuntimeError):
    pass


def _cache_bytes(gdal_cachemax: int | None) -> int:
    megabytes = max(int(gdal_cachemax or 64), 1)
    return megabytes * 1024 * 1024


def gdal_info(dataset: Path, *, gdal_cachemax: int = 512) -> str:
    """Return human-readable raster metadata (kept name for compatibility)."""
    try:
        return raster_info_text(dataset, cache_bytes=_cache_bytes(gdal_cachemax))
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc


def _step_weights(options: PreprocessOptions) -> tuple[float, float, float]:
    """Return (reproject, fill_nodata, overview) weights that sum to 1.0."""
    fill = 0.15 if options.fill_nodata else 0.0
    overview = 0.15 if options.build_overviews else 0.0
    reproject = 1.0 - fill - overview
    return reproject, fill, overview


def _replace_with_sidecar(source: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    source.replace(dest)
    ovr_src = Path(str(source) + ".ovr")
    ovr_dst = Path(str(dest) + ".ovr")
    if ovr_src.is_file():
        if ovr_dst.exists():
            ovr_dst.unlink()
        ovr_src.replace(ovr_dst)


def preprocess_dem(
    input_path: Path,
    work_dir: Path,
    options: PreprocessOptions,
    gdal_cachemax: int,
    *,
    on_subprogress: ProgressFn | None = None,
) -> Path:
    """Reproject, optionally fill NODATA and build overviews, return a CTB-ready GeoTIFF."""
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_bytes = _cache_bytes(gdal_cachemax)
    warped = work_dir / "warped.tif"
    filled = work_dir / "filled.tif"
    final = work_dir / "preprocessed.tif"

    gdal_info(input_path, gdal_cachemax=gdal_cachemax)

    show_progress = on_subprogress is not None
    reproject_w, fill_w, overview_w = _step_weights(options)
    completed = 0.0

    def _emit_step(weight: float, base: float, sub_percent: float, message: str) -> None:
        if on_subprogress is None:
            return
        scaled = base + sub_percent * weight
        on_subprogress(min(scaled, 100.0), message)

    try:
        reproject_geotiff(
            input_path,
            warped,
            dst_crs=options.target_crs,
            compress="DEFLATE",
            block_size=options.block_size,
            cache_bytes=cache_bytes,
            resampling="bilinear",
            nodata=options.nodata_value,
            on_progress=(
                (lambda pct, msg: _emit_step(reproject_w, completed, pct, msg or "reproject"))
                if show_progress
                else None
            ),
        )
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc
    completed += reproject_w * 100.0

    current = warped
    if options.fill_nodata:
        fill_base = completed
        try:
            fill_nodata_geotiff(
                current,
                filled,
                max_distance=DEFAULT_MAX_DISTANCE,
                block_size=options.block_size,
                compress="DEFLATE",
                cache_bytes=cache_bytes,
                nodata=options.nodata_value,
                on_progress=(
                    (lambda pct, msg: _emit_step(fill_w, fill_base, pct, msg or "fill-nodata"))
                    if show_progress
                    else None
                ),
            )
        except RasterError as exc:
            raise PreprocessError(str(exc)) from exc
        completed += fill_w * 100.0
        current = filled

    if options.build_overviews:
        overview_base = completed
        try:
            add_overviews(
                current,
                block_size=options.block_size,
                compress="DEFLATE",
                cache_bytes=cache_bytes,
                on_progress=(
                    (
                        lambda pct, msg: _emit_step(
                            overview_w, overview_base, pct, msg or "overview add"
                        )
                    )
                    if show_progress
                    else None
                ),
            )
        except RasterError as exc:
            raise PreprocessError(str(exc)) from exc

    _replace_with_sidecar(current, final)

    if on_subprogress is not None:
        on_subprogress(100.0, "preprocess complete")
    return final
