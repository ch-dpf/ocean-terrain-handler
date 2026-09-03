import json
from pathlib import Path
import numpy as np
from evaluate_mesh_accuracy import decode,surface,raster,reference,ROOT

# Cross section through the western edge of the ocean DEM, within the source.
xs=np.linspace(.00045,.035,400)
lat=-4.412109375
src,geo,nd=raster(Path('/source/gDEM_N/n0e0.tif'))
y=np.full(xs.shape,lat)
result={'lon':xs.tolist(),'lat':lat,'source':reference(src,geo,nd,xs,y).tolist()}
for branch in ['main','current']:
    v,t,*_=decode(ROOT/f'{branch}_r1_t4/n0e0/tiles/10/1024/486.terrain')
    width=180/1024
    points=np.c_[xs/width,(y+90)/width-486]
    result[branch]=surface(v,t,points).tolist()
(ROOT/'profile.json').write_text(json.dumps(result,indent=2))
