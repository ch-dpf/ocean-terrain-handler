"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.tilesets_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="海洋地形处理服务",
    description="地形 TIF 预处理与 Cesium 地形瓦片切片服务",
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "地形",
            "description": "任务提交、进度查询、瓦片发布/下架与工作区浏览",
        },
        {
            "name": "系统",
            "description": "健康检查等系统接口",
        },
    ],
)
app.include_router(router)


@app.get("/health", tags=["系统"], summary="健康检查")
async def health() -> dict[str, str]:
    """返回服务健康状态。"""
    return {"status": "ok"}
