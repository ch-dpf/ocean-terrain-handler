"""Redis-backed job status store with pub/sub progress events."""

import json
from datetime import UTC, datetime
from typing import Any

import redis

from app.config import Settings
from app.schemas import JobStatus


class JobStore:
    def __init__(self, settings: Settings) -> None:
        self._redis = redis.from_url(settings.redis_url, decode_responses=True)
        self._ttl = settings.job_ttl
        self._prefix = "terrain:job:"
        self._events_prefix = "terrain:job:events:"

    def _key(self, job_id: str) -> str:
        return f"{self._prefix}{job_id}"

    def events_channel(self, job_id: str) -> str:
        """Redis pub/sub channel for live job progress updates."""
        return f"{self._events_prefix}{job_id}"

    def _publish(self, job_id: str, data: dict[str, Any]) -> None:
        self._redis.publish(self.events_channel(job_id), json.dumps(data))

    def create(self, job_id: str, payload: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        data = {
            "job_id": job_id,
            "status": JobStatus.QUEUED.value,
            "stage": "queued",
            "created_at": now,
            "updated_at": now,
            "progress": {
                "percent": 0.0,
                "phase": "queued",
                "message": "Queued",
                "current_zoom": None,
                "min_zoom": None,
                "max_zoom": None,
                "weight_source": "bytes",
                "bytes_done": 0,
                "bytes_planned": 0,
            },
            **payload,
        }
        self._redis.setex(self._key(job_id), self._ttl, json.dumps(data))
        self._publish(job_id, data)

    def update(self, job_id: str, **fields: Any) -> None:
        data = self.get(job_id)
        if data is None:
            return
        data.update(fields)
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._redis.setex(self._key(job_id), self._ttl, json.dumps(data))
        self._publish(job_id, data)

    def get(self, job_id: str) -> dict[str, Any] | None:
        raw = self._redis.get(self._key(job_id))
        if raw is None:
            return None
        return json.loads(raw)

    @property
    def redis(self):
        """Expose Redis client for pub/sub."""
        return self._redis
