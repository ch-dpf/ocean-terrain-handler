#include "resample.hpp"

// CTB's GDALTiler uses GDAL warp kernels. This dependency-free implementation
// preserves the relevant PixelIsArea and area-weighted sampling semantics.

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <stdexcept>
#include <unordered_map>
#include <utility>
#include <vector>

namespace ctb_native {
namespace {

struct Point {
    double x;
    double y;
};

enum class Boundary {
    Left,
    Right,
    Top,
    Bottom,
};

constexpr int kMaxClipVertices = 16;
constexpr double kAreaEpsilon = 1e-14;
constexpr double kAxisEpsilon = 1e-9;

struct Polygon {
    Point points[kMaxClipVertices];
    int count = 0;
};

bool inside(const Point& point, Boundary boundary, double value) {
    switch (boundary) {
        case Boundary::Left:
            return point.x >= value;
        case Boundary::Right:
            return point.x <= value;
        case Boundary::Top:
            return point.y >= value;
        case Boundary::Bottom:
            return point.y <= value;
    }
    return false;
}

Point intersection(const Point& start, const Point& end, Boundary boundary, double value) {
    if (boundary == Boundary::Left || boundary == Boundary::Right) {
        const double delta = end.x - start.x;
        if (delta == 0.0) {
            return Point{value, start.y};
        }
        const double ratio = (value - start.x) / delta;
        return Point{value, start.y + ratio * (end.y - start.y)};
    }
    const double delta = end.y - start.y;
    if (delta == 0.0) {
        return Point{start.x, value};
    }
    const double ratio = (value - start.y) / delta;
    return Point{start.x + ratio * (end.x - start.x), value};
}

void clip_boundary(
    const Polygon& input,
    Boundary boundary,
    double value,
    Polygon& output
) {
    output.count = 0;
    if (input.count == 0) {
        return;
    }
    Point start = input.points[input.count - 1];
    bool start_inside = inside(start, boundary, value);
    for (int index = 0; index < input.count; ++index) {
        const Point end = input.points[index];
        const bool end_inside = inside(end, boundary, value);
        if (end_inside) {
            if (!start_inside && output.count < kMaxClipVertices) {
                output.points[output.count++] = intersection(start, end, boundary, value);
            }
            if (output.count < kMaxClipVertices) {
                output.points[output.count++] = end;
            }
        } else if (start_inside && output.count < kMaxClipVertices) {
            output.points[output.count++] = intersection(start, end, boundary, value);
        }
        start = end;
        start_inside = end_inside;
    }
}

double polygon_area(const Polygon& polygon) {
    if (polygon.count < 3) {
        return 0.0;
    }
    double twice_area = 0.0;
    Point previous = polygon.points[polygon.count - 1];
    for (int index = 0; index < polygon.count; ++index) {
        const Point point = polygon.points[index];
        twice_area += previous.x * point.y - point.x * previous.y;
        previous = point;
    }
    return std::abs(twice_area) * 0.5;
}

double overlap_area(const Polygon& footprint, int source_row, int source_col) {
    Polygon stage_a;
    Polygon stage_b;
    clip_boundary(footprint, Boundary::Left, static_cast<double>(source_col), stage_a);
    clip_boundary(stage_a, Boundary::Right, static_cast<double>(source_col + 1), stage_b);
    clip_boundary(stage_b, Boundary::Top, static_cast<double>(source_row), stage_a);
    clip_boundary(stage_a, Boundary::Bottom, static_cast<double>(source_row + 1), stage_b);
    return polygon_area(stage_b);
}

bool nearly_equal(double left, double right) {
    return std::abs(left - right) <= kAxisEpsilon;
}

bool axis_aligned_rectangle(const double* rows, const double* cols) {
    return nearly_equal(rows[0], rows[1]) && nearly_equal(rows[3], rows[2]) &&
        nearly_equal(cols[0], cols[3]) && nearly_equal(cols[1], cols[2]);
}

double rectangle_overlap(
    double min_row,
    double max_row,
    double min_col,
    double max_col,
    int source_row,
    int source_col
) {
    const double overlap_row =
        std::min(max_row, static_cast<double>(source_row + 1)) -
        std::max(min_row, static_cast<double>(source_row));
    const double overlap_col =
        std::min(max_col, static_cast<double>(source_col + 1)) -
        std::max(min_col, static_cast<double>(source_col));
    if (overlap_row <= 0.0 || overlap_col <= 0.0) {
        return 0.0;
    }
    return overlap_row * overlap_col;
}

float percentile(std::vector<float>& values, double quantile) {
    std::sort(values.begin(), values.end());
    const auto last = values.size() - 1;
    const auto index = std::min(
        last,
        static_cast<std::size_t>(
            std::ceil(quantile * static_cast<double>(values.size()) - 1.0)
        )
    );
    return values[index];
}

float reduce(std::vector<float>& values, AggregateMethod method);

void accumulate_axis_aligned(
    const float* src,
    int src_h,
    int src_w,
    double min_row,
    double max_row,
    double min_col,
    double max_col,
    AggregateMethod method,
    float fill,
    float* destination
) {
    if (max_row < min_row) {
        std::swap(min_row, max_row);
    }
    if (max_col < min_col) {
        std::swap(min_col, max_col);
    }
    const int row_begin = std::max(0, static_cast<int>(std::floor(min_row)));
    const int row_end = std::min(src_h, static_cast<int>(std::ceil(max_row)));
    const int col_begin = std::max(0, static_cast<int>(std::floor(min_col)));
    const int col_end = std::min(src_w, static_cast<int>(std::ceil(max_col)));
    if (row_begin >= row_end || col_begin >= col_end) {
        *destination = fill;
        return;
    }

    double weighted_sum = 0.0;
    double weight_sum = 0.0;
    std::vector<float> values;
    if (method != AggregateMethod::Average) {
        values.reserve(
            static_cast<std::size_t>(row_end - row_begin) *
            static_cast<std::size_t>(col_end - col_begin)
        );
    }
    for (int source_row = row_begin; source_row < row_end; ++source_row) {
        const float* row_ptr = src + source_row * src_w;
        for (int source_col = col_begin; source_col < col_end; ++source_col) {
            const double area = rectangle_overlap(
                min_row,
                max_row,
                min_col,
                max_col,
                source_row,
                source_col
            );
            if (area <= kAreaEpsilon) {
                continue;
            }
            const float value = row_ptr[source_col];
            if (!std::isfinite(value)) {
                continue;
            }
            if (method == AggregateMethod::Average) {
                weighted_sum += static_cast<double>(value) * area;
                weight_sum += area;
            } else {
                values.push_back(value);
            }
        }
    }
    if (method == AggregateMethod::Average) {
        *destination = weight_sum > 0.0 ? static_cast<float>(weighted_sum / weight_sum) : fill;
    } else if (!values.empty()) {
        *destination = reduce(values, method);
    } else {
        *destination = fill;
    }
}

float mode(const std::vector<float>& values) {
    std::unordered_map<float, std::size_t> counts;
    for (const float value : values) {
        ++counts[value];
    }
    float best_value = values.front();
    std::size_t best_count = 0;
    // A strict comparison preserves GDAL's default "first encountered" tie.
    for (const float value : values) {
        const std::size_t count = counts[value];
        if (count > best_count) {
            best_count = count;
            best_value = value;
        }
    }
    return best_value;
}

float reduce(std::vector<float>& values, AggregateMethod method) {
    switch (method) {
        case AggregateMethod::Mode:
            return mode(values);
        case AggregateMethod::Maximum:
            return *std::max_element(values.begin(), values.end());
        case AggregateMethod::Minimum:
            return *std::min_element(values.begin(), values.end());
        case AggregateMethod::Median:
            return percentile(values, 0.5);
        case AggregateMethod::FirstQuartile:
            return percentile(values, 0.25);
        case AggregateMethod::ThirdQuartile:
            return percentile(values, 0.75);
        case AggregateMethod::Average:
            break;
    }
    throw std::runtime_error("invalid aggregate method");
}

}  // namespace

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
) {
    if (
        src == nullptr || corner_rows == nullptr || corner_cols == nullptr ||
        output == nullptr || src_h <= 0 || src_w <= 0 || dst_h <= 0 || dst_w <= 0
    ) {
        throw std::runtime_error("invalid aggregate footprint arrays");
    }
    if (
        method_code < static_cast<int>(AggregateMethod::Average) ||
        method_code > static_cast<int>(AggregateMethod::ThirdQuartile)
    ) {
        throw std::runtime_error("invalid aggregate method code");
    }
    const auto method = static_cast<AggregateMethod>(method_code);
    const int corner_stride = dst_w + 1;

    for (int row = 0; row < dst_h; ++row) {
        for (int col = 0; col < dst_w; ++col) {
            const int top_left = row * corner_stride + col;
            const int top_right = top_left + 1;
            const int bottom_left = (row + 1) * corner_stride + col;
            const int bottom_right = bottom_left + 1;
            const double rows[4] = {
                corner_rows[top_left],
                corner_rows[top_right],
                corner_rows[bottom_right],
                corner_rows[bottom_left],
            };
            const double cols[4] = {
                corner_cols[top_left],
                corner_cols[top_right],
                corner_cols[bottom_right],
                corner_cols[bottom_left],
            };
            bool corners_finite = true;
            for (int index = 0; index < 4; ++index) {
                corners_finite =
                    corners_finite && std::isfinite(rows[index]) && std::isfinite(cols[index]);
            }
            const int output_index = row * dst_w + col;
            output[output_index] = fill;
            if (!corners_finite) {
                continue;
            }

            const double min_row = *std::min_element(rows, rows + 4);
            const double max_row = *std::max_element(rows, rows + 4);
            const double min_col = *std::min_element(cols, cols + 4);
            const double max_col = *std::max_element(cols, cols + 4);
            const int row_begin = std::max(0, static_cast<int>(std::floor(min_row)));
            const int row_end = std::min(src_h, static_cast<int>(std::ceil(max_row)));
            const int col_begin = std::max(0, static_cast<int>(std::floor(min_col)));
            const int col_end = std::min(src_w, static_cast<int>(std::ceil(max_col)));
            if (row_begin >= row_end || col_begin >= col_end) {
                continue;
            }

            if (axis_aligned_rectangle(rows, cols)) {
                accumulate_axis_aligned(
                    src,
                    src_h,
                    src_w,
                    min_row,
                    max_row,
                    min_col,
                    max_col,
                    method,
                    fill,
                    output + output_index
                );
                continue;
            }

            Polygon footprint;
            footprint.count = 4;
            footprint.points[0] = Point{cols[0], rows[0]};
            footprint.points[1] = Point{cols[1], rows[1]};
            footprint.points[2] = Point{cols[2], rows[2]};
            footprint.points[3] = Point{cols[3], rows[3]};
            double weighted_sum = 0.0;
            double weight_sum = 0.0;
            std::vector<float> values;
            if (method != AggregateMethod::Average) {
                values.reserve(
                    static_cast<std::size_t>(row_end - row_begin) *
                    static_cast<std::size_t>(col_end - col_begin)
                );
            }
            for (int source_row = row_begin; source_row < row_end; ++source_row) {
                const float* row_ptr = src + source_row * src_w;
                for (int source_col = col_begin; source_col < col_end; ++source_col) {
                    const double area = overlap_area(footprint, source_row, source_col);
                    if (area <= kAreaEpsilon) {
                        continue;
                    }
                    const float value = row_ptr[source_col];
                    if (!std::isfinite(value)) {
                        continue;
                    }
                    if (method == AggregateMethod::Average) {
                        weighted_sum += static_cast<double>(value) * area;
                        weight_sum += area;
                    } else {
                        values.push_back(value);
                    }
                }
            }
            if (method == AggregateMethod::Average) {
                if (weight_sum > 0.0) {
                    output[output_index] = static_cast<float>(weighted_sum / weight_sum);
                }
            } else if (!values.empty()) {
                output[output_index] = reduce(values, method);
            }
        }
    }
}

