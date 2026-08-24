"""Schema validation tests."""

import pytest
from pydantic import ValidationError

from app.schemas import CtbOptions, PreprocessOptions, TerrainJobCreate


def test_default_job_request():
    request = TerrainJobCreate(input_path="/data/workspace/dem.tif")
    assert request.preprocess.fill_nodata is True
    assert request.preprocess.block_size == 256
    assert request.ctb_options.cesium_friendly is True


def test_ctb_options_serialization():
    options = CtbOptions(start_zoom=18, end_zoom=0, resume=True)
    payload = options.model_dump()
    assert payload["start_zoom"] == 18
    assert payload["resume"] is True


def test_block_size_rejects_non_multiple_of_16():
    with pytest.raises(ValidationError):
        PreprocessOptions(block_size=65)
