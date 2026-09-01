"""Self-contained Python raster engine replacing GDAL CLI usage."""

from app.services.raster.errors import RasterError
from app.services.raster.fillnodata import fill_nodata_array, fill_nodata_geotiff
from app.services.raster.info import raster_info_json, raster_info_text
from app.services.raster.overviews import add_overviews
from app.services.raster.reproject import reproject_geotiff

__all__ = [
    "RasterError",
    "add_overviews",
    "fill_nodata_array",
    "fill_nodata_geotiff",
    "raster_info_json",
    "raster_info_text",
    "reproject_geotiff",
]
