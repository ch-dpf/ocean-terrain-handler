#pragma once

// Self-contained adaptation of CTB MeshTile.cpp / TerrainTile.cpp.
// Source: ch-dpf/cesium-terrain-builder@676719d (Apache-2.0).

#include "heightfield.hpp"

#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace ctb_native {

constexpr double kRadiusX = 6378137.0;
constexpr double kRadiusY = 6378137.0;
constexpr double kRadiusZ = 6356752.3142451793;
constexpr double kWgs84E2 = 0.0066943799901975848;
constexpr double kShortMax = 32767.0;
constexpr int kByteSplit = 65636;
constexpr int kExtensionOctVertexNormals = 1;
constexpr double kHeightmapOffsetM = 1000.0;
constexpr double kHeightmapScale = 5.0;

inline int cpp_round(double value) {
    if (value >= 0.0) {
        return static_cast<int>(std::floor(value + 0.5));
    }
    return static_cast<int>(std::ceil(value - 0.5));
}

inline std::uint16_t zigzag_encode(int n) {
    const std::int32_t n32 = static_cast<std::int32_t>(n);
    return static_cast<std::uint16_t>((n32 << 1) ^ (n32 >> 31));
}

inline int quantize_index(double origin, double factor, double value) {
    return cpp_round((value - origin) * factor);
}

struct Vec3 {
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;

    Vec3 operator+(const Vec3& other) const { return Vec3{x + other.x, y + other.y, z + other.z}; }
    Vec3 operator-(const Vec3& other) const { return Vec3{x - other.x, y - other.y, z - other.z}; }
    Vec3 operator*(double scalar) const { return Vec3{x * scalar, y * scalar, z * scalar}; }

    double operator[](int index) const {
        if (index == 0) {
            return x;
        }
        if (index == 1) {
            return y;
        }
        return z;
    }

    double dot(const Vec3& other) const { return x * other.x + y * other.y + z * other.z; }

    Vec3 cross(const Vec3& other) const {
        return Vec3{
            y * other.z - other.y * z,
            z * other.x - other.z * x,
            x * other.y - other.x * y,
        };
    }

    double magnitude_squared() const { return x * x + y * y + z * z; }

    double magnitude() const { return std::sqrt(magnitude_squared()); }

    Vec3 normalize() const {
        const double mag = magnitude();
        if (mag == 0.0) {
            return Vec3{0.0, 0.0, 0.0};
        }
        return Vec3{x / mag, y / mag, z / mag};
    }
};

inline double llh_ecef_n(double lat_rad) {
    const double snx = std::sin(lat_rad);
    return kRadiusX / std::sqrt(1.0 - kWgs84E2 * (snx * snx));
}

inline Vec3 llh_to_ecef(double lon_deg, double lat_deg, double alt) {
    const double lon = lon_deg * (3.14159265358979323846 / 180.0);
    const double lat = lat_deg * (3.14159265358979323846 / 180.0);
    const double n = llh_ecef_n(lat);
    return Vec3{
        (n + alt) * std::cos(lat) * std::cos(lon),
        (n + alt) * std::cos(lat) * std::sin(lon),
        (n * (1.0 - kWgs84E2) + alt) * std::sin(lat),
    };
}

inline double ocp_compute_magnitude(const Vec3& position, const Vec3& sphere_center) {
    double magnitude_squared = position.magnitude_squared();
    double magnitude = std::sqrt(magnitude_squared);
    const Vec3 direction = position * (1.0 / magnitude);
    magnitude_squared = std::max(1.0, magnitude_squared);
    magnitude = std::max(1.0, magnitude);
    const double cos_alpha = direction.dot(sphere_center);
    const double sin_alpha = direction.cross(sphere_center).magnitude();
    const double cos_beta = 1.0 / magnitude;
    const double sin_beta = std::sqrt(magnitude_squared - 1.0) * cos_beta;
    const double denom = cos_alpha * cos_beta - sin_alpha * sin_beta;
    if (denom == 0.0) {
        return std::numeric_limits<double>::infinity();
    }
    return 1.0 / denom;
}

