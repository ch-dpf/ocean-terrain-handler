"""Generate committed reference arrays with GDAL 3.12.4, not the implementation."""

import json
from pathlib import Path

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()
result = {"gdal": gdal.VersionInfo("RELEASE_NAME"), "fill": []}
for name in ["hole", "diagonal"]:
    if name == "hole":
        y, x = np.mgrid[:17, :19]
        a = (x * x + 3 * y * y - 700).astype("float32")
        a[5:12, 6:13] = -32768
    else:
        a = np.full((31, 31), -32768, dtype="float32")
        a[10, 10] = 100
        a[20, 20] = 200
    d = gdal.GetDriverByName("MEM").Create("", a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    band = d.GetRasterBand(1)
    band.SetNoDataValue(-32768)
    band.WriteArray(a)
    gdal.FillNodata(band, None, 10, 0)
    result["fill"].append(
        {"name": name, "input": a.tolist(), "expected": band.ReadAsArray().tolist()}
    )
y, x = np.mgrid[:17, :19]
a = (x * x + 3 * y * y - 700).astype("float32")
a[5:9, 6:10] = -32768
d = gdal.GetDriverByName("GTiff").Create("/tmp/golden_overviews.tif", 19, 17, 1, gdal.GDT_Float32)
d.GetRasterBand(1).SetNoDataValue(-32768)
d.GetRasterBand(1).WriteArray(a)
d.BuildOverviews("AVERAGE", [2, 4, 8, 16])
result["overview"] = {
    "input": a.tolist(),
    "expected": [d.GetRasterBand(1).GetOverview(i).ReadAsArray().tolist() for i in range(4)],
}
result["grid"] = []
for source, target, transform in [
    (32650, 4326, [400000, 30, 0, 3500000, 0, -30]),
    (3857, 4326, [13000000, 100, 0, 3000000, 0, -100]),
    (4326, 3857, [130, 0.001, 0.0002, -5, 0.0001, -0.001]),
]:
    src = gdal.GetDriverByName("MEM").Create("", 515, 513, 1, gdal.GDT_Float32)
    crs = osr.SpatialReference()
    crs.ImportFromEPSG(source)
    src.SetProjection(crs.ExportToWkt())
    src.SetGeoTransform(transform)
    dst = gdal.Warp("", src, format="MEM", dstSRS=f"EPSG:{target}", resampleAlg="bilinear")
    result["grid"].append(
        {
            "source": source,
            "target": target,
            "transform": transform,
            "width": 515,
            "height": 513,
            "expected_transform": dst.GetGeoTransform(),
            "expected_size": [dst.RasterXSize, dst.RasterYSize],
        }
    )
Path("/export/preprocess_gdal_3124.json").write_text(json.dumps(result, indent=2))
