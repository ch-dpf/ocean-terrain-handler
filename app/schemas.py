"""Pydantic request/response models."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PREPROCESSING = "preprocessing"
    TILING = "tiling"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"


class ResamplingMethod(str, Enum):
    NEAREST = "nearest"
    BILINEAR = "bilinear"
    CUBIC = "cubic"
    CUBICSPLINE = "cubicspline"
    LANCZOS = "lanczos"
    AVERAGE = "average"
    MODE = "mode"
    MAX = "max"
    MIN = "min"
    MED = "med"
    Q1 = "q1"
    Q3 = "q3"


class OutputFormat(str, Enum):
    TERRAIN = "Terrain"
    MESH = "Mesh"


class Profile(str, Enum):
    GEODETIC = "geodetic"
    MERCATOR = "mercator"


class PreprocessOptions(BaseModel):
    target_crs: str = Field(default="EPSG:4326", description="Target CRS for gdal raster reproject")
    fill_nodata: bool = Field(default=True, description="Fill NODATA before tiling")
    build_overviews: bool = Field(default=True, description="Build overviews with gdal raster overview add")
    # GeoTIFF TileWidth/TileHeight must be multiples of 16 (not CTB's 65px mesh size).
    block_size: int = Field(
        default=256,
        ge=16,
        description="GDAL TIFF BLOCKXSIZE/BLOCKYSIZE; must be a multiple of 16",
    )
    nodata_value: float | None = Field(default=None, description="Override NODATA value")

    @field_validator("block_size")
    @classmethod
    def block_size_multiple_of_16(cls, value: int) -> int:
        if value % 16 != 0:
            raise ValueError("block_size must be a multiple of 16 (GeoTIFF TileWidth requirement)")
        return value


class CtbOptions(BaseModel):
    output_format: OutputFormat = OutputFormat.MESH
    profile: Profile = Profile.GEODETIC
    thread_count: int | None = Field(default=None, ge=1)
    tile_size: int | None = Field(default=None, ge=1)
    start_zoom: int | None = Field(default=None, ge=0)
    end_zoom: int | None = Field(default=0, ge=0)
    resampling_method: ResamplingMethod = ResamplingMethod.AVERAGE
    error_threshold: float = Field(default=0.125, gt=0)
    warp_memory: int | None = Field(default=None, ge=0, description="Bytes for warp memory")
    resume: bool = False
    mesh_qfactor: float = Field(default=1.0, gt=0)
    layer_only: bool = Field(default=False, description="Only output layer.json")
    cesium_friendly: bool = True
    # Applies only when output_format is Mesh; ignored for Terrain heightmap.
    vertex_normals: bool = True
    quiet: bool = False
    verbose: bool = False
    creation_options: list[str] = Field(default_factory=list)


class PublishOptions(BaseModel):
    auto_publish: bool | None = Field(
        default=None,
        description="Publish tileset when job completes; defaults to AUTO_PUBLISH setting",
    )
    tileset_name: str | None = Field(
        default=None,
        description="Published tileset name; defaults to job_id",
    )


class TerrainJobCreate(BaseModel):
    input_path: str | None = Field(
        default=None,
        description="Absolute path to input TIF inside workspace (mutually exclusive with upload)",
    )
    preprocess: PreprocessOptions = Field(default_factory=PreprocessOptions)
    ctb_options: CtbOptions = Field(default_factory=CtbOptions)
    publish: PublishOptions = Field(default_factory=PublishOptions)

    @field_validator("input_path")
    @classmethod
    def strip_input_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        return stripped or None


class TerrainJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress_url: str
    message: str | None = None


class JobProgress(BaseModel):
    percent: float = Field(ge=0, le=100, description="Overall job completion 0-100")
    phase: str | None = Field(default=None, description="Current pipeline stage identifier")
    message: str | None = Field(default=None, description="Human-readable progress detail")
    current_zoom: int | None = Field(default=None, description="Zoom level currently being generated")
    min_zoom: int | None = Field(default=None, description="Minimum output zoom level")
    max_zoom: int | None = Field(default=None, description="Maximum output zoom level")
    weight_source: str | None = Field(
        default=None,
        description="Stage weight source: default (fixed) or historical (calibrated from past jobs)",
    )
    calibration_samples: int | None = Field(
        default=None,
        description="Number of completed jobs used for historical calibration, if applicable",
    )


class TerrainJobDetail(BaseModel):
    job_id: str
    status: JobStatus
    progress: JobProgress | None = None
    stage: str | None = None
    created_at: str | None = Field(
        default=None,
        description="UTC ISO-8601 timestamp when the job was created",
    )
    completed_at: str | None = Field(
        default=None,
        description="UTC ISO-8601 timestamp when the job finished (completed or failed)",
    )
    elapsed_seconds: float | None = Field(
        default=None,
        ge=0,
        description="Wall-clock seconds from created_at to completed_at (or now if still running)",
    )
    input_path: str | None = None
    output_dir: str | None = None
    terrain_url: str | None = None
    tileset_name: str | None = None
    published: bool = False
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TilesetInfo(BaseModel):
    name: str
    terrain_url: str
    format: str | None = Field(
        default=None,
        description="Terrain tile format from layer.json (e.g. quantized-mesh-1.0)",
    )
    format_label: str | None = Field(
        default=None,
        description="Human-readable format label",
    )
    projection: str | None = Field(
        default=None,
        description="Projection code from layer.json (e.g. EPSG:4326)",
    )
    crs: str | None = Field(
        default=None,
        description="Human-readable coordinate system label",
    )
    min_zoom: int | None = Field(default=None, description="Minimum zoom with available tiles")
    max_zoom: int | None = Field(default=None, description="Maximum zoom with available tiles")


class TilesetListResponse(BaseModel):
    tilesets: list[TilesetInfo]


class DiskPublishRequest(BaseModel):
    """Publish tiles from disk without requiring Redis job metadata."""

    job_id: str | None = Field(
        default=None,
        description="Publish jobs/{job_id}/tiles/ (mutually exclusive with tiles_dir)",
    )
    tiles_dir: str | None = Field(
        default=None,
        description="Absolute or workspace-relative tiles directory (mutually exclusive with job_id)",
    )
    tileset_name: str | None = Field(
        default=None,
        description="Published tileset name; defaults to job_id",
    )
    output_format: OutputFormat | None = Field(
        default=None,
        description="Override format when layer.json is missing; defaults to layer.json or Mesh",
    )
    profile: Profile | None = Field(
        default=None,
        description="Override profile when layer.json is missing; defaults to layer.json or geodetic",
    )

    @field_validator("job_id", "tiles_dir", "tileset_name")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def validate_source_fields(self) -> "DiskPublishRequest":
        if self.job_id and self.tiles_dir:
            raise ValueError("Provide either job_id or tiles_dir, not both")
        if not self.job_id and not self.tiles_dir:
            raise ValueError("Either job_id or tiles_dir is required")
        if self.tiles_dir and not self.tileset_name and not self.job_id:
            # job_id is None here; require an explicit published name unless path implies job
            pass
        return self


class WorkspaceEntryInfo(BaseModel):
    name: str
    relative_path: str
    absolute_path: str
    entry_type: str
    size_bytes: int | None = None
    selectable: bool


class WorkspaceListResponse(BaseModel):
    relative_path: str
    absolute_path: str
    parent_relative_path: str | None = None
    entries: list[WorkspaceEntryInfo]
