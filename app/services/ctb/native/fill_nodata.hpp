// Quadrant search adapted from GDAL 3.12.4 alg/rasterfill.cpp (MIT).
// Copyright (c) 2008, Frank Warmerdam
// Copyright (c) 2009-2013, Even Rouault
// Copyright (c) 2015, Sean Gillies
// See THIRD_PARTY_NOTICES.md for the license.
#pragma once
#include <algorithm>
#include <cmath>
#include <vector>

namespace ctb_native {
inline void fill_nodata(const float* src, int h, int w, int radius, float* out) {
    // Only original finite samples are donors. Bottom is strictly below the
    // current row; top includes the row, matching GDAL's tie-breaking order.
    const auto count = static_cast<size_t>(h) * w;
    std::copy(src, src + count, out);
    std::vector<int> top(count, -1), bottom(count, -1);
    for (int x = 0; x < w; ++x) {
        int last = -1;
        for (int y = 0; y < h; ++y) {
            const auto i = static_cast<size_t>(y) * w + x;
            if (std::isfinite(src[i])) last = y;
            if (last >= 0 && y - last <= radius) top[i] = last;
        }
        last = -1;
        for (int y = h - 1; y >= 0; --y) {
            const auto i = static_cast<size_t>(y) * w + x;
            if (last >= 0 && last - y <= radius) bottom[i] = last;
            if (std::isfinite(src[i])) last = y;
        }
    }
    for (int y = 0; y < h; ++y) for (int x = 0; x < w; ++x) {
        const auto i = static_cast<size_t>(y) * w + x;
        if (std::isfinite(src[i])) continue;
        double distance[4] = {radius+1., radius+1., radius+1., radius+1.};
        float values[4] = {};
        int limit = radius;
        for (int step = 0; step <= limit; ++step) {
            const int columns[2] = {std::max(0, x-step), std::min(w-1, x+step)};
            for (int side = 0; side < (step == 0 ? 1 : 2); ++side) {
                const int cx = columns[side];
                const auto ci = static_cast<size_t>(y) * w + cx;
                const int ys[2] = {top[ci], bottom[ci]};
                for (int vertical = 0; vertical < 2; ++vertical) {
                    const int cy = ys[vertical], q = side*2 + vertical;
                    if (cy < 0) continue;
                    const double dx = cx-x, dy = cy-y, square = dx*dx + dy*dy;
                    if (square > 0 && square < distance[q]*distance[q]) {
                        distance[q] = std::sqrt(square);
                        values[q] = src[static_cast<size_t>(cy)*w+cx];
                    }
                }
            }
            if (step > 0 && (step & 3) == 0)
                limit = static_cast<int>(std::floor(*std::max_element(distance, distance+4)));
        }
        double sum = 0, weights = 0;
        for (int q = 0; q < 4; ++q) if (distance[q] <= radius) {
            const double weight = 1. / distance[q];
            sum += values[q] * weight;
            weights += weight;
        }
        if (weights > 0) out[i] = static_cast<float>(sum / weights);
    }
}
}  // namespace ctb_native