void box_average(
    const float* src,
    int src_h,
    int src_w,
    int dst_h,
    int dst_w,
    float fill,
    float* output
) {
    if (
        src == nullptr || output == nullptr ||
        src_h <= 0 || src_w <= 0 || dst_h <= 0 || dst_w <= 0
    ) {
        throw std::runtime_error("invalid box-average arrays");
    }
    const double src_h_d = static_cast<double>(src_h);
    const double src_w_d = static_cast<double>(src_w);
    for (int row = 0; row < dst_h; ++row) {
        const double min_row = src_h_d * static_cast<double>(row) / static_cast<double>(dst_h);
        const double max_row = src_h_d * static_cast<double>(row + 1) / static_cast<double>(dst_h);
        for (int col = 0; col < dst_w; ++col) {
            const double min_col = src_w_d * static_cast<double>(col) / static_cast<double>(dst_w);
            const double max_col = src_w_d * static_cast<double>(col + 1) / static_cast<double>(dst_w);
            accumulate_axis_aligned(
                src,
                src_h,
                src_w,
                min_row,
                max_row,
                min_col,
                max_col,
                AggregateMethod::Average,
                fill,
                output + row * dst_w + col
            );
        }
    }
}

namespace {

double cubic_weight(double distance) {
    const double value = std::abs(distance);
    const double squared = value * value;
    const double cubed = squared * value;
    constexpr double a = -0.5;
    if (value <= 1.0) {
        return ((a + 2.0) * cubed) - ((a + 3.0) * squared) + 1.0;
    }
    if (value < 2.0) {
        return (a * cubed) - (5.0 * a * squared) + (8.0 * a * value) - (4.0 * a);
    }
    return 0.0;
}

double cubic_spline_weight(double distance) {
    const double value = std::abs(distance);
    if (value < 1.0) {
        return (0.5 * value * value * value) - (value * value) + (2.0 / 3.0);
    }
    if (value < 2.0) {
        const double delta = 2.0 - value;
        return (delta * delta * delta) / 6.0;
    }
    return 0.0;
}

double sinc(double value) {
    if (value == 0.0) {
        return 1.0;
    }
    return std::sin(value) / value;
}

double lanczos_weight(double distance) {
    const double value = std::abs(distance);
    if (value >= 3.0) {
        return 0.0;
    }
    constexpr double pi = 3.14159265358979323846;
    return sinc(pi * value) * sinc(pi * value / 3.0);
}

float source_value(
    const float* src,
    int src_h,
    int src_w,
    int bands,
    int row,
    int col,
    int band
) {
    if (row < 0 || row >= src_h || col < 0 || col >= src_w) {
        return 0.0F;
    }
    return src[(row * src_w + col) * bands + band];
}

}  // namespace

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
) {
    if (
        src == nullptr || map_x == nullptr || map_y == nullptr || output == nullptr ||
        src_h <= 0 || src_w <= 0 || bands <= 0 || dst_h <= 0 || dst_w <= 0
    ) {
        throw std::runtime_error("invalid remap arrays");
    }
    if (
        method_code < static_cast<int>(KernelMethod::Nearest) ||
        method_code > static_cast<int>(KernelMethod::Lanczos)
    ) {
        throw std::runtime_error("invalid kernel method code");
    }
    const auto method = static_cast<KernelMethod>(method_code);
    std::vector<double> sums(static_cast<std::size_t>(bands), 0.0);
    std::vector<double> weight_sums(static_cast<std::size_t>(bands), 0.0);

    for (int row = 0; row < dst_h; ++row) {
        for (int col = 0; col < dst_w; ++col) {
            const int map_index = row * dst_w + col;
            // Convert PixelIsArea coordinates to center-index coordinates.
            const double x = map_x[map_index] - 0.5;
            const double y = map_y[map_index] - 0.5;
            float* destination = output + map_index * bands;
            if (!std::isfinite(x) || !std::isfinite(y)) {
                std::fill(destination, destination + bands, 0.0F);
                continue;
            }

            if (method == KernelMethod::Nearest) {
                const int source_col = static_cast<int>(std::floor(x + 0.5));
                const int source_row = static_cast<int>(std::floor(y + 0.5));
                for (int band = 0; band < bands; ++band) {
                    destination[band] = source_value(
                        src,
                        src_h,
                        src_w,
                        bands,
                        source_row,
                        source_col,
                        band
                    );
                }
                continue;
            }

            const int base_col = static_cast<int>(std::floor(x));
            const int base_row = static_cast<int>(std::floor(y));
            int tap_begin = 0;
            int tap_end = 1;
            if (method == KernelMethod::Cubic || method == KernelMethod::CubicSpline) {
                tap_begin = -1;
                tap_end = 2;
            } else if (method == KernelMethod::Lanczos) {
                tap_begin = -2;
                tap_end = 3;
            }

            std::fill(sums.begin(), sums.end(), 0.0);
            std::fill(weight_sums.begin(), weight_sums.end(), 0.0);
            for (int row_tap = tap_begin; row_tap <= tap_end; ++row_tap) {
                const int source_row = base_row + row_tap;
                double row_weight = 0.0;
                if (method == KernelMethod::Bilinear) {
                    row_weight = row_tap == 0 ? 1.0 - (y - base_row) : y - base_row;
                } else if (method == KernelMethod::Cubic) {
                    row_weight = cubic_weight(y - source_row);
                } else if (method == KernelMethod::CubicSpline) {
                    row_weight = cubic_spline_weight(y - source_row);
                } else {
                    row_weight = lanczos_weight(y - source_row);
                }
                for (int col_tap = tap_begin; col_tap <= tap_end; ++col_tap) {
                    const int source_col = base_col + col_tap;
                    if (
                        source_row < 0 || source_row >= src_h ||
                        source_col < 0 || source_col >= src_w
                    ) {
                        continue;
                    }
                    double col_weight = 0.0;
                    if (method == KernelMethod::Bilinear) {
                        col_weight = col_tap == 0 ? 1.0 - (x - base_col) : x - base_col;
                    } else if (method == KernelMethod::Cubic) {
                        col_weight = cubic_weight(x - source_col);
                    } else if (method == KernelMethod::CubicSpline) {
                        col_weight = cubic_spline_weight(x - source_col);
                    } else {
                        col_weight = lanczos_weight(x - source_col);
                    }
                    const double weight = row_weight * col_weight;
                    if (weight == 0.0) {
                        continue;
                    }
                    const float* source =
                        src + (source_row * src_w + source_col) * bands;
                    for (int band = 0; band < bands; ++band) {
                        if (!std::isfinite(source[band])) {
                            continue;
                        }
                        sums[static_cast<std::size_t>(band)] +=
                            static_cast<double>(source[band]) * weight;
                        weight_sums[static_cast<std::size_t>(band)] += weight;
                    }
                }
            }
            for (int band = 0; band < bands; ++band) {
                const double weight_sum = weight_sums[static_cast<std::size_t>(band)];
                if (std::abs(weight_sum) > 1e-15) {
                    destination[band] = static_cast<float>(
                        sums[static_cast<std::size_t>(band)] / weight_sum
                    );
                } else {
                    destination[band] = std::numeric_limits<float>::quiet_NaN();
                }
            }
        }
    }
}

}  // namespace ctb_native
