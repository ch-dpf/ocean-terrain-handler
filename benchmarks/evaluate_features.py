import gzip
import hashlib
import json
from pathlib import Path
import struct
import sys
import time
import types
from evaluate_branches import main_api,current_api

b=sys.argv[1]
_,tile,F,P=main_api() if b=='main' else current_api()
root=Path('/data/eval_20260903')
source=root/f'{b}_r1_t4/s5e130/preprocess/preprocessed.tif'
base=dict(output_format=F.MESH,profile=P.GEODETIC,start_zoom=7,end_zoom=6,
          thread_count=4,tile_size=65,resampling_method=types.SimpleNamespace(value='average'),
          error_threshold=.125,warp_memory=None,resume=False,mesh_qfactor=1.,layer_only=False,
          cesium_friendly=True,vertex_normals=True,quiet=False,verbose=False,creation_options=[])
rows=[]
for name,patch in [('layer_only',dict(layer_only=True)),('heightmap',dict(output_format=F.TERRAIN)),
                   ('mercator_mesh65',dict(profile=P.MERCATOR))]:
    out=root/f'features_{b}'/name
    opts=types.SimpleNamespace(**(base|patch))
    t=time.perf_counter()
    try:
        tile(source,out,opts,gdal_cachemax=512)
        files=list(out.rglob('*.terrain'))
        row=dict(case=name,ok=True,seconds=time.perf_counter()-t,tiles=len(files),layer_exists=(out/'layer.json').exists())
        if name=='heightmap':
            heights=[]
            for f in files:
                raw=gzip.decompress(f.read_bytes())
                assert len(raw)==65*65*2+2
                heights.extend(x/5-1000 for x in struct.unpack_from('<4225H',raw))
            row['decoded_height_range']=[min(heights),max(heights)]
        if (out/'layer.json').exists(): row['layer']=json.loads((out/'layer.json').read_text())
    except Exception as exc: row=dict(case=name,ok=False,error=str(exc))
    rows.append(row)
out=root/f'{b}_r1_t4/s5e130/tiles'
before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*.terrain')}
opts=types.SimpleNamespace(**(base|dict(start_zoom=10,end_zoom=0,tile_size=None,resume=True)))
t=time.perf_counter(); tile(source,out,opts,gdal_cachemax=512)
rows.append(dict(case='resume',seconds=time.perf_counter()-t,tiles=len(before),
                 changed=sum(hashlib.sha256(p.read_bytes()).hexdigest()!=h for p,h in before.items())))
(root/f'features_{b}.json').write_text(json.dumps(rows,indent=2))
print(json.dumps(rows))
