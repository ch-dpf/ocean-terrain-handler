# Ocean Terrain Handler — 裸机部署

## 1. 支持范围

应用运行时不调用 GDAL CLI、`ctb-tile` 或 Docker。外部中间件只有
Redis（队列/状态）和 Nginx（瓦片与反向代理）。

| 平台 | 原生 wheel | 裸机说明 |
|---|---|---|
| Linux x86_64 | `manylinux_x86_64` | 生产支持，提供 systemd/Nginx 模板 |
| Linux arm64 | `manylinux_aarch64` | 生产支持，提供 systemd/Nginx 模板 |
| Windows x64 | `win_amd64` | wheel 支持；Celery 使用 `--pool=solo`，发布 symlink 需开发者模式/管理员 |
| macOS Intel | `macosx_x86_64` | wheel 支持，进程托管由使用方选择 |
| macOS Apple Silicon | `macosx_arm64` | wheel 支持，进程托管由使用方选择 |

wheel 与操作系统、CPU 架构及 CPython ABI 绑定。CI 为每个受支持组合构建一次；
业务主机不安装 C++ 编译器。把所有 wheel 放在同一目录后，`pip` 会按当前平台
自动选中匹配文件：

```bash
python -m pip install --find-links /path/to/wheels ocean-terrain-handler
```

生产 Worker 必须加载 `_ctb_core`。没有匹配 wheel 时安装或 Worker 启动应失败，
不会退回无法处理大 DEM 的 Python meshing。

## 2. 架构

```text
Nginx :8103
  ├── /api/*      → FastAPI :8000
  ├── /tilesets/* → WORKSPACE_DIR/tilesets/terrain
  └── /preview/*  → repository/scripts/preview

FastAPI + Celery Worker → Redis :6379
Worker                  → WORKSPACE_DIR
```

Python 负责 GeoTIFF 分块读取、overview 选择、调度和文件写入。C++ 扩展负责
重采样数值内核、BTT meshing 及 terrain 二进制编码；这些调用释放 GIL。

## 3. Linux x86_64 / arm64 生产部署

示例路径：

- 程序与预览页：`/opt/ocean-terrain-handler`
- 虚拟环境：`/opt/ocean-terrain-handler/.venv`
- 数据：`/var/lib/ocean-terrain-handler/data`
- 环境文件：`/etc/ocean-terrain-handler/env`
- 运行用户：`ocean-terrain`

### 3.1 安装系统服务

Debian/Ubuntu 示例：

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv redis-server nginx
sudo systemctl enable --now redis-server
```

不需要 `g++`、Cython、GDAL、`libgdal` 或 CTB。
Linux wheel 静态链接 GCC C++ runtime；除 glibc/libm 外不要求额外 CTB 原生库。

### 3.2 创建用户和目录

```bash
sudo useradd --system --user-group --home /opt/ocean-terrain-handler \
  --shell /usr/sbin/nologin ocean-terrain
sudo usermod -a -G ocean-terrain www-data
sudo mkdir -p /opt/ocean-terrain-handler \
  /var/lib/ocean-terrain-handler/data \
  /etc/ocean-terrain-handler
sudo chown -R ocean-terrain:ocean-terrain \
  /opt/ocean-terrain-handler /var/lib/ocean-terrain-handler
```

`www-data` 加入共享组后才能穿过 `UMask=0027` 创建的数据目录；修改组后需重启
Nginx。`.terrain` 文件另外显式写为 `0644`，兼容容器中不同 UID 的 Nginx。

将仓库内容（主要用于预览静态文件和部署模板）放到
`/opt/ocean-terrain-handler`，然后安装匹配架构的 wheel：

```bash
cd /opt/ocean-terrain-handler
sudo -u ocean-terrain python3.12 -m venv .venv
sudo -u ocean-terrain .venv/bin/pip install --upgrade pip
sudo -u ocean-terrain .venv/bin/pip install \
  /path/to/ocean_terrain_handler-*-manylinux_*.whl
```

不要在生产主机执行 `setup.py build_ext`。若从私有 Python 索引发布所有 wheel，
直接使用：

```bash
sudo -u ocean-terrain .venv/bin/pip install \
  --index-url https://your-python-index/simple ocean-terrain-handler
```

### 3.3 验证原生热路径

```bash
cd /var/lib/ocean-terrain-handler
sudo -u ocean-terrain /opt/ocean-terrain-handler/.venv/bin/python \
  -m app.services.ctb.native_check
