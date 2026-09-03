"""CTB-compatible terrain tiling (quantized-mesh and heightmap).

Algorithms, constants, and tile layout are adapted from
``ch-dpf/cesium-terrain-builder@676719d`` (Apache-2.0).

Python schedules tiles and reads raster windows; resampling, meshing, and
encoding require the bundled Cython/C++ extension. No Docker sidecar.
"""
