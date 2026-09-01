"""CTB ``ctb-tile`` orchestration: sample → mesh/heightmap → gzip ``{z}/{x}/{y}.terrain``."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
from pyproj import CRS

from app.schemas import CtbOptions, OutputFormat, Profile
from app.services.ctb.constants import (
    HEIGHTMAP_TERRAIN_QUALITY,
    SEMI_MAJOR_AXIS,
    SMOOTH_SMALL_ZOOM_MAX,
)
from app.services.ctb.encode import child_flags, encode_heightmap, encode_quantized_mesh
from app.services.ctb.grid import (
    CRSBounds,
    Grid,
    TileCoordinate,
    grid_for_profile,
    iter_tile_coordinates,
    neighbor_coord,
)
from app.services.ctb.heightfield import HeightField, MeshBuilder
from app.services.ctb.sample import (
    dataset_bounds_in_grid_crs,
    dataset_resolution,
    sample_tile_heights,
)
from app.services.layer_json import FORMAT_MAP
from app.services.raster.affine import Affine
from app.services.raster.errors import RasterError
from app.services.raster.geotiff import GeoTiffReader, write_geotiff_array
from app.services.raster.parallel import default_workers, run_unordered

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str | None], None]


class CtbError(RuntimeError):
    pass


def level_zero_geometric_error(grid: Grid, tile_size: int, mesh_qfactor: float) -> float:
    resolution_z0 = grid.resolution(0)
    tiles_at_z0 = int(grid.extent.width / (tile_size * resolution_z0))
    quality = HEIGHTMAP_TERRAIN_QUALITY * mesh_qfactor
    return (SEMI_MAJOR_AXIS * 2.0 * math.pi * quality) / float(tile_size * tiles_at_z0)


def geometric_error_for_zoom(grid: Grid, zoom: int, tile_size: int, mesh_qfactor: float) -> float:
    return level_zero_geometric_error(grid, tile_size, mesh_qfactor) / float(1 << zoom)


def _tile_path(output_dir: Path, coord: TileCoordinate) -> Path:
    return output_dir / str(coord.zoom) / str(coord.x) / f"{coord.y}.terrain"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class _LevelInfo:
    __slots__ = ("start_x", "start_y", "final_x", "final_y")

    def __init__(self) -> None:
        self.start_x = 2**31 - 1
        self.start_y = 2**31 - 1
        self.final_x = -(2**31)
        self.final_y = -(2**31)

    def add_coord(self, coord: TileCoordinate) -> None:
        self.start_x = min(self.start_x, coord.x)
        self.start_y = min(self.start_y, coord.y)
        self.final_x = max(self.final_x, coord.x)
        self.final_y = max(self.final_y, coord.y)

    def valid(self) -> bool:
        return self.final_x >= self.start_x


class _Metadata:
    def __init__(self) -> None:
        self.levels: list[_LevelInfo] = []
        self.bounds: CRSBounds | None = None
        self._lock = threading.Lock()

    def add(self, grid: Grid, coord: TileCoordinate) -> None:
        tile_bounds = grid.tile_bounds(coord)
        with self._lock:
            while len(self.levels) <= coord.zoom:
                self.levels.append(_LevelInfo())
            self.levels[coord.zoom].add_coord(coord)
            if self.bounds is None:
                self.bounds = tile_bounds
            else:
                self.bounds = CRSBounds(
                    min(self.bounds.minx, tile_bounds.minx),
                    min(self.bounds.miny, tile_bounds.miny),
                    max(self.bounds.maxx, tile_bounds.maxx),
                    max(self.bounds.maxy, tile_bounds.maxy),
                )


def _write_layer_json(
    output_dir: Path,
    metadata: _Metadata,
    *,
    name: str,
    output_format: OutputFormat,
    profile: Profile,
    vertex_normals: bool,
    cesium_friendly: bool,
    end_zoom: int,
) -> None:
    if cesium_friendly and profile == Profile.GEODETIC and end_zoom <= 0 and metadata.levels:
        level0 = metadata.levels[0]
        level0.start_x = 0
        level0.start_y = 0
        level0.final_x = 1
        level0.final_y = 0
    available: list[list[dict[str, int]]] = []
    for level in metadata.levels:
        if level.valid():
            available.append(
                [
                    {
                        "startX": level.start_x,
                        "startY": level.start_y,
                        "endX": level.final_x,
                        "endY": level.final_y,
                    }
                ]
            )
        else:
            available.append([])
    bounds = metadata.bounds
    payload = {
        "tilejson": "2.1.0",
        "name": name,
        "description": "",
        "version": "1.1.0",
        "format": FORMAT_MAP[output_format],
        "attribution": "",
        "scheme": "tms",
        "tiles": ["{z}/{x}/{y}.terrain?v={version}"],
        "projection": "EPSG:4326" if profile == Profile.GEODETIC else "EPSG:3857",
        "bounds": (
            [bounds.minx, bounds.miny, bounds.maxx, bounds.maxy] if bounds is not None else [-180.0, -90.0, 180.0, 90.0]
        ),
        "available": available,
    }
    if vertex_normals and output_format == OutputFormat.MESH:
        payload["extensions"] = ["octvertexnormals"]
    path = output_dir / "layer.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _build_mesh_tile(
    heights: np.ndarray,
    grid: Grid,
    coord: TileCoordinate,
    dataset_bounds: CRSBounds,
    max_zoom: int,
    mesh_qfactor: float,
    neighbor_heights: dict[int, np.ndarray],
) -> tuple[list[tuple[float, float, float]], list[int], int]:
    tile_size = grid.tile_size
    error = geometric_error_for_zoom(grid, coord.zoom, tile_size, mesh_qfactor)
    field = HeightField(heights)
    field.apply_geometric_error(error, coord.zoom <= SMOOTH_SMALL_ZOOM_MAX)
    if coord.zoom > SMOOTH_SMALL_ZOOM_MAX:
        for border in range(4):
            neighbor = neighbor_heights.get(border)
            if neighbor is None:
                continue
            other = HeightField(neighbor)
            other.apply_geometric_error(error, False)
            field.apply_border_activation_state(other, border)
    bounds = grid.tile_bounds(coord)
    mesh = MeshBuilder(bounds.minx, bounds.miny, bounds.maxx, bounds.maxy, tile_size)
    field.generate_mesh(mesh, 0)
    flags = child_flags(dataset_bounds, bounds, is_max_zoom=coord.zoom == max_zoom)
    return mesh.vertices, mesh.indices, flags


def _neighbor_heights(
    src: GeoTiffReader,
    grid: Grid,
    coord: TileCoordinate,
    dataset_bounds: CRSBounds,
    resampling: str,
) -> dict[int, np.ndarray]:
    found: dict[int, np.ndarray] = {}
    for border in range(4):
        neighbor = neighbor_coord(grid, coord, border)
        if neighbor is None:
            continue
        if not dataset_bounds.overlaps(grid.tile_bounds(neighbor)):
            continue
        found[border] = sample_tile_heights(src, grid, neighbor, resampling)
    return found


def _encode_tile(
    src: GeoTiffReader,
    grid: Grid,
    coord: TileCoordinate,
    options: CtbOptions,
    dataset_bounds: CRSBounds,
    max_zoom: int,
) -> bytes:
    resampling = options.resampling_method.value
    heights = sample_tile_heights(src, grid, coord, resampling)
    if options.output_format == OutputFormat.TERRAIN:
        flags = child_flags(dataset_bounds, grid.tile_bounds(coord), is_max_zoom=coord.zoom == max_zoom)
        return encode_heightmap(heights, flags)
    neighbors: dict[int, np.ndarray] = {}
    if coord.zoom > SMOOTH_SMALL_ZOOM_MAX:
        neighbors = _neighbor_heights(src, grid, coord, dataset_bounds, resampling)
    vertices, indices, _flags = _build_mesh_tile(
        heights,
        grid,
        coord,
        dataset_bounds,
        max_zoom,
        options.mesh_qfactor,
        neighbors,
    )
    if not vertices:
        raise CtbError(f"Mesh generation produced no vertices for {coord.zoom}/{coord.x}/{coord.y}")
    return encode_quantized_mesh(
        vertices,
        indices,
        write_vertex_normals=options.vertex_normals and options.output_format == OutputFormat.MESH,
    )


def _create_empty_root_tile(
    output_dir: Path,
    grid: Grid,
    coord: TileCoordinate,
    options: CtbOptions,
    max_zoom: int,
) -> None:
    """ctb-tile.cpp createEmptyRootElevationFile + runTiler for the missing z0 tile."""
    tile_bounds = grid.tile_bounds(coord)
    inset = CRSBounds(
        tile_bounds.minx + 1.0,
        tile_bounds.miny + 1.0,
        tile_bounds.maxx - 1.0,
        tile_bounds.maxy - 1.0,
    )
    size = grid.tile_size - 2
    resolution = inset.width / float(size)
    affine = Affine.north_up(inset.minx, inset.maxy, resolution, resolution)
    zeros = np.zeros((size, size), dtype=np.float32)
    fd, tmp_name = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        write_geotiff_array(
            tmp_path,
            zeros,
            affine=affine,
            crs=CRS.from_epsg(4326),
            compress="NONE",
            block_size=16,
            nodata=None,
        )
        with GeoTiffReader(tmp_path, preload=True) as empty:
            empty_bounds = dataset_bounds_in_grid_crs(empty, grid)
            payload = _encode_tile(empty, grid, coord, options, empty_bounds, max_zoom)
            _atomic_write(_tile_path(output_dir, coord), payload)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _apply_cesium_friendly(
    output_dir: Path,
    grid: Grid,
    options: CtbOptions,
    max_zoom: int,
    *,
    write_tiles: bool,
) -> None:
    if options.profile != Profile.GEODETIC:
        return
    end_zoom = 0 if options.end_zoom is None else int(options.end_zoom)
    if end_zoom > 0:
        return
    tile0 = _tile_path(output_dir, TileCoordinate(0, 0, 0))
    tile1 = _tile_path(output_dir, TileCoordinate(0, 1, 0))
    if write_tiles:
        if tile0.is_file() and not tile1.is_file():
            _create_empty_root_tile(output_dir, grid, TileCoordinate(0, 1, 0), options, max_zoom)
        elif tile1.is_file() and not tile0.is_file():
            _create_empty_root_tile(output_dir, grid, TileCoordinate(0, 0, 0), options, max_zoom)


def run_ctb_tile(
    input_path: Path,
    output_dir: Path,
    options: CtbOptions,
    *,
    cache_bytes: int | None = None,
    gdal_cachemax: int | None = None,
    on_subprogress: ProgressCallback | None = None,
    **_ignored: object,
) -> None:
    """Python CTB-compatible tiler. Extra kwargs are ignored (legacy Docker args)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    grid = grid_for_profile(options.profile, options.tile_size)
    mb = gdal_cachemax if gdal_cachemax is not None else 512
    cache = cache_bytes if cache_bytes is not None else max(int(mb), 1) * 1024 * 1024
    try:
        src_cm = GeoTiffReader(input_path, cache_bytes=cache, preload=False)
    except RasterError as exc:
        raise CtbError(str(exc)) from exc

    with src_cm as src:
        dataset_bounds = dataset_bounds_in_grid_crs(src, grid)
        resolution = dataset_resolution(src, grid, dataset_bounds)
        auto_max = grid.zoom_for_resolution(resolution)
        start_zoom = auto_max if options.start_zoom is None else int(options.start_zoom)
        end_zoom = 0 if options.end_zoom is None else int(options.end_zoom)
        if start_zoom < end_zoom:
            raise CtbError("start_zoom must be >= end_zoom")
        coords = iter_tile_coordinates(grid, dataset_bounds, start_zoom, end_zoom)
        total = max(len(coords), 1)
        metadata = _Metadata()
        workers = options.thread_count if options.thread_count is not None else default_workers()
        done = 0
        lock = threading.Lock()

        def _mark(coord: TileCoordinate) -> None:
            nonlocal done
            with lock:
                done += 1
                current = done
            if on_subprogress is not None:
                on_subprogress(100.0 * current / total, f"Zoom {coord.zoom}")

        def _process(coord: TileCoordinate) -> None:
            try:
                metadata.add(grid, coord)
                if not options.layer_only:
                    path = _tile_path(output_dir, coord)
                    if not (options.resume and path.is_file()):
                        payload = _encode_tile(src, grid, coord, options, dataset_bounds, start_zoom)
                        _atomic_write(path, payload)
            finally:
                _mark(coord)

        if options.verbose:
            logger.debug("Tiling %s tiles zoom %s→%s", len(coords), start_zoom, end_zoom)
        try:
            run_unordered(coords, _process, workers=workers)
        except Exception as exc:
            if isinstance(exc, CtbError):
                raise
            raise CtbError(str(exc)) from exc

        _apply_cesium_friendly(
            output_dir,
            grid,
            options,
            start_zoom,
            write_tiles=options.cesium_friendly and not options.layer_only,
        )
        _write_layer_json(
            output_dir,
            metadata,
            name=input_path.stem,
            output_format=options.output_format,
            profile=options.profile,
            vertex_normals=options.vertex_normals,
            cesium_friendly=options.cesium_friendly,
            end_zoom=end_zoom,
        )

    if on_subprogress is not None:
        on_subprogress(100.0, "Tiling complete")
