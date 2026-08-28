# Ocean Terrain Handler

通用地形 GeoTIFF 预处理与 Cesium 地形瓦片切片服务。基于 **FastAPI + Celery + Redis**，通过 Docker 调用本地自构建的 [cesium-terrain-builder](https://github.com/ahuarte47/cesium-terrain-builder)（`master-quantized-mesh` 分支）生成 Cesium 地形瓦片。

## 架构

```
客户端 → FastAPI → Redis 队列 → Celery Worker
                                    ├─ GDAL 预处理 (gdalwarp / fillnodata / gdaladdo)
                                    ├─ ctb-tile (本地 Docker 镜像) → 瓦片输出
                                    └─ 注册 tileset → data/tilesets/terrain/

浏览器 / Cesium 客户端 → nginx terrain-server :8103
                           ├─ /tilesets/{name}/     地形瓦片 + layer.json
                           ├─ /preview/             Cesium 预览 UI
                           └─ /api/                 反代 FastAPI（同源）
```

| 组件 | 职责 |
|------|------|
| API | 接收任务、文件上传、查询状态、发布管理 |
| Worker | GDAL 预处理 + 调用 CTB 切片 + 注册发布 |
| Redis | 任务队列与状态存储 |
| terrain-server (nginx) | 地形瓦片 HTTP 发布 + Cesium 预览页 + API 反代 |
| 工作目录 | 输入 DEM、中间产物、瓦片输出、发布注册 |

## 处理流程

1. **校验** — `gdalinfo` 检查输入栅格
2. **投影** — `gdalwarp` 转为 EPSG:4326（geodetic 推荐）
3. **NODATA 填充** — `gdal_fillnodata.py`（CTB 不处理空值，必须预处理）
4. **概览图** — `gdaladdo` 加速大文件切片
5. **切片** — `ctb-tile` 生成 `{z}/{x}/{y}.terrain`
6. **发布** — 生成/校验 `layer.json`，注册到 `data/tilesets/terrain/{name}`，由 nginx 对外服务

## CTB 镜像（本地自构建）

本服务使用**本地自构建**的 CTB 镜像，而非 Docker Hub 上的 `homme/cesium-terrain-builder`（该远程镜像已停更 8 年以上，仅支持 `heightmap-1.0`，不支持 quantized-mesh）。

本地镜像基于 `ahuarte47/cesium-terrain-builder` 的 `master-quantized-mesh` 分支，支持两种输出格式：

| `output_format` | layer.json format | 说明 |
|-----------------|-------------------|------|
| `Terrain` | `heightmap-1.0` | 传统高度图瓦片 |
| `Mesh` | `quantized-mesh-1.0` | 量化网格（推荐，现代 Cesium 性能更好） |

### 构建 CTB 镜像

在 `cesium-terrain-builder` 源码目录执行：

```bash
cd D:\workspace\cesium-terrain-builder
docker build -t cesium-terrain-builder:local .
```

验证：

```bash
docker run --rm cesium-terrain-builder:local ctb-tile --version
```

## 快速开始

### 前置条件

- Docker & Docker Compose
- 已构建本地 CTB 镜像：`cesium-terrain-builder:local`（见上方构建步骤）

### 启动

```bash
cd D:\workspace\ocean-terrain-handler
cp .env.example .env
docker compose up -d --build
```

服务地址：

| 服务 | URL |
|------|-----|
| API | `http://localhost:8000` |
| API 文档 | `http://localhost:8000/docs` |
| 地形发布 | `http://localhost:8103/tilesets/{tileset_name}/` |
| 预览 UI | `http://localhost:8103/preview/?tileset={tileset_name}` |

预览页支持侧边栏：数据接入、进度查询、瓦片发布、图层管理。URL 参数：

- `?tileset={name}` — 加载已发布地形
- `?job={job_id}` — 打开进度面板并轮询
- `?lon=&lat=&height=` — 手动设置相机
- `?exaggeration=N` — 垂直夸大（调试用，默认 1.0 真实比例）

### 提交任务（quantized-mesh 示例）

将 DEM 放入 `./data/` 目录后：

```bash
curl -X POST http://localhost:8000/api/v1/terrain/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "input_path": "/data/workspace/dem.tif",
    "preprocess": {
      "target_crs": "EPSG:4326",
      "fill_nodata": true,
      "build_overviews": true,
      "block_size": 256
    },
    "ctb_options": {
      "output_format": "Mesh",
      "profile": "geodetic",
      "thread_count": 4,
      "start_zoom": 14,
      "end_zoom": 0,
      "cesium_friendly": true,
      "vertex_normals": true,
      "resume": true
    }
  }'
```

### 上传文件提交

```bash
curl -X POST http://localhost:8000/api/v1/terrain/jobs/upload \
  -F "file=@dem.tif"
```

### 查询任务状态

```bash
curl http://localhost:8000/api/v1/terrain/jobs/{job_id}
```

任务状态：`queued` → `preprocessing` → `tiling` → `publishing` → `completed` / `failed`

输出目录：`./data/jobs/{job_id}/tiles/`  
发布 URL：`http://localhost:8103/tilesets/{job_id}/`（任务完成后自动发布，见 `publish` 参数）

### 查询已发布 tileset

```bash
curl http://localhost:8000/api/v1/terrain/tilesets
```

### 手动发布 / 取消发布

```bash
# 发布已完成任务
curl -X POST http://localhost:8000/api/v1/terrain/jobs/{job_id}/publish

# 取消发布
curl -X DELETE http://localhost:8000/api/v1/terrain/jobs/{job_id}/publish
```

### Cesium 客户端加载

```javascript
viewer.terrainProvider = await Cesium.CesiumTerrainProvider.fromUrl(
  "http://localhost:8103/tilesets/{job_id}"
);
```

## API 参数

### 预处理 `preprocess`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `target_crs` | string | `EPSG:4326` | 目标坐标系 |
| `fill_nodata` | bool | `true` | 填充 NODATA |
| `build_overviews` | bool | `true` | 构建概览图 |
| `block_size` | int | `256` | GDAL TIFF 块大小（须为 16 的倍数；勿与 CTB 的 65 像素瓦片混淆） |
| `nodata_value` | float | — | 覆盖 NODATA 值 |

### CTB 切片 `ctb_options`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `output_format` | string | `Terrain` | `Terrain`（heightmap-1.0）/ `Mesh`（quantized-mesh-1.0，需本地镜像） |
| `profile` | string | `geodetic` | `geodetic` / `mercator` |
| `thread_count` | int | CPU 核数 | 线程数 |
| `tile_size` | int | 65 | 瓦片像素尺寸 |
| `start_zoom` | int | 自动 | 起始 zoom |
| `end_zoom` | int | `0` | 结束 zoom |
| `resampling_method` | string | `average` | 重采样算法 |
| `error_threshold` | float | `0.125` | 变换误差阈值 |
| `warp_memory` | int | — | 重投影内存（字节） |
| `resume` | bool | `false` | 断点续切 |
| `mesh_qfactor` | float | `1.0` | Mesh 几何误差系数 |
| `layer_only` | bool | `false` | 仅生成 layer.json |
| `cesium_friendly` | bool | `true` | CesiumJS 兼容根瓦片 |
| `vertex_normals` | bool | `false` | Mesh 顶点法线（地形光照） |
| `creation_options` | string[] | `[]` | GDAL 创建选项（非 Terrain 格式） |

### 发布 `publish`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `auto_publish` | bool | `AUTO_PUBLISH` 环境变量 | 切片完成后自动注册到 terrain-server |
| `tileset_name` | string | job_id | 发布名称，对应 URL `/tilesets/{name}/` |

完整 CLI 参数对照见 [ahuarte47/cesium-terrain-builder](https://github.com/ahuarte47/cesium-terrain-builder/tree/master-quantized-mesh)。

## 本地开发

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
pip install -e ".[dev]"

# 启动 Redis（可用 docker run -p 6379:6379 redis:7-alpine）
cp .env.example .env          # 修改 REDIS_URL 为 redis://localhost:6379/0

# 终端 1
uvicorn app.main:app --reload --port 8000

# 终端 2
celery -A app.worker.celery_app worker --loglevel=info
```

本地 Worker 需安装 GDAL 命令行工具，并确保 Docker 可执行且已构建 `cesium-terrain-builder:local` 镜像。

预览与瓦片发布需单独启动 nginx（或使用 `docker compose up terrain-server`）。

## 项目结构

```
ocean-terrain-handler/
├── app/
│   ├── main.py              # FastAPI 入口
│   ├── config.py            # 配置
│   ├── schemas.py           # 请求/响应模型
│   ├── api/routes.py        # REST 路由
│   ├── services/
│   │   ├── preprocessor.py  # GDAL 预处理
│   │   ├── ctb_runner.py    # CTB Docker 调用
│   │   ├── layer_json.py    # layer.json 生成
│   │   ├── tile_publisher.py # 瓦片发布注册
│   │   └── job_store.py     # Redis 任务状态
│   └── worker/
│       ├── celery_app.py    # Celery 配置
│       └── tasks.py         # 异步任务
├── docker/
│   └── nginx.conf           # 地形瓦片 + 预览 + API 反代
├── scripts/preview/         # Cesium 预览 SPA
├── tests/
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis 连接 |
| `WORKSPACE_DIR` | `/data/workspace` | 工作目录 |
| `HOST_WORKSPACE_DIR` | — | 宿主机上 `./data` 的绝对路径（Worker 经 docker.sock 调 CTB 时用于 `-v`；Windows 例：`D:/workspace/ocean-terrain-handler/data`） |
| `CTB_DOCKER_IMAGE` | `cesium-terrain-builder:local` | 本地自构建 CTB 镜像名 |
| `GDAL_CACHEMAX` | `512` | GDAL 缓存 (MB) |
| `JOB_TTL` | `604800` | 任务状态保留 (秒) |
| `TERRAIN_SERVER_PUBLIC_URL` | `http://localhost:8103` | terrain-server（nginx）对外 URL |
| `TERRAIN_BASE_PATH` | `/tilesets` | 地形 URL 前缀 |
| `AUTO_PUBLISH` | `true` | 切片完成后自动发布 |

## 注意事项

- 使用前必须先构建本地 CTB 镜像：`docker build -t cesium-terrain-builder:local D:\workspace\cesium-terrain-builder`
- 需要 quantized-mesh 格式时，设置 `output_format: "Mesh"`（仅本地镜像支持）
- 输入 DEM 应为海拔高程数据，多波段栅格仅使用第一波段
- NODATA 必须在切片前填充，否则 CTB 无法正确处理
- 大文件建议设置 `start_zoom` / `end_zoom` 分级切片，避免低级别 zoom 溢出
- Worker 容器需挂载 `/var/run/docker.sock` 以调用宿主机上的 CTB 镜像
- `data/tilesets/terrain/` 目录必须存在，API 启动时会自动创建
- 发布通过符号链接注册瓦片，Worker 容器需有创建 symlink 的权限
- 预览页通过 Cesium CDN 加载（需联网）；地形瓦片由 nginx 本地提供

## License

Apache-2.0
