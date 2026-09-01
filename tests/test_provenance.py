"""Provenance / lineage JSON tests."""

from pathlib import Path

from app.schemas import TerrainJobCreate
from app.services.provenance import (
    MANIFEST_FILENAME,
    PROVENANCE_FILENAME,
    build_source_info,
    file_sha256,
    read_json,
    update_manifest,
    write_manifest_completed,
    write_manifest_created,
    write_manifest_failed,
    write_tiles_provenance,
)


def test_write_manifest_created_and_completed(tmp_path: Path):
    job_dir = tmp_path / "jobs" / "job-abc"
    source = tmp_path / "dem.tif"
    source.write_bytes(b"abc123")
    output_dir = job_dir / "tiles"
    request = TerrainJobCreate(input_path=str(source))

    path = write_manifest_created(
        job_dir,
        job_id="job-abc",
        input_path=source,
        output_dir=output_dir,
        request=request,
    )
    assert path == job_dir / MANIFEST_FILENAME
    data = read_json(path)
    assert data is not None
    assert data["job_id"] == "job-abc"
    assert data["status"] == "queued"
    assert data["source"]["input_path"] == str(source)
    assert data["source"]["size_bytes"] == 6
    assert data["source"]["sha256"] is None
    assert data["request"]["input_path"] == str(source)

    update_manifest(
        job_dir,
        status="running",
        source=build_source_info(source, compute_hash=True),
    )
    mid = read_json(path)
    assert mid is not None
    assert mid["status"] == "running"
    assert mid["source"]["sha256"] == file_sha256(source)

    write_manifest_completed(job_dir, output_dir=output_dir, published=False)
    done = read_json(path)
    assert done is not None
    assert done["status"] == "completed"
    assert done["publish"]["published"] is False
    assert done["completed_at"] is not None


def test_write_manifest_failed(tmp_path: Path):
    job_dir = tmp_path / "jobs" / "job-fail"
    write_manifest_created(
        job_dir,
        job_id="job-fail",
        input_path=None,
        output_dir=job_dir / "tiles",
    )
    write_manifest_failed(job_dir, error="boom")
    data = read_json(job_dir / MANIFEST_FILENAME)
    assert data is not None
    assert data["status"] == "failed"
    assert data["error"] == "boom"


def test_write_tiles_provenance_reads_manifest(tmp_path: Path):
    job_dir = tmp_path / "jobs" / "job-1"
    tiles_dir = job_dir / "tiles"
    tiles_dir.mkdir(parents=True)
    source = tmp_path / "src.tif"
    source.write_bytes(b"dem-bytes")

    write_manifest_created(
        job_dir,
        job_id="job-1",
        input_path=source,
        output_dir=tiles_dir,
        request=TerrainJobCreate(input_path=str(source)),
    )
    update_manifest(job_dir, source=build_source_info(source, compute_hash=True))

    prov_path = write_tiles_provenance(
        tiles_dir,
        job_id="job-1",
        tileset_name="ocean-dem",
        terrain_url="http://localhost/tilesets/ocean-dem",
        job_dir=job_dir,
    )
    assert prov_path == tiles_dir / PROVENANCE_FILENAME
    prov = read_json(prov_path)
    assert prov is not None
    assert prov["job_id"] == "job-1"
    assert prov["tileset_name"] == "ocean-dem"
    assert prov["source"]["sha256"] == file_sha256(source)
    assert prov["request"]["input_path"] == str(source)

    manifest = read_json(job_dir / MANIFEST_FILENAME)
    assert manifest is not None
    assert manifest["publish"]["published"] is True
    assert manifest["publish"]["tileset_name"] == "ocean-dem"


def test_load_job_from_disk_uses_manifest(tmp_path: Path):
    from app.services.provenance import load_job_from_disk

    jobs_dir = tmp_path / "jobs"
    job_dir = jobs_dir / "job-disk"
    tiles_dir = job_dir / "tiles"
    source = tmp_path / "dem.tif"
    source.write_bytes(b"xyz")

    write_manifest_created(
        job_dir,
        job_id="job-disk",
        input_path=source,
        output_dir=tiles_dir,
        request=TerrainJobCreate(input_path=str(source)),
    )
    write_manifest_completed(job_dir, output_dir=tiles_dir, published=False)

    data = load_job_from_disk(jobs_dir, "job-disk")
    assert data is not None
    assert data["job_id"] == "job-disk"
    assert data["status"] == "completed"
    assert data["stage"] == "done"
    assert data["progress"]["percent"] == 100.0
    assert data["from_disk"] is True
    assert data["input_path"] == str(source)
    assert data["published"] is False


def test_load_job_from_disk_tiles_only_fallback(tmp_path: Path):
    from app.services.provenance import load_job_from_disk

    jobs_dir = tmp_path / "jobs"
    tiles_dir = jobs_dir / "legacy-job" / "tiles"
    tiles_dir.mkdir(parents=True)
    (tiles_dir / "layer.json").write_text("{}", encoding="utf-8")

    data = load_job_from_disk(jobs_dir, "legacy-job")
    assert data is not None
    assert data["status"] == "completed"
    assert data["output_dir"] == str(tiles_dir)
    assert data["from_disk"] is True


def test_load_job_from_disk_missing_returns_none(tmp_path: Path):
    from app.services.provenance import load_job_from_disk

    assert load_job_from_disk(tmp_path / "jobs", "no-such") is None
