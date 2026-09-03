"""Reproduce main-branch pipeline (GDAL CLI + Docker CTB) for fair timing."""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import struct
import subprocess
import time
from pathlib import Path


def win_bind(path: Path) -> str:
    text = str(path.resolve()).replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        return f"//{text[0].lower()}/{text[2:].lstrip('/')}"
    return text


def run(cmd: list[str], *, env_extra: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    env = None
    if env_extra:
        import os

        env = os.environ.copy()
        env.update(env_extra)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"cmd failed {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def gdal_cmd(workspace: Path, args: list[str], gdal_cachemax: int) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "-e",
        f"GDAL_CACHEMAX={gdal_cachemax}",
        "-v",
        f"{win_bind(workspace)}:/data",
        "ghcr.io/osgeo/gdal:ubuntu-small-3.12.4",
        *args,
    ]


def qm_minmax(path: Path) -> tuple[float, float] | None:
    try:
        raw = gzip.decompress(path.read_bytes())
        if len(raw) < 32:
            return None
        return struct.unpack_from("<ff", raw, 24)
    except Exception:
        return None


def height_extremes(tiles_dir: Path) -> dict:
    gmin, gmax = float("inf"), float("-inf")
    worst = None
    n = 0
    for path in tiles_dir.rglob("*.terrain"):
        mm = qm_minmax(path)
        if mm is None:
            continue
        n += 1
        gmin = min(gmin, mm[0])
        gmax = max(gmax, mm[1])
        if worst is None or mm[0] < worst[0]:
            worst = (mm[0], str(path.relative_to(tiles_dir)))
    if n == 0:
        return {"tile_headers": 0}
    return {
        "tile_headers": n,
        "global_min_h": gmin,
        "global_max_h": gmax,
        "lowest_min_tile": {"min_h": worst[0], "path": worst[1]} if worst else None,
    }


def preprocess(
    *,
    workspace: Path,
    input_rel: str,
    work_rel: str,
    gdal_cachemax: int,
) -> str:
    """Return container-relative path to preprocessed.tif."""
    warped = f"{work_rel}/warped.tif"
    filled = f"{work_rel}/filled.tif"
    final = f"{work_rel}/preprocessed.tif"
    run(
        gdal_cmd(
            workspace,
            [
                "gdal",
                "raster",
                "reproject",
                "--dst-crs",
                "EPSG:4326",
                "-r",
                "bilinear",
                "--co",
                "TILED=YES",
                "--co",
                "BLOCKXSIZE=256",
                "--co",
                "BLOCKYSIZE=256",
                "--co",
                "COMPRESS=DEFLATE",
                "--overwrite",
                f"/data/{input_rel}",
                f"/data/{warped}",
            ],
            gdal_cachemax,
        )
    )
    run(
        gdal_cmd(
            workspace,
            [
                "gdal",
                "raster",
                "fill-nodata",
                "--max-distance",
                "10",
                "--overwrite",
                f"/data/{warped}",
                f"/data/{filled}",
            ],
            gdal_cachemax,
        )
    )
    # overview add works in-place; copy filled -> final first
    src = workspace / filled
    dst = workspace / final
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    run(
        gdal_cmd(
            workspace,
            [
                "gdal",
                "raster",
                "overview",
                "add",
                "-r",
                "average",
                "--levels",
                "2,4,8,16",
                f"/data/{final}",
            ],
            gdal_cachemax,
        )
    )
    return final


def tile(
    *,
    workspace: Path,
    preprocessed_rel: str,
    tiles_rel: str,
    start_zoom: int,
    end_zoom: int,
    threads: int,
    gdal_cachemax: int,
) -> None:
    (workspace / tiles_rel).mkdir(parents=True, exist_ok=True)
    cmd = [
        "docker",
        "run",
        "--rm",
        "-e",
        f"GDAL_CACHEMAX={gdal_cachemax}",
        "-v",
        f"{win_bind(workspace)}:/data",
        "cesium-terrain-builder:local",
        "ctb-tile",
        "-o",
        f"/data/{tiles_rel}",
        "-f",
        "Mesh",
        "-p",
        "geodetic",
        "-r",
        "average",
        "-z",
        "0.125",
        "-c",
        str(threads),
        "-s",
        str(start_zoom),
        "-e",
        str(end_zoom),
        "-C",
        "-N",
        f"/data/{preprocessed_rel}",
    ]
    run(cmd)


def run_one(
    *,
    workspace: Path,
    input_path: Path,
    label: str,
    start_zoom: int,
    end_zoom: int,
    threads: int,
    gdal_cachemax: int,
) -> dict:
    input_rel = str(input_path.resolve().relative_to(workspace.resolve())).replace("\\", "/")
    work_rel = f"jobs/_bench/main/{label}"
    tiles_rel = f"{work_rel}/tiles"
    work_dir = workspace / work_rel
    if work_dir.exists():
        shutil.rmtree(work_dir)
    (work_dir / "preprocess").mkdir(parents=True)

    t0 = time.perf_counter()
    pre_rel = preprocess(
        workspace=workspace,
        input_rel=input_rel,
        work_rel=f"{work_rel}/preprocess",
        gdal_cachemax=gdal_cachemax,
    )
    t_pre = time.perf_counter() - t0

    t1 = time.perf_counter()
    tile(
        workspace=workspace,
        preprocessed_rel=pre_rel,
        tiles_rel=tiles_rel,
        start_zoom=start_zoom,
        end_zoom=end_zoom,
        threads=threads,
        gdal_cachemax=gdal_cachemax,
    )
    t_tile = time.perf_counter() - t1

    tiles_dir = workspace / tiles_rel
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
    terrain_files = sum(1 for _ in tiles_dir.rglob("*.terrain"))
    pre_path = workspace / pre_rel
    return {
        "label": f"main__{label}",
        "mode": "docker_ctb_host_replay",
        "input": str(input_path),
        "input_bytes": input_path.stat().st_size,
        "start_zoom": start_zoom,
        "end_zoom": end_zoom,
        "thread_count": threads,
        "seconds_preprocess": round(t_pre, 3),
        "seconds_tile": round(t_tile, 3),
        "seconds_total": round(t_pre + t_tile, 3),
        "preprocessed_bytes": pre_path.stat().st_size if pre_path.is_file() else None,
        "terrain_files": terrain_files,
        "layer_bounds": layer.get("bounds") if layer else None,
        "layer_format": layer.get("format") if layer else None,
        "available_tile_boxes": available_counts,
        "available_tile_box_sum": sum(available_counts),
        "height_extremes": height_extremes(tiles_dir),
        "output_dir": str(tiles_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start-zoom", type=int, default=10)
    parser.add_argument("--end-zoom", type=int, default=0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--gdal-cachemax", type=int, default=512)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    rows = []
    for path in args.inputs:
        path = path.resolve()
        label = path.stem
        print(f"=== main__{label} ===", flush=True)
        row = run_one(
            workspace=args.workspace.resolve(),
            input_path=path,
            label=label,
            start_zoom=args.start_zoom,
            end_zoom=args.end_zoom,
            threads=args.threads,
            gdal_cachemax=args.gdal_cachemax,
        )
        print(
            json.dumps(
                {
                    k: row[k]
                    for k in (
                        "mode",
                        "seconds_preprocess",
                        "seconds_tile",
                        "seconds_total",
                        "terrain_files",
                        "layer_bounds",
                        "height_extremes",
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        rows.append(row)

    payload = {
        "branch_label": "main",
        "note": "Host replay of main pipeline: GDAL 3.12.4 container + cesium-terrain-builder:local (same tools as main Dockerfile/ctb_runner).",
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
