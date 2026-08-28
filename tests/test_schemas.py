"""Schema validation tests."""

import pytest
from pydantic import ValidationError

from app.schemas import (
    CtbOptions,
    DiskPublishRequest,
    JobProgress,
    PreprocessOptions,
    TerrainJobCreate,
    TerrainJobDetail,
)


def test_default_job_request():
    request = TerrainJobCreate(input_path="/data/workspace/dem.tif")
    assert request.preprocess.fill_nodata is True
    assert request.preprocess.block_size == 256
    assert request.ctb_options.output_format.value == "Mesh"
    assert request.ctb_options.cesium_friendly is True
    assert request.ctb_options.vertex_normals is True


def test_ctb_options_serialization():
    options = CtbOptions(start_zoom=18, end_zoom=0, resume=True)
    payload = options.model_dump()
    assert payload["start_zoom"] == 18
    assert payload["resume"] is True


def test_block_size_rejects_non_multiple_of_16():
    with pytest.raises(ValidationError):
        PreprocessOptions(block_size=65)


def test_job_progress_and_detail():
    progress = JobProgress(percent=42.5, phase="ctb_tile", message="Generating tiles")
    detail = TerrainJobDetail(
        job_id="abc",
        status="tiling",
        progress=progress,
        stage="ctb_tile",
        created_at="2026-08-28T07:00:00+00:00",
        elapsed_seconds=12.5,
    )
    assert detail.progress is not None
    assert detail.progress.percent == 42.5
    assert detail.progress.phase == "ctb_tile"
    assert detail.elapsed_seconds == 12.5


def test_disk_publish_requires_job_or_tiles_dir():
    with pytest.raises(ValidationError):
        DiskPublishRequest()
    with pytest.raises(ValidationError):
        DiskPublishRequest(job_id="j1", tiles_dir="/data/x")


def test_disk_publish_job_id_ok():
    req = DiskPublishRequest(job_id="j1", tileset_name="coast")
    assert req.job_id == "j1"
    assert req.tileset_name == "coast"
