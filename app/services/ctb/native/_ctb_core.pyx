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
    ) except + nogil
    string encode_heightmap_tile(
        const float* heights,
        int rows,
        int cols,
        int children,
    ) except + nogil

cdef extern from "resample.hpp" namespace "ctb_native":
    void aggregate_footprints(
        const float* src,
        int src_h,
        int src_w,
        const double* corner_rows,
        const double* corner_cols,
        int dst_h,
        int dst_w,
        int method_code,
        float fill,
        float* output,
    ) except + nogil
    void box_average(
        const float* src,
        int src_h,
        int src_w,
        int dst_h,
        int dst_w,
        float fill,
        float* output,
    ) except + nogil
    void remap_f32(
        const float* src,
        int src_h,
        int src_w,
        int bands,
        const double* map_x,
        const double* map_y,
        int dst_h,
        int dst_w,
        int method_code,
        float* output,
    ) except + nogil


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
    """Return uncompressed quantized-mesh bytes. Caller gzip-compresses with stdlib."""
    cdef object grid = np.ascontiguousarray(heights, dtype=np.float32)
    if grid.ndim != 2 or grid.shape[0] != grid.shape[1]:
        raise ValueError("heightfield must be a square 2D array")
    cdef int tile_size = <int> grid.shape[0]
    cdef int edge = tile_size - 1
    if tile_size < 3 or (edge & (edge - 1)) != 0:
        raise ValueError("heightfield size must be 2^n + 1")
    cdef float[:, ::1] view = grid
    cdef object holders = []
    cdef const float* neighbors[4]
    neighbors[0] = _optional_square(neighbor_left, tile_size, holders)
    neighbors[1] = _optional_square(neighbor_top, tile_size, holders)
    neighbors[2] = _optional_square(neighbor_right, tile_size, holders)
    neighbors[3] = _optional_square(neighbor_bottom, tile_size, holders)
    cdef string payload
    with nogil:
        payload = encode_mesh_tile(
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
    if grid.ndim != 2 or grid.shape[0] < 1 or grid.shape[1] < 1:
        raise ValueError("heightmap requires a non-empty 2D array")
    cdef int rows = <int> grid.shape[0]
    cdef int cols = <int> grid.shape[1]
    cdef float[:, ::1] view = grid
    cdef string payload
    with nogil:
        payload = encode_heightmap_tile(
            &view[0, 0],
            rows,
            cols,
            children,
        )
    return PyBytes_FromStringAndSize(payload.data(), payload.size())


def aggregate_footprints_f32(
    object src,
    object corner_rows,
    object corner_cols,
    int method_code,
    float fill,
):
    """Aggregate destination footprints in C++ and return float32 ``(H, W)``."""
    cdef object source = np.ascontiguousarray(src, dtype=np.float32)
    cdef object rows = np.ascontiguousarray(corner_rows, dtype=np.float64)
    cdef object cols = np.ascontiguousarray(corner_cols, dtype=np.float64)
    if source.ndim != 2 or source.shape[0] < 1 or source.shape[1] < 1:
        raise ValueError("source must be a non-empty 2D float array")
    if rows.ndim != 2 or cols.ndim != 2 or rows.shape != cols.shape:
        raise ValueError("corner arrays must be matching 2D arrays")
    if rows.shape[0] < 2 or rows.shape[1] < 2:
        raise ValueError("corner arrays must be at least 2x2")
    cdef int dst_h = <int> rows.shape[0] - 1
    cdef int dst_w = <int> rows.shape[1] - 1
    cdef int src_h = <int> source.shape[0]
    cdef int src_w = <int> source.shape[1]
    cdef object output = np.empty((dst_h, dst_w), dtype=np.float32)
    cdef float[:, ::1] source_view = source
    cdef double[:, ::1] rows_view = rows
    cdef double[:, ::1] cols_view = cols
    cdef float[:, ::1] output_view = output
    with nogil:
        aggregate_footprints(
            &source_view[0, 0],
            src_h,
            src_w,
            &rows_view[0, 0],
            &cols_view[0, 0],
            dst_h,
            dst_w,
            method_code,
            fill,
            &output_view[0, 0],
        )
    return output


def box_average_f32(object src, int dst_h, int dst_w, float fill):
    """Area-weighted box average of a 2D float32 band onto ``(dst_h, dst_w)``."""
    cdef object source = np.ascontiguousarray(src, dtype=np.float32)
    if source.ndim != 2 or source.shape[0] < 1 or source.shape[1] < 1:
        raise ValueError("source must be a non-empty 2D float array")
    if dst_h < 1 or dst_w < 1:
        raise ValueError("output size must be positive")
    cdef int src_h = <int> source.shape[0]
    cdef int src_w = <int> source.shape[1]
    cdef object output = np.empty((dst_h, dst_w), dtype=np.float32)
    cdef float[:, ::1] source_view = source
    cdef float[:, ::1] output_view = output
    with nogil:
        box_average(
            &source_view[0, 0],
            src_h,
            src_w,
            dst_h,
            dst_w,
            fill,
            &output_view[0, 0],
        )
    return output


def remap_f32_hwc(object src, object map_x, object map_y, int method_code):
    """Inverse-map a float32 HWC array in C++ without the GIL."""
    cdef object source = np.asarray(src, dtype=np.float32)
    if source.ndim == 2:
        source = source[:, :, np.newaxis]
    source = np.ascontiguousarray(source, dtype=np.float32)
    cdef object xs = np.ascontiguousarray(map_x, dtype=np.float64)
    cdef object ys = np.ascontiguousarray(map_y, dtype=np.float64)
    if (
        source.ndim != 3 or source.shape[0] < 1 or
        source.shape[1] < 1 or source.shape[2] < 1
    ):
        raise ValueError("source must be a non-empty 2D or HWC float array")
    if (
        xs.ndim != 2 or ys.ndim != 2 or xs.shape != ys.shape or
        xs.shape[0] < 1 or xs.shape[1] < 1
    ):
        raise ValueError("map arrays must be matching non-empty 2D arrays")
    cdef int src_h = <int> source.shape[0]
    cdef int src_w = <int> source.shape[1]
    cdef int bands = <int> source.shape[2]
    cdef int dst_h = <int> xs.shape[0]
    cdef int dst_w = <int> xs.shape[1]
    cdef object output = np.empty((dst_h, dst_w, bands), dtype=np.float32)
    cdef float[:, :, ::1] source_view = source
    cdef double[:, ::1] xs_view = xs
    cdef double[:, ::1] ys_view = ys
    cdef float[:, :, ::1] output_view = output
    with nogil:
        remap_f32(
            &source_view[0, 0, 0],
            src_h,
            src_w,
            bands,
            &xs_view[0, 0],
            &ys_view[0, 0],
            dst_h,
            dst_w,
            method_code,
            &output_view[0, 0, 0],
        )
    return output
