"""End-to-end terrain benchmark: GeoTIFF sampling + meshing + encoding + writes."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from app.schemas import CtbOptions, OutputFormat, Profile, ResamplingMethod
from app.services.ctb.mesh_encode import require_native
from app.services.ctb.tiler import run_ctb_tile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark a real preprocessed DEM through the complete terrain tile path."
    )
    parser.add_argument("input", type=Path, help="Georeferenced DEM GeoTIFF")
    parser.add_argument("output", type=Path, help="Disposable output directory")
    parser.add_argument("--start-zoom", type=int)
    parser.add_argument("--end-zoom", type=int, default=0)
    parser.add_argument("--threads", type=int)
    parser.add_argument(
        "--profile",
        choices=[member.value for member in Profile],
        default=Profile.GEODETIC.value,
    )
    parser.add_argument(
        "--resampling",
        choices=[member.value for member in ResamplingMethod],
        default=ResamplingMethod.AVERAGE.value,
    )
    parser.add_argument(
        "--minimum-tiles-per-second",
        type=float,
        help="Exit nonzero if measured throughput is lower.",
    )
    parser.add_argument("--keep-output", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.input.is_file():
        raise SystemExit(f"input not found: {args.input}")
    if args.output.exists():
        shutil.rmtree(args.output)
    require_native()

    options = CtbOptions(
        output_format=OutputFormat.MESH,
        profile=Profile(args.profile),
        start_zoom=args.start_zoom,
        end_zoom=args.end_zoom,
        thread_count=args.threads,
        resampling_method=ResamplingMethod(args.resampling),
        cesium_friendly=True,
        vertex_normals=True,
    )
    started = time.perf_counter()
    run_ctb_tile(args.input, args.output, options)
    elapsed = time.perf_counter() - started
    tile_count = 0
    tile_bytes = 0
    for path in args.output.rglob("*.terrain"):
        tile_count += 1
        tile_bytes += path.stat().st_size
    rate = tile_count / elapsed if elapsed > 0.0 else float("inf")
    result = {
        "input": str(args.input),
        "elapsed_seconds": round(elapsed, 6),
        "tiles": tile_count,
        "tiles_per_second": round(rate, 3),
        "terrain_bytes": tile_bytes,
        "profile": args.profile,
        "resampling": args.resampling,
        "threads": args.threads,
        "start_zoom": args.start_zoom,
        "end_zoom": args.end_zoom,
    }
    print(json.dumps(result, indent=2, sort_keys=True))

    failed = (
        args.minimum_tiles_per_second is not None
        and rate < args.minimum_tiles_per_second
    )
    if not args.keep_output:
        shutil.rmtree(args.output, ignore_errors=True)
    if failed:
        raise SystemExit(
            f"throughput {rate:.3f} tiles/s is below "
            f"{args.minimum_tiles_per_second:.3f} tiles/s"
        )


if __name__ == "__main__":
    main()
