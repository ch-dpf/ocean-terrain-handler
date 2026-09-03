"""Generate a deterministic tiled DEM for repeatable CI throughput checks."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from pyproj import CRS

from app.services.raster.affine import Affine
from app.services.raster.geotiff import write_geotiff_array
from app.services.raster.overviews import add_overviews


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--size", type=int, default=2048)
    args = parser.parse_args()
    if args.size < 256:
        raise SystemExit("size must be at least 256")

    x = np.arange(args.size, dtype=np.float32)[None, :]
    y = np.arange(args.size, dtype=np.float32)[:, None]
    heights = 150.0 * np.sin(x / 80.0) + 80.0 * np.cos(y / 65.0) + 0.01 * x
    write_geotiff_array(
        args.output,
        heights.astype(np.float32),
        affine=Affine.north_up(116.0, 40.0, 1.0 / args.size, 1.0 / args.size),
        crs=CRS.from_epsg(4326),
        compress="DEFLATE",
        block_size=256,
        nodata=-9999.0,
    )
    add_overviews(
        args.output,
        levels=(2, 4, 8, 16),
        block_size=256,
        workers=4,
    )


if __name__ == "__main__":
    main()