```

返回 JSON 且 `"ok": true` 才能启动 Worker。systemd Worker 模板也会在每次启动前
执行同一检查。

### 3.4 环境配置

```bash
sudo cp deploy/env.production.example /etc/ocean-terrain-handler/env
sudo editor /etc/ocean-terrain-handler/env
```

至少修改：

- `TERRAIN_SERVER_PUBLIC_URL`
- `WORKSPACE_DIR`（必须与 Nginx alias 一致）
- Redis URL（Redis 不在本机时）

### 3.5 API 与 Worker

```bash
sudo cp deploy/systemd/ocean-terrain-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ocean-terrain-api ocean-terrain-worker
sudo systemctl status ocean-terrain-api ocean-terrain-worker
```

日志：

```bash
journalctl -u ocean-terrain-api -f
journalctl -u ocean-terrain-worker -f
```

避免 CPU 过量并发：建议使
`CELERY_WORKER_CONCURRENCY × ctb_options.thread_count` 不超过主机逻辑核数。

### 3.6 Nginx

确认 `deploy/nginx-baremetal.conf` 中的数据和预览路径与实际路径一致，然后：

```bash
sudo cp deploy/nginx-baremetal.conf \
  /etc/nginx/sites-available/ocean-terrain-handler
sudo ln -sfn /etc/nginx/sites-available/ocean-terrain-handler \
  /etc/nginx/sites-enabled/ocean-terrain-handler
sudo nginx -t
sudo systemctl reload nginx
```

只需对外开放 `8103`。`8000` 可仅监听内网/本机。

## 4. Windows / macOS 裸机

从 Release 下载对应 wheel，并在虚拟环境安装：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install ocean_terrain_handler-*-win_amd64.whl
.venv\Scripts\python -m app.services.ctb.native_check
```

macOS 使用 `.venv/bin/python` 和对应的 `macosx_*` wheel。

Windows 的 Celery 上游不提供正式生产支持；功能部署需使用：

```powershell
.\deploy\windows\run.ps1 -Mode worker -Venv .venv -EnvFile .env
```

API 使用 `-Mode api`，安装验证使用 `-Mode check`。脚本会加载 `.env` 并执行
原生自检。单个地形任务内部仍按 `ctb_options.thread_count` 并行。发布目录使用 symlink，
因此 Windows 必须开启“开发者模式”或以具有创建 symlink 权限的账户运行。
`deploy/windows/nginx.conf` 是 Windows 路径模板，使用前修改其中的数据目录和
预览目录。
需要严格生产 SLA 时，官方生产基线是 Linux x86_64/arm64 裸机。

## 5. 构建与发布 wheel

`.github/workflows/native-wheels.yml` 构建并测试：

- CPython 3.11 / 3.12 / 3.13 / 3.14
- Linux x86_64 / aarch64
- Windows x64
- macOS Intel / Apple Silicon

PR 构建结果保存在 Actions artifacts；推送 `v*` tag 时 wheel 会附加到 GitHub
Release。发布到私有 Python 索引后，平台选择完全交给 `pip` wheel tag。

本地只用于开发验证的源码构建：

```bash
python -m pip install build
python -m build
python -m pip install dist/*.whl
python -m app.services.ctb.native_check
```

源码包必须能再次构建 wheel：

```bash
python -m pip wheel dist/*.tar.gz --no-deps --wheel-dir wheelhouse
```

## 6. 大 DEM 性能验收

微基准不能代表生产性能。使用真实、已预处理 DEM 验证窗口读取、overview、
重采样、meshing、编码和文件写入：

```bash
ocean-terrain-benchmark \
  /data/real-dem.tif /tmp/terrain-benchmark \
  --start-zoom 14 --end-zoom 7 --threads 8 \
  --resampling average \
  --minimum-tiles-per-second YOUR_ACCEPTANCE_RATE
```

验收值应根据目标 CPU、存储和真实 DEM 制定。基准输出总耗时、瓦片数、
tiles/s 和 terrain 字节数；低于指定门槛会返回非零状态。

本分支在当前 Linux x86_64 构建机上的回归样本：4096×4096 tiled
GeoTIFF、zoom 13→7、`average`、2982 张瓦片；4 线程约 6.3 秒
（471 tiles/s），1 线程约 9.3 秒（320 tiles/s）。该数据只证明热路径
释放 GIL 并可并行，不能替代目标机器和真实 DEM 的验收值。

## 7. 验证服务

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8103/health
```

将 DEM 放入 `WORKSPACE_DIR/source/` 后，通过 API 提交任务。发布后检查：

```bash
curl http://127.0.0.1:8103/tilesets/NAME/layer.json
```

发布使用相对 symlink，Worker 用户必须对 `WORKSPACE_DIR` 有写入和创建 symlink
权限，Nginx 用户必须能遍历并读取该目录。

systemd 的 `WorkingDirectory` 故意不设为源码仓库：否则仓库中的 `app/` 会优先于
虚拟环境里带 `_ctb_core` 的已安装 wheel。API 与 Worker 应从 wheel 运行，仓库
只提供预览静态文件和部署模板。
