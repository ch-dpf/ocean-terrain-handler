"""Thatcher Ulrich Chunked LOD + Lindstrom–Koller BTT (CTB HeightFieldChunker.hpp)."""

from __future__ import annotations

import math

import numpy as np


class MeshBuilder:
    """WrapperMesh in MeshTiler.cpp: triangle-strip → indexed triangles."""

    def __init__(self, minx: float, miny: float, maxx: float, maxy: float, tile_size: int) -> None:
        self.minx = minx
        self.miny = miny
        self.maxx = maxx
        self.maxy = maxy
        self.tile_size = tile_size
        self.cell_x = (maxx - minx) / float(tile_size - 1)
        self.cell_y = (maxy - miny) / float(tile_size - 1)
        self.vertices: list[tuple[float, float, float]] = []
        self.indices: list[int] = []
        self._index_map: dict[int, int] = {}
        self._tri: list[tuple[int, int]] = [(0, 0), (0, 0), (0, 0)]
        self._tri_index = 0
        self._tri_odd = False

    def clear(self) -> None:
        self.vertices.clear()
        self.indices.clear()
        self._index_map.clear()
        self._tri_odd = False
        self._tri_index = 0

    def emit_vertex(self, heightfield: HeightField, x: int, y: int) -> None:
        self._tri[self._tri_index] = (x, y)
        self._tri_index += 1
        if self._tri_index == 3:
            self._tri_odd = not self._tri_odd
            if self._tri_odd:
                self._append(heightfield, self._tri[0][0], self._tri[0][1])
                self._append(heightfield, self._tri[1][0], self._tri[1][1])
                self._append(heightfield, self._tri[2][0], self._tri[2][1])
            else:
                self._append(heightfield, self._tri[1][0], self._tri[1][1])
                self._append(heightfield, self._tri[0][0], self._tri[0][1])
                self._append(heightfield, self._tri[2][0], self._tri[2][1])
            self._tri[0] = self._tri[1]
            self._tri[1] = self._tri[2]
            self._tri_index -= 1

    def _append(self, heightfield: HeightField, x: int, y: int) -> None:
        index = heightfield.index_of(x, y)
        mapped = self._index_map.get(index)
        if mapped is None:
            mapped = len(self.vertices)
            height = float(heightfield.height(x, y))
            self.vertices.append(
                (self.minx + (x * self.cell_x), self.maxy - (y * self.cell_y), height)
            )
            self._index_map[index] = mapped
        self.indices.append(mapped)


class _GenState:
    __slots__ = ("my_buffer", "activation_level", "ptr", "previous_level")

    def __init__(self) -> None:
        self.my_buffer = [[-1, -1], [-1, -1]]
        self.activation_level = 0
        self.ptr = 0
        self.previous_level = 0

    def in_my_buffer(self, x: int, y: int) -> bool:
        return (x == self.my_buffer[0][0] and y == self.my_buffer[0][1]) or (
            x == self.my_buffer[1][0] and y == self.my_buffer[1][1]
        )

    def set_my_buffer(self, x: int, y: int) -> None:
        self.my_buffer[self.ptr][0] = x
        self.my_buffer[self.ptr][1] = y