inline std::pair<Vec3, double> bounding_sphere_from_points(const std::vector<Vec3>& points) {
    const double inf = std::numeric_limits<double>::infinity();
    Vec3 min_x{inf, inf, inf};
    Vec3 min_y{inf, inf, inf};
    Vec3 min_z{inf, inf, inf};
    Vec3 max_x{-inf, -inf, -inf};
    Vec3 max_y{-inf, -inf, -inf};
    Vec3 max_z{-inf, -inf, -inf};
    for (const Vec3& point : points) {
        if (point.x < min_x.x) {
            min_x = point;
        }
        if (point.y < min_y.y) {
            min_y = point;
        }
        if (point.z < min_z.z) {
            min_z = point;
        }
        if (point.x > max_x.x) {
            max_x = point;
        }
        if (point.y > max_y.y) {
            max_y = point;
        }
        if (point.z > max_z.z) {
            max_z = point;
        }
    }
    double x_span = (max_x - min_x).magnitude_squared();
    double y_span = (max_y - min_y).magnitude_squared();
    double z_span = (max_z - min_z).magnitude_squared();
    Vec3 diameter1 = min_x;
    Vec3 diameter2 = max_x;
    double max_span = x_span;
    if (y_span > max_span) {
        diameter1 = min_y;
        diameter2 = max_y;
        max_span = y_span;
    }
    if (z_span > max_span) {
        diameter1 = min_z;
        diameter2 = max_z;
        max_span = z_span;
    }
    Vec3 ritter_center{
        (diameter1.x + diameter2.x) * 0.5,
        (diameter1.y + diameter2.y) * 0.5,
        (diameter1.z + diameter2.z) * 0.5,
    };
    double radius_squared = (diameter2 - ritter_center).magnitude_squared();
    double ritter_radius = std::sqrt(radius_squared);
    const Vec3 min_box{min_x.x, min_y.y, min_z.z};
    const Vec3 max_box{max_x.x, max_y.y, max_z.z};
    const Vec3 naive_center = (min_box + max_box) * 0.5;
    double naive_radius = 0.0;
    for (const Vec3& point : points) {
        const double radius = (point - naive_center).magnitude();
        if (radius > naive_radius) {
            naive_radius = radius;
        }
        const double old_center_to_point_sq = (point - ritter_center).magnitude_squared();
        if (old_center_to_point_sq > radius_squared) {
            const double old_center_to_point = std::sqrt(old_center_to_point_sq);
            ritter_radius = (ritter_radius + old_center_to_point) * 0.5;
            const double old_to_new = old_center_to_point - ritter_radius;
            ritter_center = Vec3{
                (ritter_radius * ritter_center.x + old_to_new * point.x) / old_center_to_point,
                (ritter_radius * ritter_center.y + old_to_new * point.y) / old_center_to_point,
                (ritter_radius * ritter_center.z + old_to_new * point.z) / old_center_to_point,
            };
            radius_squared = ritter_radius * ritter_radius;
        }
    }
    if (naive_radius < ritter_radius) {
        return {ritter_center, ritter_radius};
    }
    return {naive_center, naive_radius};
}

inline std::pair<Vec3, Vec3> bounding_box_from_xyz(const std::vector<Vec3>& points) {
    Vec3 min_v{
        std::numeric_limits<double>::infinity(),
        std::numeric_limits<double>::infinity(),
        std::numeric_limits<double>::infinity(),
    };
    Vec3 max_v{
        -std::numeric_limits<double>::infinity(),
        -std::numeric_limits<double>::infinity(),
        -std::numeric_limits<double>::infinity(),
    };
    for (const Vec3& point : points) {
        min_v.x = std::min(min_v.x, point.x);
        min_v.y = std::min(min_v.y, point.y);
        min_v.z = std::min(min_v.z, point.z);
        max_v.x = std::max(max_v.x, point.x);
        max_v.y = std::max(max_v.y, point.y);
        max_v.z = std::max(max_v.z, point.z);
    }
    return {min_v, max_v};
}

inline Vec3 ocp_from_points(const std::vector<Vec3>& points, const Vec3& sphere_center) {
    const double rx = 1.0 / kRadiusX;
    const double ry = 1.0 / kRadiusY;
    const double rz = 1.0 / kRadiusZ;
    const Vec3 scaled_center{sphere_center.x * rx, sphere_center.y * ry, sphere_center.z * rz};
    double max_magnitude = -std::numeric_limits<double>::infinity();
    for (const Vec3& point : points) {
        const Vec3 scaled{point.x * rx, point.y * ry, point.z * rz};
        const double magnitude = ocp_compute_magnitude(scaled, scaled_center);
        if (magnitude > max_magnitude) {
            max_magnitude = magnitude;
        }
    }
    return scaled_center * max_magnitude;
}

inline double clamp_value(double value, double low, double high) {
    if (value < low) {
        return low;
    }
    if (value > high) {
        return high;
    }
    return value;
}

inline int snorm_value(double value, double range_max = 255.0) {
    return cpp_round((clamp_value(value, -1.0, 1.0) * 0.5 + 0.5) * range_max) & 0xFF;
}

