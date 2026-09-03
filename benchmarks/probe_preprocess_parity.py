"""Stage-isolated preprocessing probes using a real DEM crop and controlled holes."""
import json
from pathlib import Path
import sys
import time
import numpy as np

ROOT=Path('/data/preprocess_probe_20260903')
ROOT.mkdir(exist_ok=True)
branch=sys.argv[1]

if branch=='main':
    from osgeo import gdal,osr
    gdal.UseExceptions()
    src=gdal.Open('/source/gDEM_S/s5e130.tif')
    a=src.ReadAsArray(1800,1800,515,513).astype(np.float32)
    geo=src.GetGeoTransform(); geo=(geo[0]+1800*geo[1],geo[1],0,geo[3]+1800*geo[5],0,geo[5])
    projection=src.GetProjection()
    def create(path,values,nodata):
        d=gdal.GetDriverByName('GTiff').Create(str(path),values.shape[1],values.shape[0],1,gdal.GDT_Float32,options=['TILED=YES'])
        d.SetGeoTransform(geo); d.SetProjection(projection)
        if nodata is not None: d.GetRasterBand(1).SetNoDataValue(nodata)
        d.GetRasterBand(1).WriteArray(values); d=None
    arrays={}
    hole=a.copy(); hole[250:262,250:262]=-32768; hole[50:80,50:80]=-32768
    arrays['holes']=hole
    nan=hole.copy(); nan[nan==-32768]=np.nan; arrays['nan_holes']=nan
    diagonal=np.full((31,31),-32768,dtype=np.float32)
    diagonal[10,10]=100;diagonal[20,20]=200; arrays['diagonal']=diagonal
    nan_diag=diagonal.copy();nan_diag[nan_diag==-32768]=np.nan;arrays['nan_diagonal']=nan_diag
    np.savez(ROOT/'inputs.npz',**arrays)
    results={}
    for name,values in arrays.items():
        path=ROOT/f'{name}.tif'; nd=np.nan if name.startswith('nan_') else -32768
        create(path,values,nd)
        d=gdal.Open(str(path),gdal.GA_Update)
        gdal.FillNodata(d.GetRasterBand(1),None,10,0)
        results[name]=d.ReadAsArray(); d=None
    create(ROOT/'overview_main.tif',a,None)
    d=gdal.Open(str(ROOT/'overview_main.tif'),gdal.GA_Update); d.BuildOverviews('AVERAGE',[2,4,8,16])
    results.update({f'ovr_{i}':d.GetRasterBand(1).GetOverview(i).ReadAsArray() for i in range(4)}); d=None
    create(ROOT/'warp_input.tif',a,None)
    import subprocess
    subprocess.run(['gdal','raster','reproject','--dst-crs','EPSG:3857','-r','bilinear','--overwrite',str(ROOT/'warp_input.tif'),str(ROOT/'warp_main.tif')],check=True,capture_output=True)
    d=gdal.Open(str(ROOT/'warp_main.tif'))
    meta={'warp_shape':[d.RasterYSize,d.RasterXSize],'warp_geo':d.GetGeoTransform()}
    results['warp']=d.ReadAsArray(); d=None
    np.savez(ROOT/'main.npz',**results)
    (ROOT/'main.json').write_text(json.dumps(meta,indent=2))
else:
    from evaluate_branches import current_api
    current_api()
    from app.services.raster.fillnodata import fill_nodata_array,fill_nodata_geotiff
    from app.services.raster.overviews import add_overviews
    from app.services.raster.reproject import reproject_geotiff
    from app.services.raster.geotiff import GeoTiffReader
    import shutil,tifffile
    inputs=np.load(ROOT/'inputs.npz'); results={}
    for name in inputs.files:
        nd=np.nan if name.startswith('nan_') else -32768
        results[name]=fill_nodata_array(inputs[name],nodata=nd,max_distance=10)
    shutil.copy2(ROOT/'warp_input.tif',ROOT/'overview_current.tif')
    add_overviews(ROOT/'overview_current.tif')
    with tifffile.TiffFile(ROOT/'overview_current.tif.ovr') as t:
        results.update({f'ovr_{i}':p.asarray() for i,p in enumerate(t.pages)})
    reproject_geotiff(ROOT/'warp_input.tif',ROOT/'warp_current.tif',dst_crs='EPSG:3857')
    with GeoTiffReader(ROOT/'warp_current.tif') as d:
        meta={'warp_shape':[d.height,d.width],'warp_affine':str(d.affine),'warp_bounds':d.bounds}
    results['warp']=tifffile.imread(ROOT/'warp_current.tif')
    from app.services.raster.warp import warp_window
    with GeoTiffReader(ROOT/'warp_input.tif') as src, GeoTiffReader(ROOT/'warp_main.tif') as dst:
        results['warp_fixed_grid']=warp_window(src,dst.affine,dst.crs,0,0,dst.height,dst.width,'bilinear')[:,:,0]
    np.savez(ROOT/'current.npz',**results)
    baseline=np.load(ROOT/'main.npz')
    comparisons={}
    for name,a in results.items():
        b=baseline['warp' if name=='warp_fixed_grid' else name]
        if a.shape!=b.shape:
            comparisons[name]={'main_shape':list(b.shape),'current_shape':list(a.shape)}; continue
        ma=(~np.isfinite(a))|(a==-32768); mb=(~np.isfinite(b))|(b==-32768)
        valid=~(ma|mb); delta=a[valid]-b[valid]
        comparisons[name]={'main_invalid':int(mb.sum()),'current_invalid':int(ma.sum()),'mask_disagreement':int((ma!=mb).sum()),
            'mae':float(abs(delta).mean()) if len(delta) else None,'max':float(abs(delta).max()) if len(delta) else None}
        if name in inputs.files:
            original=inputs[name]; holes=(~np.isfinite(original))|(original==-32768)
            hd=(a-b)[valid&holes]
            comparisons[name]['filled_pixels_compared']=len(hd)
            comparisons[name]['filled_pixels_mae']=float(abs(hd).mean()) if len(hd) else None
        if name=='diagonal': comparisons[name]['center']={'main':float(b[15,15]),'current':float(a[15,15])}
    meta['comparisons']=comparisons
    (ROOT/'current.json').write_text(json.dumps(meta,indent=2))
    print(json.dumps(meta,indent=2))
