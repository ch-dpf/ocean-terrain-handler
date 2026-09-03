from pathlib import Path

from app.services.raster.geotiff import GeoTiffReader

cands = [
    Path("/data/workspace/source/smoke_dem.tif"),
    Path("/data/workspace/source/gDEM_S/s85e80.tif"),
    Path("/data/workspace/source/gDEM_N/n0e0.tif"),
]
root = Path("/data/workspace/source")
extras = sorted(
    [
        p
        for p in root.rglob("*.tif")
        if 20 * 1024 * 1024 <= p.stat().st_size <= 40 * 1024 * 1024
    ],
    key=lambda p: p.stat().st_size,
)
print("extras mid", [(str(p), round(p.stat().st_size / 1e6, 1)) for p in extras[:5]])
for p in cands:
    if not p.exists():
        print("MISSING", p)
        continue
    with GeoTiffReader(p, preload=False) as src:
        print(
            f"{p.name}: size={p.stat().st_size / 1e6:.1f}MB "
            f"shape={src.width}x{src.height} crs={src.crs} nodata={src.nodata} "
            f"bounds={src.bounds} dtype={src.dtype}"
        )
