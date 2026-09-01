"""JobStore Redis pub/sub progress event tests."""

import json
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.services.job_store import JobStore


def _settings() -> Settings:
    return Settings(
        redis_url="redis://localhost:6379/15",
        job_ttl=3600,
        workspace_dir="/tmp/ocean-terrain-test",
    )


def test_events_channel_name():
    with patch("app.services.job_store.redis.from_url") as from_url:
        from_url.return_value = MagicMock()
        store = JobStore(_settings())
    assert store.events_channel("abc-1") == "terrain:job:events:abc-1"


def test_create_publishes_event():
    client = MagicMock()
    with patch("app.services.job_store.redis.from_url", return_value=client):
        store = JobStore(_settings())
        store.create("job-1", {"input_path": "/data/a.tif"})

    client.setex.assert_called_once()
    client.publish.assert_called_once()
    channel, payload = client.publish.call_args[0]
    assert channel == "terrain:job:events:job-1"
    data = json.loads(payload)
    assert data["job_id"] == "job-1"
    assert data["status"] == "queued"
    assert data["progress"]["percent"] == 0.0
    assert data["input_path"] == "/data/a.tif"


def test_update_publishes_merged_document():
    existing = {
        "job_id": "job-2",
        "status": "queued",
        "stage": "queued",
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
        "progress": {"percent": 0.0, "phase": "queued", "message": "Queued"},
    }
    client = MagicMock()
    client.get.return_value = json.dumps(existing)
    with patch("app.services.job_store.redis.from_url", return_value=client):
        store = JobStore(_settings())
        store.update(
            "job-2",
            status="tiling",
            stage="ctb_tile",
            progress={"percent": 40.0, "phase": "ctb_tile", "message": "Tiling"},
        )

    client.publish.assert_called_once()
    channel, payload = client.publish.call_args[0]
    assert channel == "terrain:job:events:job-2"
    data = json.loads(payload)
    assert data["status"] == "tiling"
    assert data["progress"]["percent"] == 40.0
    assert "updated_at" in data


def test_update_missing_job_skips_publish():
    client = MagicMock()
    client.get.return_value = None
    with patch("app.services.job_store.redis.from_url", return_value=client):
        store = JobStore(_settings())
        store.update("missing", status="running")

    client.setex.assert_not_called()
    client.publish.assert_not_called()
