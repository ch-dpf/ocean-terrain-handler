"""Build reduced-resolution GeoTIFF overviews (replacement for ``gdal raster overview add``)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import tifffile

from app.services.raster.geotiff import GeoTiffReader, geotiff_extratags, tiff_compression
from app.services.raster.parallel import default_workers, ordered_parallel_map
from app.services.raster.resample import average_downsample, cast_sampled

ProgressFn = Callable[[float, str | None], None]
DEFAULT_LEVELS = (2, 4, 8, 16)


def add_overviews(
    dataset: Path,
    *,
    levels: tuple[int, ...] = DEFAULT_LEVELS,
    block_size: int = 256,
    compress: str = "DEFLATE",
    jpeg_quality: int = 85,
    cache_bytes: int = 512 * 1024 * 1024,
    workers: int | None = None,
    on_progress: ProgressFn | None = None,
) -> Path | None:
    """Write ``dataset.tif.ovr`` with average-resampled pyramid levels."""
    ovr_path = Path(str(dataset) + ".ovr")
    if ovr_path.exists():
        ovr_path.unlink()

    with GeoTiffReader(dataset, cache_bytes=cache_bytes) as src:
        valid_levels = [level for level in levels if src.width // level >= 1 and src.height // level >= 1]
        if not valid_levels:
            return None
        thread_count = default_workers() if workers is None else max(1, int(workers))
        planned_pixels = 0
        specs: list[tuple[int, int, int]] = []
        for level in valid_levels:
            out_w = max(1, src.width // level)
            out_h = max(1, src.height // level)
            specs.append((level, out_h, out_w))
            planned_pixels += out_w * out_h
        planned_pixels = max(1, planned_pixels)
        done_pixels = 0
        codec, codec_args = tiff_compression(compress, jpeg_quality)
        nodata = src.nodata
        dtype = src.dtype

        with tifffile.TiffWriter(ovr_path, bigtiff=True) as tif:
            for level, out_h, out_w in specs:
                n_ty = (out_h + block_size - 1) // block_size
                n_tx = (out_w + block_size - 1) // block_size
                affine = src.affine.scaled(level)

                def tiles(
                    level: int = level,
                    out_h: int = out_h,
                    out_w: int = out_w,
                    n_ty: int = n_ty,
                    n_tx: int = n_tx,
                ) -> Iterator[np.ndarray]:
                    nonlocal done_pixels
                    coords = [(ty, tx) for ty in range(n_ty) for tx in range(n_tx)]

                    def _compute_tile(coord: tuple[int, int]) -> np.ndarray:
                        ty, tx = coord
                        r0 = ty * block_size
                        c0 = tx * block_size
                        sl_h = min(block_size, out_h - r0)
                        sl_w = min(block_size, out_w - c0)
                        src_r0 = r0 * level
                        src_c0 = c0 * level
                        src_h = min(src.height - src_r0, sl_h * level)
                        src_w = min(src.width - src_c0, sl_w * level)
                        window = src.read_window(src_r0, src_c0, src_h, src_w)[:, :, :1]
                        resized = average_downsample(window, sl_h, sl_w, nodata=nodata)
                        resized = cast_sampled(resized, dtype, nodata=nodata)
                        full = np.zeros((block_size, block_size, 1), dtype=dtype)
                        if nodata is not None:
                            full[:] = nodata
                        full[:sl_h, :sl_w] = resized
                        return full

                    for coord, full in zip(
                        coords, ordered_parallel_map(coords, _compute_tile, workers=thread_count)
                    ):
                        ty, tx = coord
                        sl_h = min(block_size, out_h - ty * block_size)
                        sl_w = min(block_size, out_w - tx * block_size)
                        done_pixels += sl_h * sl_w
                        if on_progress is not None:
                            on_progress(100.0 * done_pixels / planned_pixels, "overview add")
                        yield full[:, :, 0]

                kwargs: dict = {
                    "shape": (out_h, out_w),
                    "dtype": dtype,
                    "photometric": "minisblack",
                    "tile": (block_size, block_size),
                    "extratags": geotiff_extratags(src.crs, affine, nodata=nodata),
                    "software": "ocean-terrain-handler",
                    "metadata": None,
                }
                if codec is not None:
                    kwargs["compression"] = codec
                    if codec_args:
                        kwargs["compressionargs"] = codec_args
                tif.write(tiles(), **kwargs)

    if on_progress is not None:
        on_progress(100.0, "overview complete")
    return ovr_path if ovr_path.is_file() else None
