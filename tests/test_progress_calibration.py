"""Progress calibration helper tests."""

from app.services.progress_calibration import (
    DEFAULT_WEIGHTS_WITH_PUBLISH,
    build_stage_ranges,
    durations_to_ratios,
    merge_weight_ema,
    normalize_weights,
)


def test_build_stage_ranges_sums_to_100():
    ranges = build_stage_ranges(DEFAULT_WEIGHTS_WITH_PUBLISH)
    assert ranges["queued"] == (0.0, 0.0)
    assert ranges["done"] == (100.0, 100.0)
    assert ranges["initializing"][0] == 0.0
    assert ranges["register_tileset"][1] == 100.0


def test_durations_to_ratios():
    ratios = durations_to_ratios(
        {"initializing": 1.0, "gdal_preprocess": 9.0, "ctb_tile": 90.0},
        ("initializing", "gdal_preprocess", "ctb_tile"),
    )
    assert abs(ratios["ctb_tile"] - 0.9) < 1e-9


def test_merge_weight_ema():
    previous = normalize_weights({"a": 0.5, "b": 0.5})
    observed = {"a": 0.2, "b": 0.8}
    merged = merge_weight_ema(previous, observed, alpha=0.5)
    assert abs(sum(merged.values()) - 1.0) < 1e-9
    assert merged["b"] > merged["a"]
