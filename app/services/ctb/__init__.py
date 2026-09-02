"""CTB-compatible terrain tiling (quantized-mesh and heightmap).

Algorithms, constants, and tile layout are ported from
``ahuarte47/cesium-terrain-builder`` (``master-quantized-mesh``),
Apache-2.0, GeoData / Alvaro Huarte.

Python schedules tiles and reads rasters; meshing and encoding run in the
Cython/C++ extension when it is built. No Docker ``ctb-tile`` sidecar.
"""
