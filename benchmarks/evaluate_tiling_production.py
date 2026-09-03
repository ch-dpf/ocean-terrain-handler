"""Independent source-reference surface comparison for shared-input tiler runs."""

import argparse
from pathlib import Path

from evaluate_mesh_accuracy import main

p = argparse.ArgumentParser()
p.add_argument("label")
p.add_argument("--samples", nargs="+")
p.add_argument("--main-label", help="Reuse an existing reference output for accuracy only")
args = p.parse_args()
root = Path("/data/tiling_production") / args.label
main(
    main_root=(Path("/data/tiling_production") / args.main_label / "main")
    if args.main_label
    else root / "main",
    current_root=root / "current",
    output=root / "accuracy.json",
    samples=args.samples,
    preprocess_root=Path("/data/preprocess_optimized/main_1"),
)
