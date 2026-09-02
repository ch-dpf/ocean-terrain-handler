# cython: language_level=3
# distutils: language = c++

from libcpp cimport bool as cpp_bool
from libcpp.string cimport string
from cpython.bytes cimport PyBytes_FromStringAndSize

import numpy as np

cdef extern from "mesh_tile.hpp" namespace "ctb_native":
    string encode_mesh_tile(
        const float* heights,
        int tile_size,
        double minx,
        double miny,
        double maxx,
        double maxy,
        double geometric_error,
        cpp_bool smooth_small_zooms,
        const float* const* neighbor_heights,
        cpp_bool write_vertex_normals,
    ) except +
    string encode_heightmap_tile(
        const float* heights,
        int rows,
        int cols,
        int children,
    ) except +


cdef const float* _optional_square(object array, int tile_size, object holders):
    if array is None:
        return NULL
    cdef object contiguous = np.ascontiguousarray(array, dtype=np.float32)
    if contiguous.ndim != 2 or contiguous.shape[0] != tile_size or contiguous.shape[1] != tile_size:
        raise ValueError("neighbor height grid must be square tile_size x tile_size")
    holders.append(contiguous)
    cdef float[:, ::1] view = contiguous
    return &view[0, 0]


def encode_mesh_tile_bytes(
    object heights,
    double minx,
    double miny,
    double maxx,
    double maxy,
    double geometric_error,
    bint smooth_small_zooms,
    object neighbor_left,
    object neighbor_top,
    object neighbor_right,
    object neighbor_bottom,
    bint write_vertex_normals,
):
    cdef object grid = np.ascontiguousarray(heights, dtype=np.float32)
    if grid.ndim != 2 or grid.shape[0] != grid.shape[1]:
        raise ValueError("heightfield must be a square 2D array")
    cdef int tile_size = <int> grid.shape[0]
    cdef float[:, ::1] view = grid
    cdef object holders = []
    cdef const float* neighbors[4]
    neighbors[0] = _optional_square(neighbor_left, tile_size, holders)
    neighbors[1] = _optional_square(neighbor_top, tile_size, holders)
    neighbors[2] = _optional_square(neighbor_right, tile_size, holders)
    neighbors[3] = _optional_square(neighbor_bottom, tile_size, holders)
    cdef string payload = encode_mesh_tile(
        &view[0, 0],
        tile_size,
        minx,
        miny,
        maxx,
        maxy,
        geometric_error,
        smooth_small_zooms,
        neighbors,
        write_vertex_normals,
    )
    return PyBytes_FromStringAndSize(payload.data(), payload.size())


def encode_heightmap_tile_bytes(object heights, int children):
    cdef object grid = np.ascontiguousarray(heights, dtype=np.float32)
    if grid.ndim != 2:
        raise ValueError("heightmap requires a 2D array")
    cdef float[:, ::1] view = grid
    cdef string payload = encode_heightmap_tile(
        &view[0, 0],
        <int> grid.shape[0],
        <int> grid.shape[1],
        children,
    )
    return PyBytes_FromStringAndSize(payload.data(), payload.size())
