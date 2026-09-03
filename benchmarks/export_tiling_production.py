"""Export reproducible evidence and explicit production readiness failures."""
import argparse
import hashlib
import json
import shutil
import statistics
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--prefix', default='paired')
args = p.parse_args()
root = Path('/data/tiling_production')
out = Path('/export')
out.mkdir(parents=True, exist_ok=True)
rows = [json.loads(p.read_text()) for run in (1,2,3)
        for p in sorted((root/f'{args.prefix}{run}').glob('*/*/measurement.json'))]
(out/'timings.json').write_text(json.dumps(rows, indent=2))
summary = {}
failures = []
for sample in ('s85e80', 's5e130', 'n0e0'):
    result = {}
    assert len({r['input_sha256'] for r in rows if r['sample'] == sample}) == 1
    for branch in ('main', 'current'):
        group = [r for r in rows if r['sample']==sample and r['branch']==branch]
        assert len(group) == 3 and len({r['input_sha256'] for r in group}) == 1
        result[branch] = {'median_seconds': statistics.median(r['seconds'] for r in group),
                          'min_seconds': min(r['seconds'] for r in group),
                          'max_seconds': max(r['seconds'] for r in group),
                          'median_peak_process_rss_mib': statistics.median(r['peak_process_rss_mib'] for r in group)}
    result['ratio'] = result['current']['median_seconds']/result['main']['median_seconds']
    digests = []
    for run in (1,2,3):
        tile_root = root/f'{args.prefix}{run}'/'current'/sample/'tiles'
        digest = hashlib.sha256()
        for p in sorted(tile_root.rglob('*.terrain')):
            digest.update(p.relative_to(tile_root).as_posix().encode())
            digest.update(hashlib.sha256(p.read_bytes()).digest())
        digests.append(digest.hexdigest())
    result['repeatable_tiles'] = len(set(digests)) == 1
    result['tile_set_sha256'] = digests
    summary[sample] = result
    if result['ratio'] > 1.2:
        failures.append(f'{sample}: tiling time exceeds 1.2x main')
    if not result['repeatable_tiles']:
        failures.append(f'{sample}: tile bytes differ across repeated runs')
accuracy_path = root/f'{args.prefix}1/accuracy.json'
accuracy = json.loads(accuracy_path.read_text())
for row in accuracy:
    if row['invalid_horizon']['current']:
        failures.append(f"{row['sample']}: non-finite root horizon remains")
failures += ['Cesium browser rendering/culling acceptance incomplete',
             'Long-running production concurrency/soak acceptance incomplete',
             'Full input/projection/NODATA compatibility matrix incomplete',
             'Shared-edge and interior accuracy tolerances not yet accepted']
if args.prefix == 'globalsupport':
    failures += ['Mixed-LOD edge continuity remains unaccepted',
                 'Synthetic root boundary differs from adjacent source-supported root',
                 'Subpixel coverage without a supported zoom-lattice point remains unaccepted']
(out/'summary.json').write_text(json.dumps(summary, indent=2))
shutil.copy2(accuracy_path, out/'accuracy.json')
if args.prefix == 'canonicaledges' and (root/'cesium_acceptance.json').exists():
    shutil.copy2(root/'cesium_acceptance.json', out/'cesium_smoke.json')
lod_path = root/f'{args.prefix}1/lod_seams.json'
if lod_path.exists():
    shutil.copy2(lod_path, out/'lod_seams.json')
main_lod = root/f'{args.prefix}1/main_lod_seams.json'
if main_lod.exists():
    shutil.copy2(main_lod, out/'main_lod_seams.json')
source_files = ['app/services/ctb/sample.py', 'app/services/ctb/tiler.py',
                'app/services/ctb/checkpoint.py', 'app/services/ctb/native/heightfield.hpp',
                'app/services/ctb/native/mesh_tile.cpp']
(out/'source_sha256.json').write_text(json.dumps({
    path: hashlib.sha256((Path('/code')/path).read_bytes()).hexdigest()
    for path in source_files
}, indent=2))
shutil.copy2(root/'before/current/n0e0/measurement.json', out/'profile_before.json')
shutil.copy2(root/'support/current/n0e0/measurement.json', out/'profile_first_iteration.json')
native = next(Path('/data/preprocess_optimized_native').glob('*.so'))
(out/'native_sha256.txt').write_text(hashlib.sha256(native.read_bytes()).hexdigest()+'  '+native.name+'\n')
(out/'readiness.json').write_text(json.dumps({'production_ready': False, 'open_gates': failures}, indent=2))
print(json.dumps({'summary': summary, 'production_ready': False, 'open_gates': failures}, indent=2))
