"""Build current native code in an isolated container, then run regression tests."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path("/tmp/preprocess_checks")
root.mkdir(exist_ok=True)
for name in ["app", "tests"]:
    shutil.copytree(Path("/code") / name, root / name, dirs_exist_ok=True)
for name in [
    "setup.py",
    "pyproject.toml",
    "README.md",
    "LICENSE.md",
    "THIRD_PARTY_NOTICES.md",
    "MANIFEST.in",
]:
    shutil.copy2(Path("/code") / name, root / name)
out = Path("/data/preprocess_optimized_native")
out.mkdir(exist_ok=True)
if "--reuse" in sys.argv:
    sys.argv.remove("--reuse")
    for p in out.glob("*.so"):
        shutil.copy2(p, root / "app/services/ctb" / p.name)
else:
    subprocess.run([sys.executable, "setup.py", "build_ext", "--inplace"], cwd=root, check=True)
for p in (root / "app/services/ctb").glob("*.so"):
    shutil.copy2(p, out / p.name)
env = os.environ.copy()
env["PYTHONPATH"] = str(root)
subprocess.run([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], cwd=root, env=env, check=True)
