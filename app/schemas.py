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
    """预处理选项。"""

    target_crs: str = Field(default="EPSG:4326", description="重投影目标坐标系")
    fill_nodata: bool = Field(default=True, description="切片前填充 NODATA")
    build_overviews: bool = Field(default=True, description="构建 GeoTIFF 低分辨率概览图（overview）")
    # GeoTIFF TileWidth/TileHeight must be multiples of 16 (not CTB's 65px mesh size).
    block_size: int = Field(
        default=256,
        ge=16,
        description="TIFF BLOCKXSIZE/BLOCKYSIZE；须为 16 的倍数",
    )
    nodata_value: float | None = Field(default=None, description="覆盖 NODATA 值")

    @field_validator("block_size")
    @classmethod
    def block_size_multiple_of_16(cls, value: int) -> int:
        if value % 16 != 0:
            raise ValueError("block_size must be a multiple of 16 (GeoTIFF TileWidth requirement)")
        return value


class CtbOptions(BaseModel):
    """CTB（cesium-terrain-builder）切片选项。"""

    output_format: OutputFormat = OutputFormat.MESH
    profile: Profile = Profile.GEODETIC
    thread_count: int | None = Field(default=None, ge=1, description="线程数")
    tile_size: int | None = Field(default=None, ge=1, description="瓦片尺寸")
    start_zoom: int | None = Field(default=None, ge=0, description="起始缩放级别")
    end_zoom: int | None = Field(default=0, ge=0, description="结束缩放级别")
    resampling_method: ResamplingMethod = ResamplingMethod.AVERAGE
    error_threshold: float = Field(
        default=0.125,
        gt=0,
        description="CTB/GDAL 近似变换误差（像素）；Python warp 为精确反算，该字段仅保留选项兼容",
    )
    warp_memory: int | None = Field(
        default=None,
        ge=0,
        description="CTB/GDAL warp 内存上限（字节）；Python 切片忽略",
    )
    resume: bool = Field(default=False, description="断点续跑")
    mesh_qfactor: float = Field(default=1.0, gt=0, description="网格质量因子")
    layer_only: bool = Field(default=False, description="仅输出 layer.json")
    cesium_friendly: bool = Field(default=True, description="生成 Cesium 友好的 layer.json")
    # Applies only when output_format is Mesh; ignored for Terrain heightmap.
    vertex_normals: bool = Field(default=True, description="输出顶点法线（仅 Mesh 格式）")
    quiet: bool = Field(default=False, description="静默输出")
    verbose: bool = Field(default=False, description="详细日志")
    creation_options: list[str] = Field(default_factory=list, description="额外创建选项")


class PublishOptions(BaseModel):
    """任务完成后的发布选项。"""

    auto_publish: bool | None = Field(
        default=None,
        description="任务完成后自动发布；默认跟随 AUTO_PUBLISH 配置",
    )
    tileset_name: str | None = Field(
        default=None,
        description="发布名称；默认使用 job_id",
    )


class TerrainJobCreate(BaseModel):
    """创建地形处理任务的请求体。"""

    input_path: str | None = Field(
        default=None,
        description="工作区内输入 TIF 的绝对路径（与上传互斥）",
    )
    preprocess: PreprocessOptions = Field(default_factory=PreprocessOptions, description="预处理选项")
    ctb_options: CtbOptions = Field(default_factory=CtbOptions, description="CTB 切片选项")
    publish: PublishOptions = Field(default_factory=PublishOptions, description="发布选项")

    @field_validator("input_path")
    @classmethod
    def strip_input_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        return stripped or None


class TerrainJobResponse(BaseModel):
    """任务创建响应。"""

    job_id: str = Field(description="任务 ID")
    status: JobStatus = Field(description="任务状态")
    progress_url: str = Field(description="进度查询 URL（REST 快照）")
    progress_ws_url: str = Field(description="进度推送 WebSocket URL")
    message: str | None = Field(default=None, description="提示信息")


