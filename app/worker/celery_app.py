"""Celery application."""

from celery import Celery

from app.config import get_settings
from app.services.ctb.mesh_encode import require_native

settings = get_settings()
require_native()

celery_app = Celery(
    "ocean_terrain_handler",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)
