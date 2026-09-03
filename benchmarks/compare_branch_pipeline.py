"""Branch-agnostic terrain pipeline micro-benchmark.

Times preprocess + tile, then records layer.json / tile / height stats.
Works on both main (GDAL + Docker CTB via ctb_runner) and main-zy (raster + tiler).
"""

from __future__ import annotations

import argparse
import gzip
import json
import struct
import time
from pathlib import Path

from app.config import get_settings
from app.schemas import CtbOptions, OutputFormat, PreprocessOptions, Profile


def _import_pipeline():
    """Resolve preprocess/tile entrypoints for whichever branch is installed."""
    from app.services.preprocessor import preprocess_dem

    try:
        from app.services.ctb.tiler import run_ctb_tile

        mode = "inprocess"
        return preprocess_dem, run_ctb_tile, mode
    except Exception:
        pass
    from app.services.ctb_runner import run_ctb_tile

    return preprocess_dem, run_ctb_tile, "docker_ctb"


def _count_terrain_files(tiles_dir: Path) -> int:
    return sum(1 for _ in tiles_dir.rglob("*.terrain"))


def _qm_minmax(path: Path) -> tuple[float, float] | None:
    try:
        raw = gzip.decompress(path.read_bytes())
        if len(raw) < 32:
            return None
        return struct.unpack_from("<ff", raw, 24)
    except Exception:
        return None


def _sample_tile_heights(tiles_dir: Path, limit: int = 12) -> list[dict]:
    samples: list[dict] = []
    for path in sorted(tiles_dir.rglob("*.terrain")):
        if path.name.startswith("."):
            continue
        parts = path.relative_to(tiles_dir).parts
        if len(parts) != 3:
            continue
        z, x, name = parts
        y = name.replace(".terrain", "")
        mm = _qm_minmax(path)
        if mm is None:
            continue
        samples.append(
            {
                "z": int(z),
                "x": int(x),
                "y": int(y),
                "min_h": mm[0],
                "max_h": mm[1],
                "bytes": path.stat().st_size,
            }
        )
        if len(samples) >= limit:
            break
    return samples


def _extreme_heights(tiles_dir: Path) -> dict:
    gmin, gmax = float("inf"), float("-inf")
    worst_neg = None
    n = 0
    for path in tiles_dir.rglob("*.terrain"):
        mm = _qm_minmax(path)
        if mm is None:
            continue
        n += 1
        gmin = min(gmin, mm[0])
        gmax = max(gmax, mm[1])
        if worst_neg is None or mm[0] < worst_neg[0]:
            worst_neg = (mm[0], str(path.relative_to(tiles_dir)))
    if n == 0:
        return {"tile_headers": 0}
    return {
        "tile_headers": n,
        "global_min_h": gmin,
        "global_max_h": gmax,
        "lowest_min_tile": {"min_h": worst_neg[0], "path": worst_neg[1]} if worst_neg else None,
    }


def run_one(
    *,
    label: str,
    input_path: Path,
    work_root: Path,
    start_zoom: int | None,
    end_zoom: int,
    thread_count: int,
) -> dict:
    preprocess_dem, run_ctb_tile, mode = _import_pipeline()
    settings = get_settings()
    job_dir = work_root / label
    if job_dir.exists():
        import shutil

        shutil.rmtree(job_dir)
    preprocess_dir = job_dir / "preprocess"
    tiles_dir = job_dir / "tiles"
    preprocess_dir.mkdir(parents=True)
    tiles_dir.mkdir(parents=True)

    preprocess_opts = PreprocessOptions(
        target_crs="EPSG:4326",
        fill_nodata=True,
        build_overviews=True,
        block_size=256,
    )
    ctb_opts = CtbOptions(
        output_format=OutputFormat.MESH,
        profile=Profile.GEODETIC,
        start_zoom=start_zoom,
        end_zoom=end_zoom,
        thread_count=thread_count,
        cesium_friendly=True,
        vertex_normals=True,
        resume=False,
    )

    t0 = time.perf_counter()
    preprocessed = preprocess_dem(
        input_path=input_path,
        work_dir=preprocess_dir,
        options=preprocess_opts,
        gdal_cachemax=settings.gdal_cachemax,
    )
    t_pre = time.perf_counter() - t0

    t1 = time.perf_counter()
    kwargs = {
        "input_path": preprocessed,
        "output_dir": tiles_dir,
        "options": ctb_opts,
        "gdal_cachemax": settings.gdal_cachemax,
    }
    if mode == "docker_ctb":
        kwargs.update(
            {
                "docker_image": settings.ctb_docker_image,
                "workspace_dir": settings.workspace_dir,
                "host_workspace_dir": settings.host_workspace_dir,
                "workspace_docker_volume": settings.workspace_docker_volume,
            }
        )
    run_ctb_tile(**kwargs)
    t_tile = time.perf_counter() - t1
    t_total = t_pre + t_tile

    layer_path = tiles_dir / "layer.json"
    layer = json.loads(layer_path.read_text(encoding="utf-8")) if layer_path.is_file() else None
    available_counts = []
    if layer and "available" in layer:
        for level in layer["available"]:
            if not level:
                available_counts.append(0)
                continue
            box = level[0]
            available_counts.append(
                (box["endX"] - box["startX"] + 1) * (box["endY"] - box["startY"] + 1)
            )

    result = {
        "label": label,
        "mode": mode,
        "input": str(input_path),
        "input_bytes": input_path.stat().st_size,
        "start_zoom": start_zoom,
        "end_zoom": end_zoom,
        "thread_count": thread_count,
        "seconds_preprocess": round(t_pre, 3),
        "seconds_tile": round(t_tile, 3),
        "seconds_total": round(t_total, 3),
        "preprocessed_bytes": preprocessed.stat().st_size if preprocessed.is_file() else None,
        "terrain_files": _count_terrain_files(tiles_dir),
        "layer_bounds": layer.get("bounds") if layer else None,
        "layer_format": layer.get("format") if layer else None,
        "available_tile_boxes": available_counts,
        "available_tile_box_sum": sum(available_counts),
        "height_extremes": _extreme_heights(tiles_dir),
        "sample_tiles": _sample_tile_heights(tiles_dir),
        "output_dir": str(tiles_dir),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--branch-label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--start-zoom", type=int, default=10)
    parser.add_argument("--end-zoom", type=int, default=0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    rows = []
    for path in args.inputs:
        path = path.resolve()
        stem = path.stem
        label = f"{args.branch_label}__{stem}"
        print(f"=== {label} ===", flush=True)
        row = run_one(
            label=label,
            input_path=path,
            work_root=args.work_root,
            start_zoom=args.start_zoom,
            end_zoom=args.end_zoom,
            thread_count=args.threads,
        )
        print(json.dumps({k: row[k] for k in (
            "mode", "seconds_preprocess", "seconds_tile", "seconds_total",
            "terrain_files", "layer_bounds", "height_extremes"
        )}, ensure_ascii=False), flush=True)
        rows.append(row)

    payload = {
        "branch_label": args.branch_label,
        "start_zoom": args.start_zoom,
        "end_zoom": args.end_zoom,
        "threads": args.threads,
        "results": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
