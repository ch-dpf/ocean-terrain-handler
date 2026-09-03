"""Build GDAL-compatible average overview dimensions and pixel footprints."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import tifffile

from app.services.raster.affine import Affine
from app.services.raster.geotiff import GeoTiffReader, geotiff_extratags, tiff_compression
from app.services.raster.parallel import ordered_parallel_map, raster_workers
from app.services.raster.resample import cast_sampled, sample_footprint

ProgressFn = Callable[[float, str | None], None]
DEFAULT_LEVELS = (2, 4, 8, 16)


def overview_shapes(width: int, height: int, levels: tuple[int, ...] = DEFAULT_LEVELS):
    """Unique reduced sizes, in increasing reduction order, preserving tails."""
    seen = set()
    for level in sorted(set(levels)):
        if level < 2:
            raise ValueError("overview factors must be >= 2")
        w, h = (width + level - 1) // level, (height + level - 1) // level
        if (w, h) != (width, height) and (w, h) not in seen:
            seen.add((w, h))
            yield level, h, w


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
    """Stream each average level, cascading from the preceding completed level."""
    ovr_path = Path(str(dataset) + ".ovr")
    if ovr_path.exists():
        ovr_path.unlink()
    with GeoTiffReader(dataset, cache_bytes=max(1, cache_bytes // 2), preload=False) as base:
        specs = list(overview_shapes(base.width, base.height, levels))
        if not specs:
            return None
        planned = sum(h * w for _, h, w in specs)
        done = 0
        codec, codec_args = tiff_compression(compress, jpeg_quality)
        dtype, nodata = base.dtype, base.nodata
        with tifffile.TiffWriter(ovr_path, bigtiff=True) as tif:
            for index, (_, out_h, out_w) in enumerate(specs):
                # Previously completed pages are flushed before reopening.
                src = (
                    base
                    if index == 0
                    else GeoTiffReader(dataset, cache_bytes=max(1, cache_bytes // 2), preload=False)
                )
                try:
                    source_level = src.select_level(src.width, src.height, out_w, out_h)
                    sx, sy = source_level.width / out_w, source_level.height / out_h
                    thread_count = raster_workers(
                        cache_bytes, int(block_size * block_size * (64 + 16 * sx * sy)), workers
                    )
                    ax, ay = base.width / out_w, base.height / out_h
                    a = base.affine
                    affine = Affine(a.a * ax, a.b * ay, a.c, a.d * ax, a.e * ay, a.f)

                    def compute(
                        coord,
                        out_h=out_h,
                        out_w=out_w,
                        sx=sx,
                        sy=sy,
                        source_level=source_level,
                        src=src,
                    ):
                        r0, c0 = coord
                        h, w = min(block_size, out_h - r0), min(block_size, out_w - c0)
                        rr, cc = int(np.floor(r0 * sy)), int(np.floor(c0 * sx))
                        rh = min(source_level.height, int(np.ceil((r0 + h) * sy))) - rr
                        cw = min(source_level.width, int(np.ceil((c0 + w) * sx))) - cc
                        window = src.read_window(rr, cc, rh, cw, level=source_level)[:, :, 0]
                        cols, rows = np.meshgrid(
                            (np.arange(c0, c0 + w + 1) * sx) - cc,
                            (np.arange(r0, r0 + h + 1) * sy) - rr,
                        )
                        resized = sample_footprint(window, rows, cols, "average", nodata=nodata)
                        # GDAL 3.12's no-mask 2-column fast path uses an
                        # unweighted 2x2 mean when a row intersects two source
                        # rows, including a fractional first/last row.
                        if nodata is None and sx == 2.0:
                            for j in range(h):
                                y0 = int((r0 + j) * sy + 1e-8)
                                y1 = min(
                                    source_level.height, int(np.ceil((r0 + j + 1) * sy - 1e-8))
                                )
                                if y1 - y0 == 2:
                                    values = window[y0 - rr : y1 - rr, : 2 * w].reshape(2, w, 2)
                                    if np.isfinite(values).all():
                                        resized[j] = values.mean(axis=(0, 2), dtype=np.float64)
                        typed = cast_sampled(resized, dtype, nodata=nodata)
                        full = np.full(
                            (block_size, block_size), 0 if nodata is None else nodata, dtype=dtype
                        )
                        full[:h, :w] = typed
                        return h * w, full

                    def tiles(
                        out_h=out_h, out_w=out_w, thread_count=thread_count, compute=compute
                    ) -> Iterator[np.ndarray]:
                        nonlocal done
                        coords = (
                            (r, c)
                            for r in range(0, out_h, block_size)
                            for c in range(0, out_w, block_size)
                        )
                        for pixels, full in ordered_parallel_map(
                            coords, compute, workers=thread_count
                        ):
                            done += pixels
                            if on_progress is not None:
                                on_progress(100.0 * done / planned, "overview add")
                            yield full

                    kwargs = {
                        "shape": (out_h, out_w),
                        "dtype": dtype,
                        "photometric": "minisblack",
                        "tile": (block_size, block_size),
                        "subfiletype": 1,
                        "extratags": geotiff_extratags(base.crs, affine, nodata=nodata),
                        "software": "ocean-terrain-handler",
                        "metadata": None,
                    }
                    if codec is not None:
                        kwargs["compression"] = codec
                        if codec_args:
                            kwargs["compressionargs"] = codec_args
                    tif.write(tiles(), **kwargs)
                    tif.filehandle.flush()
                finally:
                    # Metadata remains available, but no previous level's cache
                    # should compete with the next reader's half-budget.
                    src.close()
    if on_progress is not None:
        on_progress(100.0, "overview complete")
    return ovr_path
