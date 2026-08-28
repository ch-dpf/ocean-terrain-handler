"""Tile publishing tests."""

import os
from pathlib import Path

import pytest

from app.schemas import OutputFormat, Profile
from app.services.tile_publisher import (
    PublishError,
    infer_job_id_from_tiles_dir,
    list_published_tilesets,
    publish_from_disk,
    publish_tileset,
    resolve_job_tiles_dir,
    resolve_tiles_dir_path,
    unpublish_tileset,
)


def _make_tile(tiles_dir: Path, z: int, x: int, y: int) -> None:
    tile_path = tiles_dir / str(z) / str(x)
    tile_path.mkdir(parents=True, exist_ok=True)
    (tile_path / f"{y}.terrain").write_bytes(b"\x00")


@pytest.fixture
def tile_dirs(tmp_path: Path):
    tiles_dir = tmp_path / "jobs" / "job-1" / "tiles"
    tilesets_dir = tmp_path / "tilesets" / "terrain"
    _make_tile(tiles_dir, 0, 0, 0)
    return tiles_dir, tilesets_dir


def test_publish_tileset_creates_symlink_and_layer_json(tile_dirs):
    tiles_dir, tilesets_dir = tile_dirs
    if os.name == "nt":
        pytest.skip("Symlink creation may require elevated privileges on Windows")

    terrain_url, name = publish_tileset(
        job_id="job-1",
        tiles_dir=tiles_dir,
        tilesets_dir=tilesets_dir,
        public_url="http://localhost:8080",
        base_path="/tilesets",
        output_format=OutputFormat.MESH,
        profile=Profile.GEODETIC,
    )

    assert name == "job-1"
    assert terrain_url == "http://localhost:8080/tilesets/job-1"
    assert (tilesets_dir / "job-1").is_symlink()
    assert (tiles_dir / "layer.json").is_file()
    assert list_published_tilesets(tilesets_dir) == ["job-1"]


def test_publish_tileset_custom_name(tile_dirs):
    tiles_dir, tilesets_dir = tile_dirs
    if os.name == "nt":
        pytest.skip("Symlink creation may require elevated privileges on Windows")

    _, name = publish_tileset(
        job_id="job-1",
        tiles_dir=tiles_dir,
        tilesets_dir=tilesets_dir,
        public_url="http://localhost:8080",
        base_path="/tilesets",
        output_format=OutputFormat.TERRAIN,
        profile=Profile.GEODETIC,
        tileset_name="ocean-dem",
    )

    assert name == "ocean-dem"
    assert (tilesets_dir / "ocean-dem").exists()


def test_publish_tileset_rejects_invalid_name(tile_dirs):
    tiles_dir, tilesets_dir = tile_dirs
    with pytest.raises(PublishError):
        publish_tileset(
            job_id="job-1",
            tiles_dir=tiles_dir,
            tilesets_dir=tilesets_dir,
            public_url="http://localhost:8080",
            base_path="/tilesets",
            output_format=OutputFormat.TERRAIN,
            profile=Profile.GEODETIC,
            tileset_name="../evil",
        )


def test_publish_tileset_ignores_swagger_placeholder(tile_dirs):
    tiles_dir, tilesets_dir = tile_dirs
    if os.name == "nt":
        pytest.skip("Symlink creation may require elevated privileges on Windows")

    _, name = publish_tileset(
        job_id="job-1",
        tiles_dir=tiles_dir,
        tilesets_dir=tilesets_dir,
        public_url="http://localhost:8080",
        base_path="/tilesets",
        output_format=OutputFormat.TERRAIN,
        profile=Profile.GEODETIC,
        tileset_name="string",
    )

    assert name == "job-1"
    assert (tilesets_dir / "job-1").exists()


def test_unpublish_tileset(tile_dirs):
    tiles_dir, tilesets_dir = tile_dirs
    if os.name == "nt":
        pytest.skip("Symlink creation may require elevated privileges on Windows")

    publish_tileset(
        job_id="job-1",
        tiles_dir=tiles_dir,
        tilesets_dir=tilesets_dir,
        public_url="http://localhost:8080",
        base_path="/tilesets",
        output_format=OutputFormat.TERRAIN,
        profile=Profile.GEODETIC,
    )

    unpublish_tileset(tilesets_dir, "job-1")
    assert list_published_tilesets(tilesets_dir) == []


def test_unpublish_missing_raises(tile_dirs):
    _, tilesets_dir = tile_dirs
    with pytest.raises(PublishError):
        unpublish_tileset(tilesets_dir, "missing")


def test_infer_job_id_from_tiles_dir(tmp_path: Path):
    tiles_dir = tmp_path / "workspace" / "jobs" / "abc-123" / "tiles"
    tiles_dir.mkdir(parents=True)
    assert infer_job_id_from_tiles_dir(tiles_dir) == "abc-123"


def test_resolve_job_tiles_dir(tmp_path: Path):
    jobs_dir = tmp_path / "jobs"
    tiles_dir = jobs_dir / "job-9" / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)
    assert resolve_job_tiles_dir(jobs_dir, "job-9") == tiles_dir.resolve()


def test_resolve_tiles_dir_path_rejects_outside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside" / "tiles"
    outside.mkdir(parents=True)

    with pytest.raises(PublishError, match="outside"):
        resolve_tiles_dir_path(
            str(outside),
            workspace_dir=workspace,
            jobs_dir=workspace / "jobs",
        )


def test_publish_from_disk_by_job_id(tmp_path: Path):
    if os.name == "nt":
        pytest.skip("Symlink creation may require elevated privileges on Windows")

    workspace = tmp_path / "workspace"
    jobs_dir = workspace / "jobs"
    tilesets_dir = workspace / "tilesets" / "terrain"
    tiles_dir = jobs_dir / "old-job" / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)

    terrain_url, name, resolved_dir = publish_from_disk(
        jobs_dir=jobs_dir,
        workspace_dir=workspace,
        tilesets_dir=tilesets_dir,
        public_url="http://localhost:8103",
        base_path="/tilesets",
        job_id="old-job",
        tileset_name="coast-dem",
    )

    assert name == "coast-dem"
    assert terrain_url == "http://localhost:8103/tilesets/coast-dem"
    assert resolved_dir == tiles_dir.resolve()
    assert (tilesets_dir / "coast-dem").is_symlink()
    assert (tiles_dir / "layer.json").is_file()
