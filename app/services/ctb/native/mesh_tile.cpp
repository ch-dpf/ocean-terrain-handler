#include "mesh_tile.hpp"

// CTB-compatible meshing/encoding facade.
// Source behavior: ch-dpf/cesium-terrain-builder@676719d (Apache-2.0).

#include "encode.hpp"
#include "heightfield.hpp"

#include <stdexcept>

namespace ctb_native {

std::string encode_mesh_tile(
    const float* heights,
    int tile_size,
    double minx,
    double miny,
    double maxx,
    double maxy,
    double geometric_error,
    bool smooth_small_zooms,
    const float* const* neighbor_heights,
    bool write_vertex_normals,
    bool web_mercator,
    bool canonical_edges
) {
    if (heights == nullptr || tile_size < 2) {
        throw std::runtime_error("mesh tile requires a square height grid");
    }
    HeightField field(heights, tile_size);
    if (canonical_edges) field.prepare_canonical_edges(geometric_error * 0.25);
    field.apply_geometric_error(geometric_error, smooth_small_zooms);
    if (!canonical_edges && neighbor_heights != nullptr) {
        for (int border = 0; border < 4; ++border) {
            if (neighbor_heights[border] == nullptr) {
                continue;
            }
            HeightField other(neighbor_heights[border], tile_size);
            other.apply_geometric_error(geometric_error, false);
            field.apply_border_activation_state(other, border);
        }
    }
    MeshBuilder mesh(minx, miny, maxx, maxy, tile_size);
    field.generate_mesh(mesh, 0);
    if (mesh.vertices.empty()) {
        throw std::runtime_error("Mesh generation produced no vertices");
    }
    return encode_quantized_mesh(mesh.vertices, mesh.indices, write_vertex_normals, web_mercator);
}

std::string encode_heightmap_tile(const float* heights, int rows, int cols, int children) {
    if (heights == nullptr || rows < 1 || cols < 1) {
        throw std::runtime_error("heightmap tile requires a 2D height grid");
    }
    return encode_heightmap(heights, rows * cols, children);
}

}  // namespace ctb_native