inline std::pair<int, int> oct_encode(const Vec3& vector, double range_max = 255.0) {
    const double llnorm = std::abs(vector.x) + std::abs(vector.y) + std::abs(vector.z);
    if (llnorm == 0.0) {
        return {snorm_value(0.0, range_max), snorm_value(0.0, range_max)};
    }
    double temp_x = vector.x / llnorm;
    double temp_y = vector.y / llnorm;
    if (vector.z < 0) {
        const double x = temp_x;
        const double y = temp_y;
        temp_x = (1.0 - std::abs(y)) * (x < 0.0 ? -1.0 : 1.0);
        temp_y = (1.0 - std::abs(x)) * (y < 0.0 ? -1.0 : 1.0);
    }
    return {snorm_value(temp_x, range_max), snorm_value(temp_y, range_max)};
}

inline double triangle_area(const Vec3& a, const Vec3& b) {
    const double i = (a[1] * b[2] - a[2] * b[1]);
    const double j = (a[2] * b[0] - a[0] * b[2]);
    const double k = (a[0] * b[1] - a[1] * b[0]);
    return 0.5 * std::sqrt(i * i + j * j + k * k);
}

template <typename T>
inline void write_le(std::vector<std::uint8_t>& buf, T value) {
    const auto* raw = reinterpret_cast<const std::uint8_t*>(&value);
    buf.insert(buf.end(), raw, raw + sizeof(T));
}

inline std::string as_string(const std::vector<std::uint8_t>& data) {
    return std::string(reinterpret_cast<const char*>(data.data()), data.size());
}

inline void write_edge_indices(
    std::vector<std::uint8_t>& buf,
    const std::vector<Vertex>& vertices,
    const std::vector<int>& indices,
    double edge_coord,
    int component,
    bool wide
) {
    std::unordered_map<int, int> seen;
    std::vector<int> edge;
    for (int i = 0; i < static_cast<int>(indices.size()); ++i) {
        const int indice = indices[static_cast<size_t>(i)];
        const Vertex& vertex = vertices[static_cast<size_t>(indice)];
        const double val = component == 0 ? vertex.x : (component == 1 ? vertex.y : vertex.z);
        if (val == edge_coord && seen.find(indice) == seen.end()) {
            seen[indice] = i;
            edge.push_back(indice);
        }
    }
    write_le<std::int32_t>(buf, static_cast<std::int32_t>(edge.size()));
    for (int indice : edge) {
        if (wide) {
            write_le<std::uint32_t>(buf, static_cast<std::uint32_t>(indice));
        } else {
            write_le<std::uint16_t>(buf, static_cast<std::uint16_t>(indice & 0xFFFF));
        }
    }
}

