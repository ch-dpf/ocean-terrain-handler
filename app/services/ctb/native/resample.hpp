#pragma once

namespace ctb_native {

enum class AggregateMethod : int {
    Average = 0,
    Mode = 1,
    Maximum = 2,
    Minimum = 3,
    Median = 4,
    FirstQuartile = 5,
    ThirdQuartile = 6,
};

enum class KernelMethod : int {
    Nearest = 0,
    Bilinear = 1,
    Cubic = 2,
    CubicSpline = 3,
    Lanczos = 4,
};

// Aggregate source pixels intersecting each destination-pixel quadrilateral.
// Arrays are contiguous row-major. Corner arrays are (dst_h + 1, dst_w + 1).
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
    float* output
);

// Area-weighted box average from a source band onto a regular destination grid.
void box_average(
    const float* src,
    int src_h,
    int src_w,
    int dst_h,
    int dst_w,
    float fill,
    float* output
);

// Inverse-map a float32 HWC source. Maps are PixelIsArea coordinates.
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
    float* output
);

}  // namespace ctb_native
