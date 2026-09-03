import gzip
import json
import statistics as stats
import struct
import subprocess
from pathlib import Path


def qm(path: Path) -> tuple[float, float]:
    raw = gzip.decompress(path.read_bytes())
    return struct.unpack_from("<ff", raw, 24)


subprocess.run(
    [
        "docker",
        "compose",
        "cp",
        "worker:/data/workspace/jobs/_bench/main-zy.json",
        "data/jobs/_bench/main-zy.json",
    ],
    check=False,
)

for dem in ["n0e0", "s5e130", "s85e80"]:
    print("\n==", dem, "==")
    zy_dir = f"/data/workspace/jobs/_bench/main-zy/main-zy__{dem}/tiles"
    main_dir = Path(f"data/jobs/_bench/main/{dem}/tiles")
    diffs = []
    checked = 0
    for p in sorted(main_dir.rglob("*.terrain")):
        if checked >= 120:
            break
        rel = p.relative_to(main_dir).as_posix()
        local = Path(f"data/jobs/_bench/_zy_tiles/{dem}/{rel}")
        local.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["docker", "compose", "cp", f"worker:{zy_dir}/{rel}", str(local)],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0 or not local.exists():
            continue
        try:
            a = qm(p)
            b = qm(local)
        except Exception:
            continue
        checked += 1
        dmin = abs(a[0] - b[0])
        dmax = abs(a[1] - b[1])
        diffs.append((max(dmin, dmax), dmin, dmax, rel, a, b))
    diffs.sort(reverse=True)
    print("compared", checked)
    if checked:
        dmins = [d[1] for d in diffs]
        dmaxs = [d[2] for d in diffs]
        print(f"dmin: mean={stats.mean(dmins):.4f} p95={stats.quantiles(dmins, n=20)[18]:.4f} max={max(dmins):.4f}")
        print(f"dmax: mean={stats.mean(dmaxs):.4f} p95={stats.quantiles(dmaxs, n=20)[18]:.4f} max={max(dmaxs):.4f}")
        print("worst", diffs[0][3], "main", diffs[0][4], "zy", diffs[0][5])
        eq = sum(1 for d in diffs if d[1] < 0.01 and d[2] < 0.01)
        near1 = sum(1 for d in diffs if d[1] < 1.0 and d[2] < 1.0)
        print(f"near-equal(<1cm header): {eq}/{checked}; within 1m: {near1}/{checked}")

    lp = Path(f"data/jobs/_bench/main/{dem}/tiles/layer.json")
    print(
        dem,
        "main layer.json",
        lp.exists(),
        "size",
        lp.stat().st_size if lp.exists() else None,
    )
    if lp.exists():
        layer = json.loads(lp.read_text(encoding="utf-8"))
        print(" bounds", layer.get("bounds"), "levels", len(layer.get("available", [])))

# summarize speed table
zy = json.loads(Path("data/jobs/_bench/main-zy.json").read_text(encoding="utf-8"))
main = json.loads(Path("data/jobs/_bench/main.json").read_text(encoding="utf-8"))
print("\n== SPEED SUMMARY ==")
for dem in ["s85e80", "s5e130", "n0e0"]:
    z = next(r for r in zy["results"] if dem in r["label"])
    m = next(r for r in main["results"] if dem in r["label"])
    print(
        dem,
        "zy",
        z["seconds_preprocess"],
        z["seconds_tile"],
        z["seconds_total"],
        "main",
        m["seconds_preprocess"],
        m["seconds_tile"],
        m["seconds_total"],
        "speedup",
        round(m["seconds_total"] / z["seconds_total"], 2),
        "tiles",
        z["terrain_files"],
        m["terrain_files"],
        "hmin zy/main",
        z["height_extremes"].get("global_min_h"),
        m["height_extremes"].get("global_min_h"),
        "hmax zy/main",
        z["height_extremes"].get("global_max_h"),
        m["height_extremes"].get("global_max_h"),
    )