inline std::string encode_quantized_mesh(
    const std::vector<Vertex>& vertices,
    const std::vector<int>& indices,
    bool write_vertex_normals
) {
    std::vector<Vec3> cartesian;
    cartesian.reserve(vertices.size());
    for (const Vertex& vertex : vertices) {
        cartesian.push_back(llh_to_ecef(vertex.x, vertex.y, vertex.z));
    }
    const auto sphere = bounding_sphere_from_points(cartesian);
    const Vec3 sphere_center = sphere.first;
    const double sphere_radius = sphere.second;
    const auto cart_box = bounding_box_from_xyz(cartesian);
    std::vector<Vec3> llh_points;
    llh_points.reserve(vertices.size());
    for (const Vertex& vertex : vertices) {
        llh_points.push_back(Vec3{vertex.x, vertex.y, vertex.z});
    }
    const auto bounds = bounding_box_from_xyz(llh_points);
    const Vec3& bounds_min = bounds.first;
    const Vec3& bounds_max = bounds.second;
    const Vec3& cart_min = cart_box.first;
    const Vec3& cart_max = cart_box.second;

    std::vector<std::uint8_t> buf;
    const double center_x = cart_min.x + 0.5 * (cart_max.x - cart_min.x);
    const double center_y = cart_min.y + 0.5 * (cart_max.y - cart_min.y);
    const double center_z = cart_min.z + 0.5 * (cart_max.z - cart_min.z);
    write_le<double>(buf, center_x);
    write_le<double>(buf, center_y);
    write_le<double>(buf, center_z);
    write_le<float>(buf, static_cast<float>(bounds_min.z));
    write_le<float>(buf, static_cast<float>(bounds_max.z));
    write_le<double>(buf, sphere_center.x);
    write_le<double>(buf, sphere_center.y);
    write_le<double>(buf, sphere_center.z);
    write_le<double>(buf, sphere_radius);
    const Vec3 horizon = ocp_from_points(cartesian, sphere_center);
    write_le<double>(buf, horizon.x);
    write_le<double>(buf, horizon.y);
    write_le<double>(buf, horizon.z);

    const int vertex_count = static_cast<int>(vertices.size());
    write_le<std::int32_t>(buf, vertex_count);
    const double origin[3] = {bounds_min.x, bounds_min.y, bounds_min.z};
    const double span[3] = {
        bounds_max.x - bounds_min.x,
        bounds_max.y - bounds_min.y,
        bounds_max.z - bounds_min.z,
    };
    for (int component = 0; component < 3; ++component) {
        const double factor = span[component] > 0.0 ? kShortMax / span[component] : 0.0;
        const auto coord = [&](int i) {
            const Vertex& vertex = vertices[static_cast<size_t>(i)];
            if (component == 0) {
                return vertex.x;
            }
            if (component == 1) {
                return vertex.y;
            }
            return vertex.z;
        };
        int u0 = quantize_index(origin[component], factor, coord(0));
        write_le<std::uint16_t>(buf, zigzag_encode(u0));
        for (int i = 1; i < vertex_count; ++i) {
            const int u1 = quantize_index(origin[component], factor, coord(i));
            write_le<std::uint16_t>(buf, zigzag_encode(u1 - u0));
            u0 = u1;
        }
    }

    const int triangle_count = static_cast<int>(indices.size()) / 3;
    write_le<std::int32_t>(buf, triangle_count);
    const bool wide = vertex_count > kByteSplit;
    if (wide) {
        int highest = 0;
        for (int indice : indices) {
            const std::uint32_t code = static_cast<std::uint32_t>(highest - indice);
            write_le<std::uint32_t>(buf, code);
            if (code == 0) {
                ++highest;
            }
        }
        write_edge_indices(buf, vertices, indices, bounds_min.x, 0, true);
        write_edge_indices(buf, vertices, indices, bounds_min.y, 1, true);
        write_edge_indices(buf, vertices, indices, bounds_max.x, 0, true);
        write_edge_indices(buf, vertices, indices, bounds_max.y, 1, true);
    } else {
        int highest = 0;
        for (int indice : indices) {
            const std::uint16_t code = static_cast<std::uint16_t>((highest - indice) & 0xFFFF);
            write_le<std::uint16_t>(buf, code);
            if (code == 0) {
                ++highest;
            }
        }
        write_edge_indices(buf, vertices, indices, bounds_min.x, 0, false);
        write_edge_indices(buf, vertices, indices, bounds_min.y, 1, false);
        write_edge_indices(buf, vertices, indices, bounds_max.x, 0, false);
        write_edge_indices(buf, vertices, indices, bounds_max.y, 1, false);
    }

    if (write_vertex_normals && triangle_count > 0) {
        write_le<std::uint8_t>(buf, static_cast<std::uint8_t>(kExtensionOctVertexNormals));
        write_le<std::int32_t>(buf, 2 * vertex_count);
        std::vector<Vec3> normals_vertex(static_cast<size_t>(vertex_count));
        for (int j = 0; j < triangle_count; ++j) {
            const int i0 = indices[static_cast<size_t>(j * 3)];
            const int i1 = indices[static_cast<size_t>(j * 3 + 1)];
            const int i2 = indices[static_cast<size_t>(j * 3 + 2)];
            const Vec3& v0 = cartesian[static_cast<size_t>(i0)];
            const Vec3& v1 = cartesian[static_cast<size_t>(i1)];
            const Vec3& v2 = cartesian[static_cast<size_t>(i2)];
            const Vec3 normal = (v1 - v0).cross(v2 - v0);
            const double area = triangle_area(v0, v1);
            const Vec3 weighted = normal * area;
            normals_vertex[static_cast<size_t>(i0)] = normals_vertex[static_cast<size_t>(i0)] + weighted;
            normals_vertex[static_cast<size_t>(i1)] = normals_vertex[static_cast<size_t>(i1)] + weighted;
            normals_vertex[static_cast<size_t>(i2)] = normals_vertex[static_cast<size_t>(i2)] + weighted;
        }
        for (const Vec3& normal : normals_vertex) {
            const auto encoded = oct_encode(normal.normalize());
            write_le<std::uint8_t>(buf, static_cast<std::uint8_t>(encoded.first));
            write_le<std::uint8_t>(buf, static_cast<std::uint8_t>(encoded.second));
        }
    }
    return as_string(buf);
}

inline std::string encode_heightmap(const float* heights, int count, int children) {
    std::vector<std::uint8_t> buf;
    buf.reserve(static_cast<size_t>(count) * 2 + 2);
    for (int i = 0; i < count; ++i) {
        const double value = (static_cast<double>(heights[i]) + kHeightmapOffsetM) * kHeightmapScale;
        const auto encoded = static_cast<std::uint16_t>(static_cast<std::int32_t>(std::trunc(value)));
        write_le<std::uint16_t>(buf, encoded);
    }
    buf.push_back(static_cast<std::uint8_t>(children & 0xFF));
    buf.push_back(0);
    return as_string(buf);
}

}  // namespace ctb_native
