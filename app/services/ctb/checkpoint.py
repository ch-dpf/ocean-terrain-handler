"""Bind resume to source content and encoding options; reject partial gzip files."""

import gzip
import hashlib
import json
import struct
import zlib
from pathlib import Path

import numpy as np


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def job_signature(source, options):
    # Sidecars affect sampling and must also invalidate stale resume output.
    keys = (
        "output_format",
        "profile",
        "tile_size",
        "start_zoom",
        "end_zoom",
        "resampling_method",
        "mesh_qfactor",
        "vertex_normals",
        "cesium_friendly",
    )
    values = {key: getattr(getattr(options, key), "value", getattr(options, key)) for key in keys}
    from app.services.ctb.mesh_encode import _native_module, require_native

    require_native()

    return {
        "engine": "terrain-v4-global-support",
        "native_sha256": file_digest(Path(_native_module.__file__)),
        "source_sha256": file_digest(source),
        "overview_sha256": file_digest(Path(str(source) + ".ovr"))
        if Path(str(source) + ".ovr").is_file()
        else None,
        "options": values,
    }


def reusable_tile(path, mesh, tile_size=65):
    try:
        # Bound decompression to reject corrupt/hostile resume artifacts.
        with gzip.open(path, "rb") as source:
            raw = source.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024:
            return False
        if mesh:
            if len(raw) < 92:
                return False
            count = struct.unpack_from("<I", raw, 88)[0]
            if (
                count < 3
                or count > tile_size * tile_size
                or not np.isfinite(struct.unpack_from("<3d2f4d", raw)).all()
            ):
                return False
            coordinates = (
                np.frombuffer(raw, "<u2", count * 3, 92).astype(np.int64).reshape(3, count)
            )
            coordinates = np.cumsum((coordinates >> 1) ^ -(coordinates & 1), axis=1)
            if np.any((coordinates < 0) | (coordinates > 32767)):
                return False
            size = 4 if count > 65536 else 2
            offset = ((92 + 6 * count + size - 1) // size) * size
            triangles = struct.unpack_from("<I", raw, offset)[0]
            offset += 4
            codes = np.frombuffer(raw, "<u4" if size == 4 else "<u2", triangles * 3, offset).astype(
                np.int64
            )
            indices = np.cumsum(codes == 0) - (codes == 0) - codes
            if triangles == 0 or np.any((indices < 0) | (indices >= count)):
                return False
            offset += triangles * 3 * size
            for _ in range(4):
                n = struct.unpack_from("<I", raw, offset)[0]
                offset += 4
                edge = np.frombuffer(raw, "<u4" if size == 4 else "<u2", n, offset)
                if np.any(edge >= count):
                    return False
                offset += n * size
            while offset < len(raw):
                kind, length = struct.unpack_from("<BI", raw, offset)
                offset += 5
                if offset + length > len(raw) or (kind == 1 and length != count * 2):
                    return False
                offset += length
            return offset == len(raw)
        return len(raw) == tile_size * tile_size * 2 + 2
    except (OSError, EOFError, ValueError, struct.error, zlib.error):
        return False


def read_signature(path):
    return json.loads(path.read_text(encoding="utf-8"))["signature"]
