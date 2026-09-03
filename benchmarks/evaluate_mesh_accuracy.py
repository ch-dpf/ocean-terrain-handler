"""Independent quantized-mesh decoder and DEM-reference sampling (metres)."""
import gzip
import json
from pathlib import Path
import struct
import numpy as np
import tifffile

ROOT = Path('/data/eval_20260903')


def decode(path):
    b = gzip.decompress(path.read_bytes())
    lo, hi = struct.unpack_from('<ff', b, 24)
    assert np.isfinite(struct.unpack_from('<3d2f4d', b, 0)).all()
    n = struct.unpack_from('<I', b, 88)[0]
    a = np.frombuffer(b, '<u2', n*3, 92).astype(np.int64).reshape(3, n)
    a = np.cumsum((a >> 1) ^ -(a & 1), axis=1)
    assert n >= 3 and np.all((a >= 0) & (a <= 32767))
    v = a.T.astype(float)/32767
    v[:, 2] = lo + v[:, 2]*(hi-lo)
    off = 92+n*6
    size = 4 if n > 65536 else 2
    off = (off+size-1)//size*size
    nt = struct.unpack_from('<I', b, off)[0]
    off += 4
    codes = np.frombuffer(b, '<u4' if size == 4 else '<u2', nt*3, off).astype(np.int64)
    highest = np.cumsum(codes == 0) - (codes == 0)
    inds = highest-codes
    assert np.all((inds >= 0) & (inds < n))
    off += nt*3*size
    for _ in range(4):
        count = struct.unpack_from('<I', b, off)[0]
        off += 4
        edge = np.frombuffer(b, '<u4' if size == 4 else '<u2', count, off)
        assert np.all(edge < n)
        off += size*count
    exts = []
    while off < len(b):
        eid, length = struct.unpack_from('<BI', b, off)
        off += 5
        assert off+length <= len(b)
        if eid == 1:
            assert length == n*2
        exts.append(eid)
        off += length
    assert off == len(b)
    return v, inds.reshape(-1, 3), (lo, hi), exts


def surface(v, triangles, points):
    a,b,c = (v[triangles[:, i]] for i in range(3))
    den = (b[:,1]-c[:,1])*(a[:,0]-c[:,0])+(c[:,0]-b[:,0])*(a[:,1]-c[:,1])
    good = np.abs(den)>1e-15
    a,b,c,den = a[good],b[good],c[good],den[good]
    x,y = points[:,0,None],points[:,1,None]
    w1 = ((b[:,1]-c[:,1])*(x-c[:,0])+(c[:,0]-b[:,0])*(y-c[:,1]))/den
    w2 = ((c[:,1]-a[:,1])*(x-c[:,0])+(a[:,0]-c[:,0])*(y-c[:,1]))/den
    w3 = 1-w1-w2
    inside = (w1>=-1e-9)&(w2>=-1e-9)&(w3>=-1e-9)
    idx = inside.argmax(axis=1)
    out = (w1*a[:,2]+w2*b[:,2]+w3*c[:,2])[np.arange(len(points)),idx]
    out[~inside.any(axis=1)] = np.nan
    return out


def raster(path):
    with tifffile.TiffFile(path) as t:
        p = t.pages[0]
        sx,sy,_ = p.tags['ModelPixelScaleTag'].value
        tie = p.tags['ModelTiepointTag'].value
        x0,y0 = tie[3]-tie[0]*sx,tie[4]+tie[1]*sy
        nd = float(p.tags['GDAL_NODATA'].value.rstrip('\x00')) if 'GDAL_NODATA' in p.tags else None
        a = p.asarray().astype(float)
    return a,(x0,y0,sx,sy),nd


