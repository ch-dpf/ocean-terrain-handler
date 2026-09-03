"""Decode same-level and mixed-LOD edge surfaces, including outside coverage."""

import argparse
import json
from pathlib import Path

import numpy as np
from evaluate_mesh_accuracy import decode, raster, reference, surface

p = argparse.ArgumentParser()
p.add_argument("label")
p.add_argument("--branch", choices=("current", "main"), default="current")
args = p.parse_args()
root = Path("/data/tiling_production") / args.label
report = []
t = np.linspace(0, 1, 33)
for name, region in (("s85e80", "S"), ("s5e130", "S"), ("n0e0", "N")):
    source, geo, nd = raster(Path(f"/source/gDEM_{region}/{name}.tif"))
    tile_root = root / args.branch / name / "tiles"
    previous = {}
    for z in range(11):
        current = {
            (int(f.parent.name), int(f.stem)): decode(f)[:2]
            for f in (tile_root / str(z)).rglob("*.terrain")
        }
        values = {"same_all": [], "same_valid": [], "mixed_all": [], "mixed_valid": []}
        for (x, y), mesh in current.items():
            for dx, dy, a in (
                (1, 0, np.c_[np.ones_like(t), t]),
                (-1, 0, np.c_[np.zeros_like(t), t]),
                (0, 1, np.c_[t, np.ones_like(t)]),
                (0, -1, np.c_[t, np.zeros_like(t)]),
            ):
                lon = -180 + (x + a[:, 0]) * 180 / 2**z
                lat = -90 + (y + a[:, 1]) * 180 / 2**z
                valid = np.isfinite(reference(source, geo, nd, lon, lat))
                neighbor = (x + dx, y + dy)
                targets = []
                if neighbor in current and (dx > 0 or dy > 0):
                    targets.append(("same", current[neighbor], a - [dx, dy]))
                parent = (neighbor[0] // 2, neighbor[1] // 2)
                if parent != (x // 2, y // 2) and parent in previous:
                    targets.append(("mixed", previous[parent], (a + [x, y]) / 2 - parent))
                for kind, other, b in targets:
                    delta = surface(*mesh, a) - surface(*other, b)
                    if not np.isfinite(delta).all():
                        raise ValueError(f"Uncovered edge in decoded mesh: {name}/{z}/{x}/{y}")
                    values[kind + "_all"].extend(delta.tolist())
                    values[kind + "_valid"].extend(delta[valid].tolist())
        row = {"sample": name, "zoom": z}
        for key, v in values.items():
            a = np.asarray(v)
            row[key] = {
                "n": len(v),
                "max_m": float(abs(a).max()) if len(v) else None,
                "rmse_m": float(np.sqrt(np.mean(a * a))) if len(v) else None,
            }
        report.append(row)
        previous = current
output = root / ("lod_seams.json" if args.branch == "current" else "main_lod_seams.json")
output.write_text(json.dumps(report, indent=2))
print(json.dumps({"output": str(output), "rows": len(report)}))
