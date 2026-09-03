"""Compare whole decoded outputs using independent GDAL; export benchmark evidence."""

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
from osgeo import gdal

gdal.UseExceptions()
root = Path("/data/preprocess_optimized")
export = Path("/export")
export.mkdir(exist_ok=True, parents=True)
measurements = []
for branch, runs in [("main", (1, 2, 3)), ("current", (7, 8, 9))]:
    for run in runs:
        for path in sorted((root / f"{branch}_{run}").glob("*/measurement.json")):
            measurements.append(json.loads(path.read_text()))
(export / "timings.json").write_text(json.dumps(measurements, indent=2))
results = {}
for name in ["s85e80", "s5e130", "n0e0"]:
    a = gdal.Open(str(root / "main_1" / name / "preprocessed.tif"))
    b = gdal.Open(str(root / "current_7" / name / "preprocessed.tif"))
    row = {
        "main_size": [a.RasterXSize, a.RasterYSize],
        "current_size": [b.RasterXSize, b.RasterYSize],
        "crs_equal": bool(a.GetSpatialRef().IsSame(b.GetSpatialRef())),
        "affine_max_difference": max(
            abs(x - y) for x, y in zip(a.GetGeoTransform(), b.GetGeoTransform())
        ),
        "main_overviews": a.GetRasterBand(1).GetOverviewCount(),
        "current_overviews": b.GetRasterBand(1).GetOverviewCount(),
        "levels": [],
    }
    for i in range(-1, min(row["main_overviews"], row["current_overviews"])):
        x = a.GetRasterBand(1) if i < 0 else a.GetRasterBand(1).GetOverview(i)
        y = b.GetRasterBand(1) if i < 0 else b.GetRasterBand(1).GetOverview(i)
        item = {"level": i, "main_size": [x.XSize, x.YSize], "current_size": [y.XSize, y.YSize]}
        if (x.XSize, x.YSize) == (y.XSize, y.YSize):
            max_error = total = count = mismatch = 0
            for offset in range(0, x.YSize, 256):
                h = min(256, x.YSize - offset)
                xx = x.ReadAsArray(0, offset, x.XSize, h).astype("float64")
                yy = y.ReadAsArray(0, offset, y.XSize, h).astype("float64")
                mx = ~np.isfinite(xx) | (xx == x.GetNoDataValue())
                my = ~np.isfinite(yy) | (yy == y.GetNoDataValue())
                delta = abs(xx[~(mx | my)] - yy[~(mx | my)])
                mismatch += int(np.count_nonzero(mx != my))
                if delta.size:
                    max_error = max(max_error, float(delta.max()))
                    total += float(delta.sum())
                    count += delta.size
            item.update(
                max_error=max_error,
                mae=total / count if count else None,
                mask_disagreement=mismatch,
            )
        row["levels"].append(item)
    results[name] = row
(export / "whole_raster_parity.json").write_text(json.dumps(results, indent=2))
for name in ["current.json", "main.json"]:
    shutil.copy2(Path("/data/preprocess_probe_20260903") / name, export / ("stage_probe_" + name))
native = next(Path("/data/preprocess_optimized_native").glob("*.so"))
(export / "native_sha256.txt").write_text(
    hashlib.sha256(native.read_bytes()).hexdigest() + "  " + native.name + "\n"
)
shutil.copy2("/data/eval_20260903/current_r8_t4/timings.json", export / "end_to_end.json")
print(json.dumps(results, indent=2))
