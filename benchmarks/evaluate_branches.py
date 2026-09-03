"""Isolated branch benchmark; see docs/BRANCH_EVALUATION_20260903.md."""
import argparse
import ast
import enum
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import types


def main_api():
    # Execute the baseline's actual functions with only its process/progress
    # dependencies adapted to this standalone container (no Celery/Redis).
    class OutputFormat(enum.Enum):
        MESH = 'Mesh'
        TERRAIN = 'Terrain'
    class Profile(enum.Enum):
        GEODETIC = 'geodetic'
        MERCATOR = 'mercator'
    def run(cmd, **kwargs):
        subprocess.run(cmd, env=kwargs.get('env'), check=True, capture_output=True, text=True)
    ns = dict(Path=Path, Callable=object, logging=logging, os=os, shutil=shutil,
              subprocess=subprocess, json=json, TypedDict=dict, OutputFormat=OutputFormat,
              Profile=Profile, PreprocessOptions=object, run_gdal_command=run,
              gdal_progress_flag_unsupported=lambda _: False, run_streaming_command=run)
    for name in ['preprocessor', 'ctb_runner']:
        path = Path('/baseline/app/services') / (name + '.py')
        tree = ast.parse(path.read_text())
        tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        tree.body.insert(0, ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0))
        exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), ns)
    def tile(input_path, output_dir, options, **kw):
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = ns['build_ctb_command'](input_path, output_dir, options, 'BASELINE_IMAGE',
                                      Path('/data'), 512)
        subprocess.run(cmd[cmd.index('BASELINE_IMAGE') + 1:], check=True, capture_output=True, text=True)
    return ns['preprocess_dem'], tile, OutputFormat, Profile


def current_api():
    # Use every Python file from the current worktree, and the installed native ABI.
    import app.services.ctb._ctb_core as native
    native_path = Path(native.__file__)
    if os.environ.get('PREPROCESS_NATIVE_DIR'):
        native_path = next(Path(os.environ['PREPROCESS_NATIVE_DIR']).glob('_ctb_core*.so'))
    for key in list(sys.modules):
        if key == 'app' or key.startswith('app.'):
            del sys.modules[key]
    dest = Path('/tmp/evaluation_app')
    shutil.copytree('/code/app', dest / 'app')
    shutil.copy2(native_path, dest / 'app/services/ctb' / native_path.name)
    sys.path.insert(0, str(dest))
    from app.services.preprocessor import preprocess_dem
    from app.services.ctb.tiler import run_ctb_tile
    from app.schemas import OutputFormat, Profile
    return preprocess_dem, run_ctb_tile, OutputFormat, Profile


def main():
    p = argparse.ArgumentParser()
    p.add_argument('branch', choices=['main', 'current'])
    p.add_argument('run', type=int)
    p.add_argument('--threads', type=int, default=4)
    args = p.parse_args()
    pre, tile, F, P = main_api() if args.branch == 'main' else current_api()
    root = Path('/data/eval_20260903') / f'{args.branch}_r{args.run}_t{args.threads}'
    root.mkdir(parents=True, exist_ok=False)
    opts = types.SimpleNamespace(output_format=F.MESH, profile=P.GEODETIC,
        start_zoom=10, end_zoom=0, thread_count=args.threads, tile_size=None,
        resampling_method=types.SimpleNamespace(value='average'), error_threshold=.125,
        warp_memory=None, resume=False, mesh_qfactor=1., layer_only=False,
        cesium_friendly=True, vertex_normals=True, quiet=False, verbose=False, creation_options=[])
    popts = types.SimpleNamespace(target_crs='EPSG:4326', fill_nodata=True,
                                 build_overviews=True, block_size=256, nodata_value=None)
    rows = []
    for name, region in [('s85e80', 'S'), ('s5e130', 'S'), ('n0e0', 'N')]:
        source = Path(f'/source/gDEM_{region}/{name}.tif')
        work = root / name
        t0 = time.perf_counter()
        dem = pre(source, work / 'preprocess', popts, 512)
        t1 = time.perf_counter()
        tile(dem, work / 'tiles', opts, gdal_cachemax=512)
        t2 = time.perf_counter()
        paths = list((work / 'tiles').rglob('*.terrain'))
        row = dict(branch=args.branch, run=args.run, threads=args.threads, sample=name,
                   preprocess_s=t1-t0, tile_s=t2-t1, total_s=t2-t0, tiles=len(paths),
                   tile_bytes=sum(x.stat().st_size for x in paths),
                   preprocess_bytes=sum(x.stat().st_size for x in (work/'preprocess').iterdir()),
                   final_dem_bytes=dem.stat().st_size,
                   final_with_overviews_bytes=sum(x.stat().st_size for x in dem.parent.glob('preprocessed.tif*')),
                   layer_exists=(work/'tiles/layer.json').exists())
        rows.append(row)
        (root/'timings.json').write_text(json.dumps(rows, indent=2))
        print(json.dumps(row), flush=True)


if __name__ == '__main__':
    main()
