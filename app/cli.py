"""Platform-neutral bare-metal process entry points."""

from __future__ import annotations

import sys

import uvicorn

from app.config import get_settings
from app.services.ctb.mesh_encode import require_native


def api_main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


def worker_main() -> None:
    require_native()
    settings = get_settings()
    from app.worker.celery_app import celery_app

    pool = settings.celery_worker_pool
    concurrency = max(1, int(settings.celery_worker_concurrency))
    if sys.platform == "win32" and pool is None:
        # Celery prefork is unavailable on native Windows. Terrain tasks retain
        # their internal C++/thread parallelism under the solo task pool.
        pool = "solo"
        concurrency = 1
    argv = [
        "worker",
        "--loglevel=info",
        f"--concurrency={concurrency}",
    ]
    if pool:
        argv.append(f"--pool={pool}")
    celery_app.worker_main(argv)
