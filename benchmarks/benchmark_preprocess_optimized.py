"""One sample/process for comparable stage timing and peak-process RSS."""

import json
import resource
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from evaluate_branches import current_api, main_api

branch, run, name = sys.argv[1:]
pre, *_ = main_api() if branch == "main" else current_api()
out = Path("/data/preprocess_optimized") / f"{branch}_{run}" / name
out.mkdir(parents=True, exist_ok=False)
stages = {}


def timed(name, fn):
    def wrapper(*args, **kwargs):
        t = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            stages[name] = stages.get(name, 0) + time.perf_counter() - t

    return wrapper


ns = pre.__globals__
if branch == "main":
    original = ns["_run_gdal"]

    def gdal_stage(cmd, **kwargs):
        return timed(cmd[2], original)(cmd, **kwargs)

    ns["_run_gdal"] = gdal_stage
else:
    for key in ["reproject_geotiff", "fill_nodata_geotiff", "add_overviews"]:
        ns[key] = timed(key, ns[key])
source = Path("/source") / ("gDEM_N" if name == "n0e0" else "gDEM_S") / (name + ".tif")
options = SimpleNamespace(
    target_crs="EPSG:4326",
    fill_nodata=True,
    build_overviews=True,
    block_size=256,
    nodata_value=None,
)
t = time.perf_counter()
final = pre(source, out, options, 512)
elapsed = time.perf_counter() - t
row = {
    "branch": branch,
    "run": int(run),
    "sample": name,
    "seconds": elapsed,
    "stages": stages,
    "peak_process_rss_mib": max(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    )
    / 1024,
    "final_bytes": sum(p.stat().st_size for p in out.glob("preprocessed.tif*")),
    "all_preprocess_bytes": sum(p.stat().st_size for p in out.iterdir() if p.is_file()),
}
(out / "measurement.json").write_text(json.dumps(row, indent=2))
print(json.dumps(row), flush=True)
