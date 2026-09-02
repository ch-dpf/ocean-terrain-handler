#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <unordered_map>
#include <utility>
#include <vector>

namespace ctb_native {

struct Vertex {
    double x;
    double y;
    double z;
};

class HeightField;

class MeshBuilder {
public:
    MeshBuilder(double minx, double miny, double maxx, double maxy, int tile_size)
        : minx_(minx),
          miny_(miny),
          maxx_(maxx),
          maxy_(maxy),
          tile_size_(tile_size),
          cell_x_((maxx - minx) / static_cast<double>(tile_size - 1)),
          cell_y_((maxy - miny) / static_cast<double>(tile_size - 1)) {}

    void clear() {
        vertices.clear();
        indices.clear();
        index_map_.clear();
        tri_odd_ = false;
        tri_index_ = 0;
    }

    void emit_vertex(const HeightField& heightfield, int x, int y);

    std::vector<Vertex> vertices;
    std::vector<int> indices;

private:
    void append(const HeightField& heightfield, int x, int y);

    double minx_;
    double miny_;
    double maxx_;
    double maxy_;
    int tile_size_;
    double cell_x_;
    double cell_y_;
    std::unordered_map<int, int> index_map_;
    std::pair<int, int> tri_[3]{{0, 0}, {0, 0}, {0, 0}};
    int tri_index_ = 0;
    bool tri_odd_ = false;
};

struct GenState {
    int my_buffer[2][2]{{-1, -1}, {-1, -1}};
    int activation_level = 0;
    int ptr = 0;
    int previous_level = 0;

    bool in_my_buffer(int x, int y) const {
        return (x == my_buffer[0][0] && y == my_buffer[0][1]) ||
               (x == my_buffer[1][0] && y == my_buffer[1][1]);
    }

    void set_my_buffer(int x, int y) {
        my_buffer[ptr][0] = x;
        my_buffer[ptr][1] = y;
    }
};

class HeightField {
public:
    HeightField(const float* heights, int size) : size_(size), heights_(heights, heights + size * size) {
        if (size < 2) {
            throw std::runtime_error("heightfield must be at least 2x2");
        }
        log_size_ = static_cast<int>(std::floor(std::log2(static_cast<double>(size) - 1.0) + 0.5));
        levels_.assign(static_cast<size_t>(size) * static_cast<size_t>(size), static_cast<std::uint8_t>(255));
    }

    int size() const { return size_; }

    int index_of(int x, int y) const { return y * size_ + x; }

    float height(int x, int y) const { return heights_[static_cast<size_t>(index_of(x, y))]; }

    int get_level(int x, int y) const {
        int packed = static_cast<int>(levels_[static_cast<size_t>(index_of(x, y))]);
        if (x & 1) {
            packed >>= 4;
        }
        packed &= 0x0F;
        if (packed == 0x0F) {
            return -1;
        }
        return packed;
    }

    void set_level(int x, int y, int newlevel) {
        newlevel &= 0x0F;
        int packed = static_cast<int>(levels_[static_cast<size_t>(index_of(x, y))]);
        if (x & 1) {
            packed = (packed & 0x0F) | (newlevel << 4);
        } else {
            packed = (packed & 0xF0) | newlevel;
        }
        levels_[static_cast<size_t>(index_of(x, y))] = static_cast<std::uint8_t>(packed);
    }

    void activate(int x, int y, int level) {
        const int current = get_level(x, y);
        if (level > current) {
            set_level(x, y, level);
        }
    }

    void apply_geometric_error(double maximum_geometric_error, bool smooth_small_zooms) {
        std::fill(levels_.begin(), levels_.end(), static_cast<std::uint8_t>(255));
        const int size = size_;
        update(maximum_geometric_error, 0, size - 1, size - 1, size - 1, 0, 0);
        update(maximum_geometric_error, size - 1, 0, 0, 0, size - 1, size - 1);
        const int last = size - 1;
        activate(last, 0, 0);
        activate(0, 0, 0);
        activate(0, last, 0);
        activate(last, last, 0);
        if (smooth_small_zooms) {
            const int step = last / 16;
            if (step > 0) {
                for (int x = 0; x <= last; x += step) {
                    for (int y = 0; y <= last; y += step) {
                        if (get_level(x, y) == -1) {
                            activate(x, y, 0);
                        }
                    }
                }
            }
        }
        for (int i = 0; i < log_size_; ++i) {
            propagate_activation_level(size >> 1, size >> 1, log_size_ - 1, i);
            propagate_activation_level(size >> 1, size >> 1, log_size_ - 1, i);
        }
    }

