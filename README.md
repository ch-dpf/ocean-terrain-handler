# Ocean Terrain Handler

通用地形 GeoTIFF 预处理与 Cesium 地形瓦片切片服务。基于 **FastAPI + Celery + Redis** 与自研 Python 栅格引擎（`tifffile` / `pyproj` / `numpy`），切片为 [cesium-terrain-builder](https://github.com/ahuarte47/cesium-terrain-builder) `master-quantized-mesh` 的 Python 原样移植（量化网格 / 高度图），不再调用 Docker `ctb-tile`。

## 架构

```
客户端 → FastAPI → Redis 队列 → Celery Worker
                                    ├─ 自研 Python 栅格预处理 (reproject / fill-nodata / overview)
                                    ├─ 自研 Python CTB 切片 (quantized-mesh / heightmap)
                                    └─ 注册 tileset → data/tilesets/terrain/

浏览器 / Cesium 客户端 → nginx terrain-server :8103
                           ├─ /tilesets/{name}/     地形瓦片 + layer.json
                           ├─ /preview/             Cesium 预览 UI
                           └─ /api/                 反代 FastAPI（同源）
```

| 组件 | 职责 |
|------|------|
| API | 接收任务、文件上传、查询状态、发布管理 |
| Worker | Python 栅格预处理 + CTB 兼容切片 + 注册发布 |
| Redis | 任务队列与状态存储 |
| terrain-server (nginx) | 地形瓦片 HTTP 发布 + Cesium 预览页 + API 反代 |
| 工作目录 | 输入 DEM、中间产物、瓦片输出、发布注册 |

## 处理流程

1. **校验** — 读取 GeoTIFF 元数据，确认尺寸 / CRS / NODATA
2. **投影** — 重投影为 EPSG:4326（geodetic 推荐）
3. **NODATA 填充** — 四方向反距离加权填充（CTB 不处理空值，必须预处理）
4. **概览图** — 构建 2/4/8/16 金字塔，加速大文件切片
5. **切片** — Python CTB 兼容实现生成 `{z}/{x}/{y}.terrain`
6. **发布** — 生成/校验 `layer.json`，注册到 `data/tilesets/terrain/{name}`，由 nginx 对外服务

## 地形切片（CTB 兼容 Python 实现）

切片算法、常量和规则原样移植自 `ahuarte47/cesium-terrain-builder` 的 `master-quantized-mesh` 分支（Apache-2.0），包括：

- TMS Global Geodetic / Mercator 网格（默认瓦片边长 geodetic 65、mercator 256）
- 高程采样时的 terrain 规格 1 像素西/北重叠
- Chunked LOD + Lindstrom–Koller BTT（`HeightFieldChunker`），几何误差 `2πR × 0.25 × mesh_qfactor / (tileWidth × tilesAtL0) / 2^z`
- zoom ≤ 6 的 CTB `smoothSmallZooms` 格网加密；zoom > 6 的邻接边激活缝合
- quantized-mesh-1.0（ECEF、包围球、地平遮挡点、zigzag、边索引、oct 法线）与 heightmap-1.0
- `-C` CesiumJS 缺根瓦片补齐

栅格采进瓦片使用本库 Python warp（精确反算；`error_threshold` 仅为兼容 CTB 选项，不再做 GDAL 近似变换）。

| `output_format` | layer.json format | 说明 |
|-----------------|-------------------|------|
| `Terrain` | `heightmap-1.0` | 传统高度图瓦片 |
| `Mesh` | `quantized-mesh-1.0` | 量化网格（推荐，现代 Cesium 性能更好） |

## 快速开始

### 前置条件

- Docker & Docker Compose（部署 API / Worker / Redis / nginx）
- 本地开发：Python 3.11+

### 启动

```bash
cd D:\workspace\ocean-terrain-handler
cp .env.example .env
docker compose up -d --build
```

默认将 `jobs` / `tilesets` / `uploads` 放在 Docker **命名卷**（Linux FS，加快瓦片读写）；**`source/`（DEM）仍绑在宿主机 `./data/source`**，避免大 DEM 挤占 Docker 默认所在的 C: 虚拟盘。

若本机已有历史 `jobs/tilesets/uploads`，可迁入卷：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\migrate-data-to-volume.ps1
docker compose up -d --build
```

补充 DEM 请直接放到 `.\data\source\`（不要整目录进命名卷，除非已把 Docker 数据盘迁到非 C: 盘）。

需要回到「整个 `./data` 都在宿主机」的旧模式时：

```bash
docker compose -f docker-compose.yml -f docker-compose.host-data.yml up -d --build
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
- `?job={job_id}` — 打开进度面板并订阅进度（WebSocket，失败时回退轮询）
- `?lon=&lat=&height=` — 手动设置相机
- `?exaggeration=N` — 垂直夸大（调试用，默认 1.0 真实比例）

### 提交任务（quantized-mesh 示例）

将 DEM 放入 `./data/source/` 目录后：

```bash
curl -X POST http://localhost:8000/api/v1/terrain/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "input_path": "/data/workspace/source/dem.tif",
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
# REST 快照（保留；重连/调试/非浏览器客户端可用）
curl http://localhost:8000/api/v1/terrain/jobs/{job_id}
```

任务状态：`queued` → `preprocessing` → `tiling` → `publishing` → `completed` / `failed`

查询响应含量化进度字段 `progress`（`percent` 为已写入/计划的未压缩栅格字节比，0–100；另含 `bytes_done` / `bytes_planned`、`phase`、`message`，切片阶段可含 zoom），以及处理耗时字段 `created_at` / `completed_at` / `elapsed_seconds`。

若 Redis 中的任务记录已按 `JOB_TTL` 过期，查询会回退读取磁盘上的 `jobs/{job_id}/manifest.json`（无 manifest 时若已有 `tiles/` 产物则按已完成快照返回）。WebSocket 在仅磁盘快照可用时发送一次终态/静态快照后关闭。

实时进度通过 WebSocket 推送（与 REST 同结构的 `TerrainJobDetail` JSON）：

```text
ws://localhost:8000/api/v1/terrain/jobs/{job_id}/ws
```

连接后立即收到当前快照，随后为增量更新；`completed` / `failed` 后关闭连接。创建任务响应同时返回 `progress_url` 与 `progress_ws_url`。预览页优先使用 WebSocket，不可用时回退到 2s 轮询。

输出目录（命名卷内）：`/data/workspace/jobs/{job_id}/tiles/`  
发布 URL：`http://localhost:8103/tilesets/{job_id}/`（默认不自动发布，见 `publish.auto_publish` / `AUTO_PUBLISH`）

### 查询已发布 tileset

```bash
curl http://localhost:8000/api/v1/terrain/tilesets
```

响应中每个 tileset 含 `name`、`terrain_url`，以及从 `layer.json` 解析的 `format` / `format_label`、`projection` / `crs`、`min_zoom` / `max_zoom`（无元数据时为 `null`）。预览页「图层管理」以标签展示格式、坐标系与层级。

列表接口优先读取发布目录旁路文件 `data/tilesets/terrain/.{name}.layer-meta.json`（发布时写入，并带进程内缓存），避免每次跟随 symlink 进入大型瓦片树。

### 发布 / 下架

```bash
# 按 job 发布（Redis 有记录，或已过期但磁盘仍有 jobs/{id}/tiles/）
curl -X POST http://localhost:8000/api/v1/terrain/jobs/{job_id}/publish \
  -H "Content-Type: application/json" \
  -d '{"tileset_name": "coast-dem"}'

# 按 job 下架（Redis 过期时按 job_id 名尽力删链接）
curl -X DELETE http://localhost:8000/api/v1/terrain/jobs/{job_id}/publish

# 不依赖 Redis：按磁盘路径 / job 目录发布
curl -X POST http://localhost:8000/api/v1/terrain/tilesets/publish \
  -H "Content-Type: application/json" \
  -d '{"job_id": "a0e3214c-cc22-4278-90aa-af20b5745c0a", "tileset_name": "coast-dem"}'

# 按名称下架（不依赖 Redis）
curl -X DELETE http://localhost:8000/api/v1/terrain/tilesets/{tileset_name}
```

`tilesets/publish` 也可传 `tiles_dir`（工作区内路径，与 `job_id` 二选一）。`output_format` / `profile` 可选，缺省时从已有 `layer.json` 推断，否则默认 Mesh + geodetic。

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
| `block_size` | int | `256` | TIFF 块大小（须为 16 的倍数；勿与 CTB 的 65 像素瓦片混淆） |
| `nodata_value` | float | — | 覆盖 NODATA 值 |

### CTB 切片 `ctb_options`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `output_format` | string | `Mesh` | `Mesh`（quantized-mesh-1.0）/ `Terrain`（heightmap-1.0） |
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
| `vertex_normals` | bool | `true` | Mesh 顶点法线（地形光照；仅 `output_format=Mesh` 时生效） |
| `creation_options` | string[] | `[]` | GDAL 创建选项（非 Terrain 格式） |

### 发布 `publish`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `auto_publish` | bool | `false`（`AUTO_PUBLISH`） | 切片完成后自动注册到 terrain-server |
| `tileset_name` | string | job_id | 发布名称，对应 URL `/tilesets/{name}/` |

完整选项对照见 [ahuarte47/cesium-terrain-builder](https://github.com/ahuarte47/cesium-terrain-builder/tree/master-quantized-mesh) 的 `ctb-tile` CLI。

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

Worker 镜像基于 `python:3.12-slim`，预处理与切片均为自研 Python 实现（`tifffile` / `pyproj` / `numpy`），不调用 GDAL 命令行，也不再通过 `docker.sock` 启动 CTB。本地开发需 Python 3.11+。

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
│   │   ├── preprocessor.py  # DEM 预处理
│   │   ├── raster/          # 自研 GeoTIFF / warp / fill-nodata / overview
│   │   ├── ctb/             # CTB 兼容切片（网格 / BTT / quantized-mesh）
│   │   ├── ctb_runner.py    # 切片入口
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
| `GDAL_CACHEMAX` | `512` | 预处理与切片的 GeoTIFF 瓦片解码缓存 (MB) |
| `JOB_TTL` | `604800` | 任务状态保留 (秒) |
| `TERRAIN_SERVER_PUBLIC_URL` | `http://localhost:8103` | terrain-server（nginx）对外 URL |
| `TERRAIN_BASE_PATH` | `/tilesets` | 地形 URL 前缀 |
| `AUTO_PUBLISH` | `false` | 切片完成后自动发布 |

## 注意事项

- 默认输出 `output_format: "Mesh"`（quantized-mesh）；若需 heightmap 再设为 `"Terrain"`
- 输入 DEM 应为海拔高程数据，多波段栅格仅使用第一波段
- NODATA 必须在切片前填充，否则空值会进入网格高程
- 大文件建议设置 `start_zoom` / `end_zoom` 分级切片
- 默认 `jobs/tilesets/uploads` 用命名卷，`source/` 仍在宿主机 `./data/source`；历史产物可用 `scripts/migrate-data-to-volume.ps1` 迁入卷
- `data/tilesets/terrain/`（卷内）在 API 启动时会自动创建
- 发布通过符号链接注册瓦片，Worker 容器需有创建 symlink 的权限
- 预览页通过 Cesium CDN 加载（需联网）；地形瓦片由 nginx 本地提供

## License

Apache-2.0
