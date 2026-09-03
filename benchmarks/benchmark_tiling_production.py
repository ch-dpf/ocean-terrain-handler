"""Paired tiler benchmark: both engines read the exact same baseline TIFF.

Run one process per branch/run/sample. Timers count cumulative thread time;
only seconds is wall time. No container startup is included.
"""

import argparse
import hashlib
import json
import resource
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from evaluate_branches import current_api, main_api

p = argparse.ArgumentParser()
p.add_argument("branch", choices=["main", "current"])
p.add_argument("label")
p.add_argument("sample")
p.add_argument("--threads", type=int, default=4)
p.add_argument(
    "--profile",
    action="store_true",
    help="Enable asymmetric diagnostic instrumentation, not fair timing",
)
args = p.parse_args()
_, tile, F, P = main_api() if args.branch == "main" else current_api()
source = Path("/data/preprocess_optimized/main_1") / args.sample / "preprocessed.tif"
out = Path("/data/tiling_production") / args.label / args.branch / args.sample
out.mkdir(parents=True, exist_ok=False)
stages = {}
lock = threading.Lock()


def instrument(module, name):
    original = getattr(module, name)

    def wrapped(*a, **kw):
        start = time.perf_counter()
        try:
            return original(*a, **kw)
        finally:
            elapsed = time.perf_counter() - start
            with lock:
                row = stages.setdefault(name, {"calls": 0, "thread_seconds": 0})
                row["calls"] += 1
                row["thread_seconds"] += elapsed

    setattr(module, name, wrapped)


if args.branch == "current" and args.profile:
    from app.services.ctb import mesh_encode, sample, tiler
    from app.services.raster.geotiff import GeoTiffReader

    instrument(sample, "warp_window")
    instrument(tiler, "sample_tile_heights")
    instrument(tiler, "_atomic_write")
    instrument(mesh_encode, "gzip_terrain")
    instrument(mesh_encode._native_module, "encode_mesh_tile_bytes")
    instrument(GeoTiffReader, "read_window")
options = SimpleNamespace(
    output_format=F.MESH,
    profile=P.GEODETIC,
    start_zoom=10,
    end_zoom=0,
    thread_count=args.threads,
    tile_size=65,
    resampling_method=SimpleNamespace(value="average"),
    error_threshold=0.125,
    warp_memory=None,
    resume=False,
    mesh_qfactor=1.0,
    layer_only=False,
    cesium_friendly=True,
    vertex_normals=True,
    quiet=False,
    verbose=False,
    creation_options=[],
)
digest = hashlib.sha256()
with source.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        digest.update(chunk)
start = time.perf_counter()
tile(source, out / "tiles", options, gdal_cachemax=512)
elapsed = time.perf_counter() - start
paths = list((out / "tiles").rglob("*.terrain"))
result = dict(
    branch=args.branch,
    label=args.label,
    sample=args.sample,
    threads=args.threads,
    profiled=args.profile,
    input=str(source),
    input_sha256=digest.hexdigest(),
    seconds=elapsed,
    tiles=len(paths),
    bytes=sum(x.stat().st_size for x in paths),
    stages=stages,
    peak_process_rss_mib=max(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    )
    / 1024,
)
(out / "measurement.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)
