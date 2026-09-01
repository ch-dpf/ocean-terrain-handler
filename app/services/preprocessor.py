"""DEM preprocessing pipeline (Python raster engine, no GDAL CLI)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.schemas import PreprocessOptions
from app.services.byte_progress import fraction_to_bytes, overview_bytes, raster_bytes
from app.services.raster.crsutil import parse_crs
from app.services.raster.errors import RasterError
from app.services.raster.fillnodata import DEFAULT_MAX_DISTANCE, fill_nodata_geotiff
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.info import raster_info_text
from app.services.raster.overviews import add_overviews
from app.services.raster.reproject import plan_destination_grid, reproject_geotiff

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

    try:
        with GeoTiffReader(input_path, cache_bytes=cache_bytes, preload=False) as src:
            _, dst_w, dst_h = plan_destination_grid(src, parse_crs(options.target_crs))
            itemsize = int(src.dtype.itemsize)
            reproject_b = raster_bytes(dst_w, dst_h, 1, itemsize)
            fill_b = reproject_b if options.fill_nodata else 0
            overview_b = overview_bytes(dst_w, dst_h, 1, itemsize) if options.build_overviews else 0
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc
    preprocess_bytes = max(1, reproject_b + fill_b + overview_b)

    def _emit_reproject(sub_percent: float, message: str | None) -> None:
        if on_subprogress is None:
            return
        done = fraction_to_bytes(reproject_b, sub_percent)
        on_subprogress(100.0 * done / preprocess_bytes, message or "reproject")

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
            on_progress=_emit_reproject if on_subprogress is not None else None,
        )
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc

    current = warped
    if options.fill_nodata:

        def _emit_fill(sub_percent: float, message: str | None) -> None:
            if on_subprogress is None:
                return
            done = reproject_b + fraction_to_bytes(fill_b, sub_percent)
            on_subprogress(min(100.0 * done / preprocess_bytes, 100.0), message or "fill-nodata")

        try:
            fill_nodata_geotiff(
                current,
                filled,
                max_distance=DEFAULT_MAX_DISTANCE,
                block_size=options.block_size,
                compress="DEFLATE",
                cache_bytes=cache_bytes,
                nodata=options.nodata_value,
                on_progress=_emit_fill if on_subprogress is not None else None,
            )
        except RasterError as exc:
            raise PreprocessError(str(exc)) from exc
        current = filled

    if options.build_overviews:

        def _emit_overview(sub_percent: float, message: str | None) -> None:
            if on_subprogress is None:
                return
            done = reproject_b + fill_b + fraction_to_bytes(overview_b, sub_percent)
            on_subprogress(min(100.0 * done / preprocess_bytes, 100.0), message or "overview add")

        try:
            add_overviews(
                current,
                block_size=options.block_size,
                compress="DEFLATE",
                cache_bytes=cache_bytes,
                on_progress=_emit_overview if on_subprogress is not None else None,
            )
        except RasterError as exc:
            raise PreprocessError(str(exc)) from exc

    _replace_with_sidecar(current, final)

    if on_subprogress is not None:
        on_subprogress(100.0, "preprocess complete")
    return final