    void apply_border_activation_state(const HeightField& neighbor, int border_index) {
        const int size = size_;
        if (border_index == 0) {
            for (int y = 0; y < size; ++y) {
                const int level = neighbor.get_level(size - 1, y);
                if (level != -1) {
                    activate(0, y, level);
                }
            }
        } else if (border_index == 1) {
            for (int x = 0; x < size; ++x) {
                const int level = neighbor.get_level(x, size - 1);
                if (level != -1) {
                    activate(x, 0, level);
                }
            }
        } else if (border_index == 2) {
            for (int y = 0; y < size; ++y) {
                const int level = neighbor.get_level(0, y);
                if (level != -1) {
                    activate(size - 1, y, level);
                }
            }
        } else if (border_index == 3) {
            for (int x = 0; x < size; ++x) {
                const int level = neighbor.get_level(x, 0);
                if (level != -1) {
                    activate(x, size - 1, level);
                }
            }
        } else {
            throw std::runtime_error("Bad neighbor border index");
        }
        for (int i = 0; i < log_size_; ++i) {
            propagate_activation_level(size >> 1, size >> 1, log_size_ - 1, i);
            propagate_activation_level(size >> 1, size >> 1, log_size_ - 1, i);
        }
    }

    void generate_mesh(MeshBuilder& mesh, int level) {
        const int size = 1 << log_size_;
        const int half = size >> 1;
        mesh.clear();
        activate(size, 0, level);
        activate(0, 0, level);
        activate(0, size, level);
        activate(size, size, level);
        generate_block(mesh, level, log_size_, half, half);
    }

private:
    bool update(double base_max_error, int ax, int ay, int rx, int ry, int lx, int ly) {
        const int dx = lx - rx;
        const int dy = ly - ry;
        if (std::abs(dx) <= 1 && std::abs(dy) <= 1) {
            return false;
        }
        const int bx = rx + (dx >> 1);
        const int by = ry + (dy >> 1);
        const double height_b = static_cast<double>(height(bx, by));
        const double height_l = static_cast<double>(height(lx, ly));
        const double height_r = static_cast<double>(height(rx, ry));
        const double error_b = std::abs(height_b - 0.5 * (height_l + height_r));
        bool activated = false;
        if (error_b >= base_max_error) {
            const int activation_level =
                static_cast<int>(std::floor(std::log2(error_b / base_max_error) + 0.5));
            activate(bx, by, activation_level);
            activated = true;
        }
        update(base_max_error, bx, by, ax, ay, rx, ry);
        update(base_max_error, bx, by, lx, ly, ax, ay);
        return activated;
    }

    void propagate_activation_level(int cx, int cy, int level, int target_level) {
        const int half_size = 1 << level;
        const int quarter_size = half_size >> 1;
        if (level > target_level) {
            for (int j = 0; j < 2; ++j) {
                for (int i = 0; i < 2; ++i) {
                    propagate_activation_level(
                        cx - quarter_size + half_size * i,
                        cy - quarter_size + half_size * j,
                        level - 1,
                        target_level
                    );
                }
            }
            return;
        }
        if (level > 0) {
            int lev = get_level(cx + quarter_size, cy - quarter_size);
            activate(cx + half_size, cy, lev);
            activate(cx, cy - half_size, lev);
            lev = get_level(cx - quarter_size, cy - quarter_size);
            activate(cx, cy - half_size, lev);
            activate(cx - half_size, cy, lev);
            lev = get_level(cx - quarter_size, cy + quarter_size);
            activate(cx - half_size, cy, lev);
            activate(cx, cy + half_size, lev);
            lev = get_level(cx + quarter_size, cy + quarter_size);
            activate(cx, cy + half_size, lev);
            activate(cx + half_size, cy, lev);
        }
        activate(cx, cy, get_level(cx + half_size, cy));
        activate(cx, cy, get_level(cx, cy - half_size));
        activate(cx, cy, get_level(cx, cy + half_size));
        activate(cx, cy, get_level(cx - half_size, cy));
    }

