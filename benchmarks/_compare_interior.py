"""Compare only max-zoom tiles fully inside DEM bounds (reduces skirt/nodata effects)."""

from __future__ import annotations

import gzip
import json
import statistics as stats
import struct
from pathlib import Path

from app.services.ctb.grid import global_geodetic, TileCoordinate
from app.services.raster.geotiff import GeoTiffReader


def qm(path: Path) -> tuple[float, float]:
    raw = gzip.decompress(path.read_bytes())
    return struct.unpack_from("<ff", raw, 24)


def dem_bounds(path: Path) -> tuple[float, float, float, float]:
    with GeoTiffReader(path, preload=False) as src:
        return src.bounds


def fully_inside(tile_bounds, dem) -> bool:
    return (
        tile_bounds.minx >= dem[0]
        and tile_bounds.miny >= dem[1]
        and tile_bounds.maxx <= dem[2]
        and tile_bounds.maxy <= dem[3]
    )


def compare(dem_name: str, source: Path, zoom: int = 10) -> dict:
    dem = dem_bounds(source)
    grid = global_geodetic(65)
    main_dir = Path(f"data/jobs/_bench/main/{dem_name}/tiles/{zoom}")
    zy_dir = Path(f"data/jobs/_bench/_zy_tiles/{dem_name}/{zoom}")
    diffs = []
    for p in main_dir.rglob("*.terrain"):
        rel = p.relative_to(main_dir.parent)
        # rel = zoom/x/y.terrain
        z, x, name = rel.parts
        y = int(name.replace(".terrain", ""))
        x_i = int(x)
        tb = grid.tile_bounds(TileCoordinate(int(z), x_i, y))
        if not fully_inside(tb, dem):
            continue
        zy = zy_dir / x / name
        if not zy.exists():
            continue
        a = qm(p)
        b = qm(zy)
        diffs.append(
            {
                "tile": f"{z}/{x}/{y}",
                "dmin": abs(a[0] - b[0]),
                "dmax": abs(a[1] - b[1]),
                "main": a,
                "zy": b,
            }
        )
    if not diffs:
        return {"dem": dem_name, "compared": 0}
    dmins = [d["dmin"] for d in diffs]
    dmaxs = [d["dmax"] for d in diffs]
    worst = max(diffs, key=lambda d: max(d["dmin"], d["dmax"]))
    return {
        "dem": dem_name,
        "zoom": zoom,
        "compared": len(diffs),
        "dmin_mean_m": round(stats.mean(dmins), 4),
        "dmin_max_m": round(max(dmins), 4),
        "dmax_mean_m": round(stats.mean(dmaxs), 4),
        "dmax_max_m": round(max(dmaxs), 4),
        "within_0_5m": sum(1 for d in diffs if d["dmin"] < 0.5 and d["dmax"] < 0.5),
        "within_1m": sum(1 for d in diffs if d["dmin"] < 1.0 and d["dmax"] < 1.0),
        "within_5m": sum(1 for d in diffs if d["dmin"] < 5.0 and d["dmax"] < 5.0),
        "worst_tile": worst["tile"],
        "worst_main": worst["main"],
        "worst_zy": worst["zy"],
    }


def main() -> None:
    rows = [
        compare("s85e80", Path("data/source/gDEM_S/s85e80.tif")),
        compare("s5e130", Path("data/source/gDEM_S/s5e130.tif")),
        compare("n0e0", Path("data/source/gDEM_N/n0e0.tif")),
    ]
    Path("data/jobs/_bench/accuracy_interior_z10.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
