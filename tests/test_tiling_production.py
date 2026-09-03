"""Production contracts independent of CTB's historical failure behavior."""

import gzip
import json
import struct
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import numpy as np
import pytest
from pyproj import Transformer

from app.schemas import CtbOptions, Profile
from app.services.ctb.encode import encode_heightmap
from app.services.ctb.mesh_encode import encode_heightmap_tile_bytes, encode_mesh_tile_bytes
from app.services.ctb.sample import extend_outer_support
from app.services.ctb.sample_cache import SampleCache
from app.services.ctb.tiler import CtbError, run_ctb_tile
from tests.raster_fixtures import write_dem_geotiff_4326


def test_cache_single_flight_immutable_and_eviction():
    cache = SampleCache(16)
    started, finish = Event(), Event()
    calls = []

    def compute():
        calls.append(1)
        started.set()
        assert finish.wait(5)
        return np.ones((2, 2), dtype="float32")

    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(cache.get, "a", compute)
        assert started.wait(5)
        two = pool.submit(cache.get, "a", compute)
        finish.set()
        a, b = one.result(), two.result()
    assert a is b and calls == [1]
    assert not a.flags.writeable
    cache.get("b", lambda: np.zeros((2, 2), dtype="float32"))
    assert cache._bytes <= 16 and "a" not in cache._ready


def test_cache_failure_allows_retry():
    cache = SampleCache(16)

    def fail():
        raise ValueError("failure")

    with pytest.raises(ValueError, match="failure"):
        cache.get("a", fail)
    assert cache.get("a", lambda: np.array([3]))[0] == 3
    assert not cache._pending


def test_outer_support_preserves_deep_ocean_and_interior_holes():
    a = np.full((5, 5), -32768, dtype="float32")
    a[1:4, 1:4] = -5000
    a[2, 2] = -32768
    inside = np.zeros((5, 5), dtype=bool)
    inside[1:4, 1:4] = True
    actual = extend_outer_support(a, inside, -32768)
    assert actual[0, 0] == -5000 and actual[-1, -1] == -5000
    assert actual[2, 2] == -32768
    np.testing.assert_array_equal(actual[inside], a[inside])
    assert a[0, 0] == -32768


@pytest.mark.parametrize("value", [-1000.25, 12107.25, np.nan, np.inf])
@pytest.mark.parametrize("encoder", [encode_heightmap, encode_heightmap_tile_bytes])
def test_heightmap_rejects_unrepresentable_values(value, encoder):
    with pytest.raises((ValueError, RuntimeError), match="use Mesh"):
        encoder(np.full((3, 3), value, dtype="float32"), 0)


def test_heightmap_endpoints_match_native():
    a = np.array([[-1000, 0, 12107]], dtype="float32")
    assert gzip.decompress(encode_heightmap(a, 0)) == gzip.decompress(
        encode_heightmap_tile_bytes(a, 0)
    )
    np.testing.assert_array_equal(
        np.frombuffer(gzip.decompress(encode_heightmap(a, 0))[:6], "<u2"), [0, 5000, 65535]
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"creation_options": ["COMPRESS=LZW"]}, {"warp_memory": 123}, {"error_threshold": 0.5}],
)
def test_unsupported_overrides_fail_before_creating_output(tmp_path, kwargs):
    out = tmp_path / "tiles"
    with pytest.raises(CtbError, match="not supported"):
        run_ctb_tile(tmp_path / "missing.tif", out, CtbOptions(**kwargs))
    assert not out.exists()


def test_mercator_layer_bounds_are_degrees(tmp_path):
    source = write_dem_geotiff_4326(
        tmp_path / "dem.tif", width=32, height=32, west=116, north=40, pixel_deg=0.01
    )
    out = tmp_path / "tiles"
    run_ctb_tile(
        source, out, CtbOptions(profile=Profile.MERCATOR, start_zoom=6, end_zoom=6, layer_only=True)
    )
    layer = json.loads((out / "layer.json").read_text())
    assert layer["projection"] == "EPSG:3857"
    assert layer["bounds"] == pytest.approx([116, 39.68, 116.32, 40], abs=1e-7)


def test_mercator_mesh_ecef_header_uses_inverse_projection():
    heights = np.full((65, 65), -2000, dtype="float32")
    heights.flags.writeable = False
    raw = gzip.decompress(
        encode_mesh_tile_bytes(
            heights, 13000000, 3000000, 13002000, 3002000, 1, False, None, True, web_mercator=True
        )
    )
    center = np.array(struct.unpack_from("<3d", raw, 32))
    radius = struct.unpack_from("<d", raw, 56)[0]
    expected = Transformer.from_crs(3857, 4978, always_xy=True).transform(13001000, 3001000, -2000)
    assert np.linalg.norm(center - expected) < 20
    assert 0 < radius < 3000


def test_resume_repairs_corruption_and_rejects_changed_options(tmp_path):
    source = write_dem_geotiff_4326(
        tmp_path / "dem.tif", width=32, height=32, west=116, north=40, pixel_deg=0.01
    )
    out = tmp_path / "tiles"
    options = CtbOptions(start_zoom=7, end_zoom=7, thread_count=2)
    run_ctb_tile(source, out, options)
    paths = sorted(out.rglob("*.terrain"))
    expected = {p: p.read_bytes() for p in paths}
    paths[0].write_bytes(b"broken")
    options.resume = True
    run_ctb_tile(source, out, options)
    assert {p: p.read_bytes() for p in paths} == expected
    assert json.loads((out / ".tiling-state.json").read_text())["status"] == "complete"
    options.mesh_qfactor = 2
    with pytest.raises(CtbError, match="changed"):
        run_ctb_tile(source, out, options)


def test_failed_write_does_not_leave_published_metadata(tmp_path, monkeypatch):
    from app.services.ctb import tiler

    source = write_dem_geotiff_4326(
        tmp_path / "dem.tif", width=32, height=32, west=116, north=40, pixel_deg=0.01
    )
    out = tmp_path / "tiles"
    original = tiler._atomic_write

    def fail(path, payload):
        if path.suffix == ".terrain":
            raise OSError("disk full")
        return original(path, payload)

    monkeypatch.setattr(tiler, "_atomic_write", fail)
    with pytest.raises(CtbError, match="disk full"):
        run_ctb_tile(source, out, CtbOptions(start_zoom=7, end_zoom=7))
    assert not (out / "layer.json").exists()
    assert json.loads((out / ".tiling-state.json").read_text())["status"] == "building"