class JobProgress(BaseModel):
    """任务进度。"""

    percent: float = Field(ge=0, le=100, description="整体完成百分比 0–100")
    phase: str | None = Field(default=None, description="当前流水线阶段标识")
    message: str | None = Field(default=None, description="可读进度说明")
    current_zoom: int | None = Field(default=None, description="正在生成的缩放级别")
    min_zoom: int | None = Field(default=None, description="输出最小缩放级别")
    max_zoom: int | None = Field(default=None, description="输出最大缩放级别")
    weight_source: str | None = Field(
        default="bytes",
        description="进度计量：已写入/计划的未压缩栅格字节数",
    )
    bytes_done: int | None = Field(default=None, ge=0, description="已完成的未压缩栅格字节数")
    bytes_planned: int | None = Field(default=None, ge=0, description="任务计划中的未压缩栅格字节数")
    calibration_samples: int | None = Field(
        default=None,
        description="兼容字段；字节进度不使用历史校准",
    )


class TerrainJobDetail(BaseModel):
    """任务详情。"""

    job_id: str = Field(description="任务 ID")
    status: JobStatus = Field(description="任务状态")
    progress: JobProgress | None = Field(default=None, description="进度信息")
    stage: str | None = Field(default=None, description="当前阶段")
    created_at: str | None = Field(
        default=None,
        description="任务创建时间（UTC ISO-8601）",
    )
    completed_at: str | None = Field(
        default=None,
        description="任务结束时间（完成或失败，UTC ISO-8601）",
    )
    elapsed_seconds: float | None = Field(
        default=None,
        ge=0,
        description="从创建到完成（或当前）的耗时秒数",
    )
    input_path: str | None = Field(default=None, description="输入文件路径")
    output_dir: str | None = Field(default=None, description="输出瓦片目录")
    terrain_url: str | None = Field(default=None, description="已发布地形访问 URL")
    tileset_name: str | None = Field(default=None, description="已发布 tileset 名称")
    published: bool = Field(default=False, description="是否已发布")
    error: str | None = Field(default=None, description="失败错误信息")
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加元数据")


class TilesetInfo(BaseModel):
    """已发布 tileset 信息。"""

    name: str = Field(description="tileset 名称")
    terrain_url: str = Field(description="地形访问 URL")
    format: str | None = Field(
        default=None,
        description="layer.json 中的地形瓦片格式（如 quantized-mesh-1.0）",
    )
    format_label: str | None = Field(
        default=None,
        description="格式可读标签",
    )
    projection: str | None = Field(
        default=None,
        description="layer.json 中的投影代码（如 EPSG:4326）",
    )
    crs: str | None = Field(
        default=None,
        description="坐标系可读标签",
    )
    min_zoom: int | None = Field(default=None, description="有瓦片的最小缩放级别")
    max_zoom: int | None = Field(default=None, description="有瓦片的最大缩放级别")


class TilesetListResponse(BaseModel):
    """已发布 tileset 列表。"""

    tilesets: list[TilesetInfo] = Field(description="tileset 列表")


class DiskPublishRequest(BaseModel):
    """从磁盘发布瓦片（不依赖 Redis 任务元数据）。"""

    job_id: str | None = Field(
        default=None,
        description="发布 jobs/{job_id}/tiles/（与 tiles_dir 互斥）",
    )
    tiles_dir: str | None = Field(
        default=None,
        description="瓦片目录的绝对路径或相对工作区路径（与 job_id 互斥）",
    )
    tileset_name: str | None = Field(
        default=None,
        description="发布名称；默认使用 job_id",
    )
    output_format: OutputFormat | None = Field(
        default=None,
        description="缺少 layer.json 时覆盖格式；默认取自 layer.json 或 Mesh",
    )
    profile: Profile | None = Field(
        default=None,
        description="缺少 layer.json 时覆盖剖分剖面；默认取自 layer.json 或 geodetic",
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
    """工作区目录项。"""

    name: str = Field(description="名称")
    relative_path: str = Field(description="相对工作区根的路径")
    absolute_path: str = Field(description="绝对路径")
    entry_type: str = Field(description="类型：directory 或 file")
    size_bytes: int | None = Field(
        default=None,
        description="文件大小（字节）；列表接口为性能可不返回",
    )
    selectable: bool = Field(description="是否可选为输入 DEM")


class WorkspaceListResponse(BaseModel):
    """工作区列表响应。"""

    relative_path: str = Field(description="当前相对路径")
    absolute_path: str = Field(description="当前绝对路径")
    parent_relative_path: str | None = Field(default=None, description="上级目录相对路径")
    entries: list[WorkspaceEntryInfo] = Field(description="目录与文件条目")