def reference(a,geo,nd,x,y):
    x0,y0,sx,sy = geo
    c,r = (x-x0)/sx-.5,(y0-y)/sy-.5
    ci,ri = np.floor(c).astype(int),np.floor(r).astype(int)
    valid = (ci>=0)&(ri>=0)&(ci+1<a.shape[1])&(ri+1<a.shape[0])
    ci=np.clip(ci,0,a.shape[1]-2); ri=np.clip(ri,0,a.shape[0]-2)
    vals=np.array([a[ri,ci],a[ri,ci+1],a[ri+1,ci],a[ri+1,ci+1]])
    valid &= np.isfinite(vals).all(axis=0)
    if nd is not None:
        valid &= (vals!=nd).all(axis=0)
    dc,dr = c-ci,r-ri
    out=vals[0]*(1-dc)*(1-dr)+vals[1]*dc*(1-dr)+vals[2]*(1-dc)*dr+vals[3]*dc*dr
    out[~valid]=np.nan
    return out


def stats(d):
    d=np.asarray(d); d=d[np.isfinite(d)]
    if not len(d): return {'n':0}
    return dict(n=len(d),bias=float(d.mean()),mae=float(abs(d).mean()),
                rmse=float(np.sqrt((d*d).mean())),p95=float(np.percentile(abs(d),95)),max=float(abs(d).max()))


