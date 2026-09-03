# CTB native port provenance

The production native core is a dependency-free adaptation of
`ch-dpf/cesium-terrain-builder` branch `master-quantized-mesh-adaptation`,
pinned at:

```text
676719d22622c6ef754e2a348a14459df4ff2db6
```

| Upstream CTB source | Local implementation |
|---|---|
| `HeightFieldChunker.hpp` | `app/services/ctb/native/heightfield.hpp` |
| `MeshTiler.cpp` / `WrapperMesh` | `heightfield.hpp`, `mesh_tile.cpp` |
| `MeshTile.cpp`, `BoundingSphere.hpp` | `encode.hpp` |
| `TerrainTile.cpp` | `encode.hpp` |
| `GDALTiler.cpp` aggregate/kernel sampling semantics | `resample.cpp` |
| Overview box average | `resample.cpp` `box_average` |
| `tools/ctb-tile.cpp` grid/default rules | Python `grid.py`, `constants.py`, `tiler.py` |

The port intentionally preserves CTB compatibility details including:

- low-zoom activation lattice and high-zoom neighbor-border activation;
- CTB's `BYTESPLIT=65636` behavior;
- its bounding-sphere branch selection;
- zigzag/high-water-mark encoding and edge ordering;
- heightmap offset/scale and child flags;
- one-pixel west/north terrain sampling overlap.

The native module does not link GDAL or CTB. GeoTIFF decoding and CRS transforms
remain in the Python orchestration layer; numeric interpolation, area-weighted
aggregation, meshing, and binary encoding run in C++ without the GIL.

`tests/test_ctb_native.py` compares native payloads with the retained Python
reference implementation and exercises all native resampling methods.
