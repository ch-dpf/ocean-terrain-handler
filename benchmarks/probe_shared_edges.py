"""Locate the worst in-coverage seam and isolate sampling vs mesh activation."""

import json
from pathlib import Path

import numpy as np
from evaluate_branches import current_api
from evaluate_mesh_accuracy import decode, raster, reference, surface

current_api()
from app.services.ctb.grid import TileCoordinate, global_geodetic
from app.services.ctb.sample import sample_tile_heights
from app.services.raster.geotiff import GeoTiffReader

root = Path("/data/tiling_production")
result = []
for name, region in [("s5e130", "S"), ("n0e0", "N")]:
    source, geo, nodata = raster(Path("/source") / f"gDEM_{region}" / f"{name}.tif")
    tiles = root / "paired1/current" / name / "tiles/10"
    cache = {(int(p.parent.name), int(p.stem)): decode(p)[:2] for p in tiles.rglob("*.terrain")}
    ts = np.linspace(0.05, 0.95, 19)
    worst = None
    for (x, y), (v, t) in cache.items():
        for axis, other, p1, p2 in [
            ("x", (x + 1, y), np.c_[np.ones(19), ts], np.c_[np.zeros(19), ts]),
            ("y", (x, y + 1), np.c_[ts, np.ones(19)], np.c_[ts, np.zeros(19)]),
        ]:
            if other not in cache:
                continue
            lon = -180 + (x + p1[:, 0]) * 180 / 1024
            lat = -90 + (y + p1[:, 1]) * 180 / 1024
            valid = np.isfinite(reference(source, geo, nodata, lon, lat))
            delta = surface(v, t, p1) - surface(*cache[other], p2)
            for k in np.flatnonzero(valid):
                if worst is None or abs(delta[k]) > worst["max"]:
                    worst = {
                        "sample": name,
                        "tile": [x, y],
                        "neighbor": list(other),
                        "axis": axis,
                        "max": float(abs(delta[k])),
                        "lon": float(lon[k]),
                        "lat": float(lat[k]),
                        "fraction": float(ts[k]),
                    }
    with GeoTiffReader(
        Path("/data/preprocess_optimized/main_1") / name / "preprocessed.tif", preload=False
    ) as src:
        grid = global_geodetic(65)
        a = sample_tile_heights(src, grid, TileCoordinate(10, *worst["tile"]), "average")
        b = sample_tile_heights(src, grid, TileCoordinate(10, *worst["neighbor"]), "average")
    av, bv = (a[:, -1], b[:, 0]) if worst["axis"] == "x" else (a[0, :], b[-1, :])
    worst["sample_grid_max_difference"] = float(abs(av - bv).max())
    worst["sample_grid_a"] = av.tolist()
    worst["sample_grid_b"] = bv.tolist()
    for label, coord in [("a", worst["tile"]), ("b", worst["neighbor"])]:
        v, t = cache[tuple(coord)]
        dim = 0 if worst["axis"] == "x" else 1
        edge = 1 if label == "a" else 0
        worst[f"edge_vertices_{label}"] = v[np.isclose(v[:, dim], edge)].tolist()
    result.append(worst)
out = root / "shared_edge_probe.json"
out.write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
