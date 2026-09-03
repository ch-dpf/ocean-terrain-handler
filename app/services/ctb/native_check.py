"""Fail-fast validation for bare-metal and wheel installations."""

from __future__ import annotations

import gzip
import json
import platform
import struct

import numpy as np

from app.services.ctb.constants import CTB_SOURCE_COMMIT
from app.services.ctb.mesh_encode import (
    aggregate_footprints_f32,
    box_average_f32,
    encode_mesh_tile_bytes,
    native_import_error,
    remap_f32_hwc,
    require_native,
)


def check_native() -> dict[str, object]:
    """Exercise meshing, encoding, gzip wrapping, and aggregate sampling."""
    require_native()
    size = 65
    heights = np.linspace(0.0, 100.0, size * size, dtype=np.float32).reshape(size, size)
    payload = encode_mesh_tile_bytes(
        heights,
        -180.0,
        -90.0,
        0.0,
        90.0,
        1.0,
        True,
        None,
        True,
    )
    raw = gzip.decompress(payload)
    vertex_count = struct.unpack_from("<i", raw, 88)[0]
    if vertex_count <= 0:
        raise RuntimeError("native CTB self-check produced an empty mesh")

    source = np.array([[0.0, 10.0], [20.0, 30.0]], dtype=np.float32)
    rows = np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float64)
    cols = np.array([[0.5, 1.5], [0.5, 1.5]], dtype=np.float64)
    average = float(aggregate_footprints_f32(source, rows, cols, 0, np.nan)[0, 0])
    if not np.isclose(average, 5.0):
        raise RuntimeError(f"native aggregate self-check failed: {average}")

    boxed = float(box_average_f32(source, 1, 1, np.nan)[0, 0])
    if not np.isclose(boxed, 15.0):
        raise RuntimeError(f"native box-average self-check failed: {boxed}")

    identity = remap_f32_hwc(
        source[:, :, np.newaxis],
        np.array([[0.5, 1.5], [0.5, 1.5]], dtype=np.float64),
        np.array([[0.5, 0.5], [1.5, 1.5]], dtype=np.float64),
        1,
    )[:, :, 0]
    if not np.allclose(identity, source, atol=1e-5):
        raise RuntimeError("native remap self-check failed")

    return {
        "ok": True,
        "machine": platform.machine(),
        "platform": platform.system(),
        "python": platform.python_version(),
        "ctb_source_commit": CTB_SOURCE_COMMIT,
        "vertices": vertex_count,
        "native_import_error": native_import_error(),
    }


def main() -> None:
    print(json.dumps(check_native(), sort_keys=True))


if __name__ == "__main__":
    main()
