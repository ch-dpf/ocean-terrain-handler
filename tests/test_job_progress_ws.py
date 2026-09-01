"""WebSocket job progress endpoint tests."""

import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import JobStatus


def _job_doc(job_id: str, *, status: str = "tiling", percent: float = 40.0) -> dict:
    return {
        "job_id": job_id,
        "status": status,
        "stage": "ctb_tile",
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:10+00:00",
        "progress": {
            "percent": percent,
            "phase": "ctb_tile",
            "message": "Tiling",
            "current_zoom": 12,
            "min_zoom": 0,
            "max_zoom": 14,
        },
        "published": False,
    }


def test_ws_sends_snapshot_then_closes_for_completed_job():
    store = MagicMock()
    store.get.return_value = _job_doc("done-1", status=JobStatus.COMPLETED.value, percent=100.0)

    with patch("app.api.routes._store", return_value=store):
        client = TestClient(app)
        with client.websocket_connect("/api/v1/terrain/jobs/done-1/ws") as ws:
            payload = ws.receive_json()
            assert payload["job_id"] == "done-1"
            assert payload["status"] == "completed"
            assert payload["progress"]["percent"] == 100.0


def test_ws_missing_job_closes_with_error_payload():
    store = MagicMock()
    store.get.return_value = None

    with (
        patch("app.api.routes._store", return_value=store),
        patch("app.api.routes.load_job_from_disk", return_value=None),
    ):
        client = TestClient(app)
        with client.websocket_connect("/api/v1/terrain/jobs/missing/ws") as ws:
            payload = ws.receive_json()
            assert payload["detail"] == "Job not found"


def test_ws_disk_fallback_sends_snapshot_and_closes():
    store = MagicMock()
    store.get.return_value = None
    disk_job = {
        "job_id": "disk-1",
        "status": JobStatus.COMPLETED.value,
        "stage": "done",
        "created_at": "2026-09-01T00:00:00+00:00",
        "completed_at": "2026-09-01T00:10:00+00:00",
        "progress": {"percent": 100.0, "phase": "done", "message": "Completed"},
        "published": False,
        "from_disk": True,
    }

    with (
        patch("app.api.routes._store", return_value=store),
        patch("app.api.routes.load_job_from_disk", return_value=disk_job),
    ):
        client = TestClient(app)
        with client.websocket_connect("/api/v1/terrain/jobs/disk-1/ws") as ws:
            payload = ws.receive_json()
            assert payload["job_id"] == "disk-1"
            assert payload["status"] == "completed"
            assert payload["progress"]["percent"] == 100.0
            assert payload["metadata"]["from_disk"] is True

    store.redis.pubsub.assert_not_called()


def test_get_job_falls_back_to_disk():
    store = MagicMock()
    store.get.return_value = None
    disk_job = {
        "job_id": "disk-2",
        "status": JobStatus.FAILED.value,
        "stage": "failed",
        "created_at": "2026-09-01T00:00:00+00:00",
        "completed_at": "2026-09-01T00:01:00+00:00",
        "error": "boom",
        "progress": {"percent": 0.0, "phase": "failed", "message": "boom"},
        "published": False,
        "from_disk": True,
    }

    with (
        patch("app.api.routes._store", return_value=store),
        patch("app.api.routes.load_job_from_disk", return_value=disk_job),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/terrain/jobs/disk-2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["error"] == "boom"
    assert payload["metadata"]["from_disk"] is True


def test_ws_forwards_pubsub_updates_until_terminal():
    store = MagicMock()
    store.get.return_value = _job_doc("run-1")
    store.events_channel.return_value = "terrain:job:events:run-1"

    pubsub = MagicMock()
    store.redis.pubsub.return_value = pubsub

    running = _job_doc("run-1", status="tiling", percent=55.0)
    completed = _job_doc("run-1", status=JobStatus.COMPLETED.value, percent=100.0)
    completed["stage"] = "done"
    completed["progress"] = {
        "percent": 100.0,
        "phase": "done",
        "message": "Done",
        "current_zoom": None,
        "min_zoom": 0,
        "max_zoom": 14,
    }

    pubsub.get_message.side_effect = [
        None,
        {"type": "message", "data": json.dumps(running)},
        {"type": "message", "data": json.dumps(completed)},
    ]

    with patch("app.api.routes._store", return_value=store):
        client = TestClient(app)
        with client.websocket_connect("/api/v1/terrain/jobs/run-1/ws") as ws:
            first = ws.receive_json()
            assert first["status"] == "tiling"
            assert first["progress"]["percent"] == 40.0

            second = ws.receive_json()
            assert second["progress"]["percent"] == 55.0

            third = ws.receive_json()
            assert third["status"] == "completed"
            assert third["progress"]["percent"] == 100.0

    pubsub.subscribe.assert_called_once_with("terrain:job:events:run-1")
    pubsub.unsubscribe.assert_called()
    pubsub.close.assert_called()
