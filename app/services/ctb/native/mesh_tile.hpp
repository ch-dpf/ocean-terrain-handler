#pragma once

#include <string>

namespace ctb_native {

// Heights are row-major tile_size x tile_size float32. neighbor_heights[i] may be null.
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
);

std::string encode_heightmap_tile(const float* heights, int rows, int cols, int children);

}  // namespace ctb_native