    void generate_quadrant(
        MeshBuilder& mesh,
        GenState& state,
        int lx,
        int ly,
        int tx,
        int ty,
        int rx,
        int ry,
        int recursion_level
    ) {
        if (recursion_level <= 0) {
            return;
        }
        if (get_level(tx, ty) >= state.activation_level) {
            const int bx = (lx + rx) >> 1;
            const int by = (ly + ry) >> 1;
            generate_quadrant(mesh, state, lx, ly, bx, by, tx, ty, recursion_level - 1);
            if (!state.in_my_buffer(tx, ty)) {
                if ((recursion_level + state.previous_level) & 1) {
                    state.ptr ^= 1;
                } else {
                    const int x = state.my_buffer[1 - state.ptr][0];
                    const int y = state.my_buffer[1 - state.ptr][1];
                    mesh.emit_vertex(*this, x, y);
                }
                mesh.emit_vertex(*this, tx, ty);
                state.set_my_buffer(tx, ty);
                state.previous_level = recursion_level;
            }
            generate_quadrant(mesh, state, tx, ty, bx, by, rx, ry, recursion_level - 1);
        }
    }

    void generate_block(MeshBuilder& mesh, int activation_level, int log_size, int cx, int cy) {
        const int hs = 1 << (log_size - 1);
        const int q[4][2] = {
            {cx + hs, cy + hs},
            {cx + hs, cy - hs},
            {cx - hs, cy - hs},
            {cx - hs, cy + hs},
        };
        GenState state;
        state.ptr = 0;
        state.previous_level = 0;
        state.activation_level = activation_level;
        mesh.emit_vertex(*this, q[0][0], q[0][1]);
        state.set_my_buffer(q[0][0], q[0][1]);
        for (int i = 0; i < 4; ++i) {
            if ((state.previous_level & 1) == 0) {
                state.ptr ^= 1;
            } else {
                const int x = state.my_buffer[1 - state.ptr][0];
                const int y = state.my_buffer[1 - state.ptr][1];
                mesh.emit_vertex(*this, x, y);
            }
            mesh.emit_vertex(*this, q[i][0], q[i][1]);
            state.set_my_buffer(q[i][0], q[i][1]);
            state.previous_level = 2 * log_size + 1;
            const int* nxt = q[(i + 1) & 3];
            generate_quadrant(
                mesh,
                state,
                q[i][0],
                q[i][1],
                cx,
                cy,
                nxt[0],
                nxt[1],
                2 * log_size
            );
        }
        if (!state.in_my_buffer(q[0][0], q[0][1])) {
            mesh.emit_vertex(*this, q[0][0], q[0][1]);
        }
    }

    int size_;
    int log_size_;
    std::vector<float> heights_;
    std::vector<std::uint8_t> levels_;
};

inline void MeshBuilder::emit_vertex(const HeightField& heightfield, int x, int y) {
    tri_[tri_index_] = {x, y};
    ++tri_index_;
    if (tri_index_ == 3) {
        tri_odd_ = !tri_odd_;
        if (tri_odd_) {
            append(heightfield, tri_[0].first, tri_[0].second);
            append(heightfield, tri_[1].first, tri_[1].second);
            append(heightfield, tri_[2].first, tri_[2].second);
        } else {
            append(heightfield, tri_[1].first, tri_[1].second);
            append(heightfield, tri_[0].first, tri_[0].second);
            append(heightfield, tri_[2].first, tri_[2].second);
        }
        tri_[0] = tri_[1];
        tri_[1] = tri_[2];
        --tri_index_;
    }
}

inline void MeshBuilder::append(const HeightField& heightfield, int x, int y) {
    const int index = heightfield.index_of(x, y);
    auto it = index_map_.find(index);
    int mapped;
    if (it == index_map_.end()) {
        mapped = static_cast<int>(vertices.size());
        const double height = static_cast<double>(heightfield.height(x, y));
        vertices.push_back(Vertex{
            minx_ + (static_cast<double>(x) * cell_x_),
            maxy_ - (static_cast<double>(y) * cell_y_),
            height,
        });
        index_map_[index] = mapped;
    } else {
        mapped = it->second;
    }
    indices.push_back(mapped);
}

}  // namespace ctb_native
