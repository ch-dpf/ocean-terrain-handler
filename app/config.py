"""Application configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = False

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    workspace_dir: Path = Path("/data/workspace")
    # Host path for docker -v when worker calls CTB via docker.sock (DinD-style).
    # Must be the host-side path that maps to workspace_dir (e.g. D:/.../data).
    # Falls back to workspace_dir for local (non-container) workers.
    host_workspace_dir: Path | None = None
    ctb_docker_image: str = "cesium-terrain-builder:local"
    gdal_cachemax: int = 512
    job_ttl: int = 604800
    progress_calibration_min_samples: int = 3
    progress_calibration_ema_alpha: float = 0.25

    terrain_server_public_url: str = "http://localhost:8103"
    terrain_base_path: str = "/tilesets"
    auto_publish: bool = False

    @property
    def jobs_dir(self) -> Path:
        return self.workspace_dir / "jobs"

    @property
    def uploads_dir(self) -> Path:
        return self.workspace_dir / "uploads"

    @property
    def tilesets_dir(self) -> Path:
        return self.workspace_dir / "tilesets" / "terrain"

    @property
    def ctb_volume_source(self) -> Path:
        return self.host_workspace_dir or self.workspace_dir

    def terrain_url_for(self, tileset_name: str) -> str:
        base = self.terrain_server_public_url.rstrip("/")
        path = self.terrain_base_path.rstrip("/")
        return f"{base}{path}/{tileset_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