def main(main_root=None, current_root=None, output=None, samples=None, preprocess_root=None):
    roots = {'main': main_root or ROOT/'main_r1_t4', 'current': current_root or ROOT/'current_r1_t4'}
    rows=[]
    for name,region in [('s85e80','S'),('s5e130','S'),('n0e0','N')]:
        if samples is not None and name not in samples:
            continue
        src,geo,nd=raster(Path(f'/source/gDEM_{region}/{name}.tif'))
        valid=np.isfinite(src) if nd is None else np.isfinite(src)&(src!=nd)
        row=dict(sample=name,source_shape=list(src.shape),geotransform=geo,nodata=nd,
                 source_min=float(src[valid].min()),source_max=float(src[valid].max()),
                 source_nodata_count=int((~valid).sum()))
        dirs={b:roots[b]/name/'tiles' for b in ['main','current']}
        paths={b:{p.relative_to(d).as_posix():p for p in d.rglob('*.terrain')} for b,d in dirs.items()}
        row['only_main']=len(paths['main'].keys()-paths['current'].keys())
        row['only_current']=len(paths['current'].keys()-paths['main'].keys())
        vals={b:[] for b in dirs}; refs=[]; delta=[]; near_delta=[]
        headers={b:[] for b in dirs}; vertices={b:0 for b in dirs}; errors=[]; zero_bad={b:0 for b in dirs}
        invalid_horizon={b:[] for b in dirs}; boundary_errors={b:[] for b in dirs}; worst={b:None for b in dirs}
        normals={b:0 for b in dirs}; seams={b:[] for b in dirs}; valid_seams={b:[] for b in dirs}; cache={b:{} for b in dirs}
        pp=np.array([(x,y) for y in np.linspace(.1,.9,5) for x in np.linspace(.1,.9,5)])
        bp=np.array([(x,y) for x in [.001,.01,.99,.999] for y in np.linspace(.1,.9,5)] +
                    [(x,y) for y in [.001,.01,.99,.999] for x in np.linspace(.1,.9,5)])
        edge_t=np.linspace(.05,.95,19)
        for rel in sorted(paths['main'].keys() & paths['current'].keys()):
            z,x,y=rel.replace('.terrain','').split('/'); z,x,y=int(z),int(x),int(y)
            pair={}
            for b in dirs:
                try:
                    v,t,h,ext=decode(paths[b][rel]); pair[b]=(v,t)
                    raw=gzip.decompress(paths[b][rel].read_bytes())
                    if not np.isfinite(struct.unpack_from('<3d',raw,64)).all(): invalid_horizon[b].append(rel)
                    headers[b].append(h); vertices[b]+=len(v); normals[b]+=1 in ext
                    if z==10: cache[b][(x,y)]=(v,t)
                except Exception as exc: errors.append([b,rel,repr(exc)])
            if z!=10 or len(pair)!=2: continue
            width=180/(2**z); lon=-180+(x+pp[:,0])*width; lat=-90+(y+pp[:,1])*width
            ref=reference(src,geo,nd,lon,lat)
            if not np.isfinite(ref).any(): continue
            ss={b:surface(*pair[b],pp) for b in dirs}
            ok=np.isfinite(ref)&np.isfinite(ss['main'])&np.isfinite(ss['current'])
            refs.extend(ref[ok]); delta.extend((ss['current']-ss['main'])[ok])
            x0,y0,sx,sy=geo
            dist=np.minimum.reduce([lon-x0,x0+src.shape[1]*sx-lon,y0-lat,lat-(y0-src.shape[0]*sy)])
            near_delta.extend((ss['current']-ss['main'])[ok&(dist<width)])
            for b in dirs:
                vals[b].extend((ss[b]-ref)[ok]); zero_bad[b]+=int(((abs(ss[b])<.01)&(abs(ref)>100)&ok).sum())
            blon=-180+(x+bp[:,0])*width; blat=-90+(y+bp[:,1])*width
            bref=reference(src,geo,nd,blon,blat)
            for b in dirs:
                bs=surface(*pair[b],bp); err=bs-bref
                boundary_errors[b].extend(err[np.isfinite(err)])
                if np.isfinite(err).any():
                    k=int(np.nanargmax(abs(err)))
                    if worst[b] is None or abs(err[k])>worst[b]['abs_error']:
                        worst[b]=dict(tile=rel,lon=float(blon[k]),lat=float(blat[k]),reference=float(bref[k]),mesh=float(bs[k]),abs_error=float(abs(err[k])))
        for b in dirs:
            for (x,y),(v,t) in cache[b].items():
                for key,p1,p2 in [((x+1,y),np.c_[np.ones(19),edge_t],np.c_[np.zeros(19),edge_t]),
                                  ((x,y+1),np.c_[edge_t,np.ones(19)],np.c_[edge_t,np.zeros(19)])]:
                    if key in cache[b]:
                        differences = surface(v,t,p1)-surface(*cache[b][key],p2)
                        seams[b].extend(differences)
                        lon = -180+(x+p1[:,0])*(180/1024)
                        lat = -90+(y+p1[:,1])*(180/1024)
                        mask = np.isfinite(reference(src,geo,nd,lon,lat))
                        valid_seams[b].extend(differences[mask])
        row.update(mesh_decode_failures=errors,invalid_horizon=invalid_horizon,decoded_tiles={b:len(headers[b]) for b in dirs},
                   normals_tiles=normals,vertices=vertices,reference_error_m={b:stats(vals[b]) for b in dirs},
                   branch_difference_m=stats(delta),near_source_edge_difference_m=stats(near_delta),
                   false_zero_samples=zero_bad,seam_difference_m={b:stats(seams[b]) for b in dirs},
                   valid_source_seam_difference_m={b:stats(valid_seams[b]) for b in dirs},
                   height_range={b:[min(h[0] for h in headers[b]),max(h[1] for h in headers[b])] for b in dirs},
                   tile_boundary_reference_error_m={b:stats(boundary_errors[b]) for b in dirs},worst_boundary=worst)
        # Direct pixel comparison separates preprocessing from meshing/resampling.
        row['preprocess_pixel_difference']={}
        for b in dirs:
            path = preprocess_root/name/'preprocessed.tif' if preprocess_root else roots[b]/name/'preprocess/preprocessed.tif'
            a,g,n=raster(path)
            if a.shape==src.shape and np.allclose(g,geo,rtol=0,atol=1e-9):
                mask=valid&np.isfinite(a)
                if n is not None: mask &= a!=n
                row['preprocess_pixel_difference'][b]=stats((a-src)[mask])
            else:
                row['preprocess_pixel_difference'][b]={'shape':list(a.shape),'geo':g,'same_grid':False}
        rows.append(row)
        (output or ROOT/'accuracy.json').write_text(json.dumps(rows,indent=2))
        print(json.dumps(row),flush=True)


if __name__=='__main__': main()