class HeightField:
    """Regular height grid with Lindstrom–Koller activation levels."""

    def __init__(self, heights: np.ndarray) -> None:
        if heights.ndim != 2 or heights.shape[0] != heights.shape[1]:
            raise ValueError("heightfield must be a square 2D array")
        self.size = int(heights.shape[0])
        edge = self.size - 1
        if self.size < 3 or edge & (edge - 1):
            raise ValueError("heightfield size must be 2^n + 1")
        self.heights = np.asarray(heights, dtype=np.float32)
        self.log_size = int(math.log2(float(self.size) - 1) + 0.5)
        self.levels = np.full((self.size, self.size), 255, dtype=np.uint8)

    def index_of(self, x: int, y: int) -> int:
        return y * self.size + x

    def height(self, x: int, y: int) -> float:
        return float(self.heights[y, x])

    def get_level(self, x: int, y: int) -> int:
        packed = int(self.levels[y, x])
        if x & 1:
            packed = packed >> 4
        packed &= 0x0F
        if packed == 0x0F:
            return -1
        return packed

    def set_level(self, x: int, y: int, newlevel: int) -> None:
        newlevel &= 0x0F
        packed = int(self.levels[y, x])
        if x & 1:
            packed = (packed & 0x0F) | (newlevel << 4)
        else:
            packed = (packed & 0xF0) | newlevel
        self.levels[y, x] = packed

    def activate(self, x: int, y: int, level: int) -> None:
        current = self.get_level(x, y)
        if level > current:
            self.set_level(x, y, level)

    def apply_geometric_error(self, maximum_geometric_error: float, smooth_small_zooms: bool = False) -> None:
        self.levels.fill(255)
        size = self.size
        self._update(maximum_geometric_error, 0, size - 1, size - 1, size - 1, 0, 0)
        self._update(maximum_geometric_error, size - 1, 0, 0, 0, size - 1, size - 1)
        last = size - 1
        self.activate(last, 0, 0)
        self.activate(0, 0, 0)
        self.activate(0, last, 0)
        self.activate(last, last, 0)
        if smooth_small_zooms:
            step = last // 16
            if step > 0:
                for x in range(0, last + 1, step):
                    for y in range(0, last + 1, step):
                        if self.get_level(x, y) == -1:
                            self.activate(x, y, 0)
        for i in range(self.log_size):
            self._propagate_activation_level(size >> 1, size >> 1, self.log_size - 1, i)
            self._propagate_activation_level(size >> 1, size >> 1, self.log_size - 1, i)

    def apply_border_activation_state(self, neighbor: HeightField, border_index: int) -> None:
        size = self.size
        if border_index == 0:
            for y in range(size):
                level = neighbor.get_level(size - 1, y)
                if level != -1:
                    self.activate(0, y, level)
        elif border_index == 1:
            for x in range(size):
                level = neighbor.get_level(x, size - 1)
                if level != -1:
                    self.activate(x, 0, level)
        elif border_index == 2:
            for y in range(size):
                level = neighbor.get_level(0, y)
                if level != -1:
                    self.activate(size - 1, y, level)
        elif border_index == 3:
            for x in range(size):
                level = neighbor.get_level(x, 0)
                if level != -1:
                    self.activate(x, size - 1, level)
        else:
            raise ValueError(f"Bad neighbor border index: {border_index}")
        for i in range(self.log_size):
            self._propagate_activation_level(size >> 1, size >> 1, self.log_size - 1, i)
            self._propagate_activation_level(size >> 1, size >> 1, self.log_size - 1, i)

    def generate_mesh(self, mesh: MeshBuilder, level: int) -> None:
        size = 1 << self.log_size
        half = size >> 1
        mesh.clear()
        self.activate(size, 0, level)
        self.activate(0, 0, level)
        self.activate(0, size, level)
        self.activate(size, size, level)
        self._generate_block(mesh, level, self.log_size, half, half)

    def _update(self, base_max_error: float, ax: int, ay: int, rx: int, ry: int, lx: int, ly: int) -> bool:
        dx = lx - rx
        dy = ly - ry
        if abs(dx) <= 1 and abs(dy) <= 1:
            return False
        bx = rx + (dx >> 1)
        by = ry + (dy >> 1)
        height_b = self.height(bx, by)
        height_l = self.height(lx, ly)
        height_r = self.height(rx, ry)
        error_b = abs(height_b - 0.5 * (height_l + height_r))
        activated = False
        if error_b >= base_max_error:
            activation_level = int(math.floor(math.log2(error_b / base_max_error) + 0.5))
            self.activate(bx, by, activation_level)
            activated = True
        self._update(base_max_error, bx, by, ax, ay, rx, ry)
        self._update(base_max_error, bx, by, lx, ly, ax, ay)
        return activated

    def _propagate_activation_level(self, cx: int, cy: int, level: int, target_level: int) -> None:
        half_size = 1 << level
        quarter_size = half_size >> 1
        if level > target_level:
            for j in range(2):
                for i in range(2):
                    self._propagate_activation_level(
                        cx - quarter_size + half_size * i,
                        cy - quarter_size + half_size * j,
                        level - 1,
                        target_level,
                    )
            return
        if level > 0:
            lev = self.get_level(cx + quarter_size, cy - quarter_size)
            self.activate(cx + half_size, cy, lev)
            self.activate(cx, cy - half_size, lev)
            lev = self.get_level(cx - quarter_size, cy - quarter_size)
            self.activate(cx, cy - half_size, lev)
            self.activate(cx - half_size, cy, lev)
            lev = self.get_level(cx - quarter_size, cy + quarter_size)
            self.activate(cx - half_size, cy, lev)
            self.activate(cx, cy + half_size, lev)
            lev = self.get_level(cx + quarter_size, cy + quarter_size)
            self.activate(cx, cy + half_size, lev)
            self.activate(cx + half_size, cy, lev)
        self.activate(cx, cy, self.get_level(cx + half_size, cy))
        self.activate(cx, cy, self.get_level(cx, cy - half_size))
        self.activate(cx, cy, self.get_level(cx, cy + half_size))
        self.activate(cx, cy, self.get_level(cx - half_size, cy))

    def _generate_quadrant(
        self,
        mesh: MeshBuilder,
        state: _GenState,
        lx: int,
        ly: int,
        tx: int,
        ty: int,
        rx: int,
        ry: int,
        recursion_level: int,
    ) -> None:
        if recursion_level <= 0:
            return
        if self.get_level(tx, ty) >= state.activation_level:
            bx = (lx + rx) >> 1
            by = (ly + ry) >> 1
            self._generate_quadrant(mesh, state, lx, ly, bx, by, tx, ty, recursion_level - 1)
            if not state.in_my_buffer(tx, ty):
                if (recursion_level + state.previous_level) & 1:
                    state.ptr ^= 1
                else:
                    x = state.my_buffer[1 - state.ptr][0]
                    y = state.my_buffer[1 - state.ptr][1]
                    mesh.emit_vertex(self, x, y)
                mesh.emit_vertex(self, tx, ty)
                state.set_my_buffer(tx, ty)
                state.previous_level = recursion_level
            self._generate_quadrant(mesh, state, tx, ty, bx, by, rx, ry, recursion_level - 1)

    def _generate_block(self, mesh: MeshBuilder, activation_level: int, log_size: int, cx: int, cy: int) -> None:
        hs = 1 << (log_size - 1)
        q = (
            (cx + hs, cy + hs),
            (cx + hs, cy - hs),
            (cx - hs, cy - hs),
            (cx - hs, cy + hs),
        )
        state = _GenState()
        state.ptr = 0
        state.previous_level = 0
        state.activation_level = activation_level
        mesh.emit_vertex(self, q[0][0], q[0][1])
        state.set_my_buffer(q[0][0], q[0][1])
        for i in range(4):
            if (state.previous_level & 1) == 0:
                state.ptr ^= 1
            else:
                x = state.my_buffer[1 - state.ptr][0]
                y = state.my_buffer[1 - state.ptr][1]
                mesh.emit_vertex(self, x, y)
            mesh.emit_vertex(self, q[i][0], q[i][1])
            state.set_my_buffer(q[i][0], q[i][1])
            state.previous_level = 2 * log_size + 1
            nxt = q[(i + 1) & 3]
            self._generate_quadrant(
                mesh,
                state,
                q[i][0],
                q[i][1],
                cx,
                cy,
                nxt[0],
                nxt[1],
                2 * log_size,
            )
        if not state.in_my_buffer(q[0][0], q[0][1]):
            mesh.emit_vertex(self, q[0][0], q[0][1])
