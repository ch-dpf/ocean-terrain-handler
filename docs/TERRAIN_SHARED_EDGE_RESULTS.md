# 第二轮：共享边一致性与 Cesium 消费加固

日期：2026-09-03。工作树：main-zy（HEAD ddd0a17），参考 main：e980d49。生产路径仍不调用 GDAL 或外部 CTB 服务，保留进程内 Cython/C++ 扩展。本轮未部署或提交代码，生产验收状态仍为 false。

## 实现与原因

此前两个相邻瓦片的共享采样数组完全一致，但独立网格激活/传播产生了不同边界折线：s5e130 有效区最大裂缝 69.49m，n0e0 为 72.46m。`benchmarks/probe_shared_edges.py` 可定位反例，不能将其归因为 TIFF 或重投影采样误差。

原生 HeightField 新增只依赖边界高程的确定性折线简化，边界误差预算为该层 geometric error 的四分之一。保留折线节点，并将其间边界样本投影到折线；内部三角剖分新增的边界节点仍位于同一折线上。编码器在传播前激活这些节点，避免相邻内部地形影响共享边几何。独立瓦片量化仍可带来小量高差，并非字节级或数学上完全无缝。

生产 tiler 启用此算法，取消邻瓦片采样/激活，因此每瓦片只采样一次。低层编码 API 的 `canonical_edges=False` 默认值保留旧行为。任务签名升级为 `terrain-v3-canonical-edges`，防止续跑混合新旧算法。编译后的原生扩展必须随 Python 代码一起发布，不能仅替换 Python 文件。

固定 Cesium 1.120 的预览端加入 horizon 适配器。官方算法对于半球范围可能无可用单点 horizon；非有限点转为该版本内部支持的 undefined，让 Cesium 使用 bounding-volume 回退。保留有限点、请求节流返回值和异常传播；升级 Cesium 必须重新验证。此修复依赖经过验证的私有字段，仅覆盖本项目预览端，不代表输出二进制已符合所有客户端要求。

## 同输入三轮结果

三个源 TIFF 来自 `data/source`；两引擎读取同一个 main 预处理结果及其内部概览。4 CPU、4 线程、512MiB 配置、geodetic、z10→0、65×65、average、qfactor=1、法线开启。各进程串行，第二轮交换分支顺序，无诊断插桩；时间不含容器启动，未清理操作系统缓存。两套实现保留自身写入方式，不能解释为完全相同持久化成本的纯算法速度。

| 样例 | main 中位秒 | 当前中位秒 | 当前/main | main/当前输出字节 | main/当前 RSS MiB |
|---|---:|---:|---:|---:|---:|
| s85e80 | 1.496 | 1.948 | 1.302 | 412346 / 194009 | 319.89 / 228.50 |
| s5e130 | 1.538 | 1.778 | 1.156 | 584479 / 1157171 | 226.30 / 150.46 |
| n0e0 | 1.573 | 1.961 | 1.247 | 694945 / 1570690 | 323.60 / 231.25 |

两项仍超过 1.2× 耗时目标；海陆/海底输出为 main 的约 1.98×/2.26×。准确度提升伴随顶点和体积成本，不能宣称所有维度均优于 main。历史轮次运行环境波动明显，性能比较以本表同期成对结果为准。

| 样例 | 有效源区共享边最大差 main→当前 (m) | 内部参考 RMSE main→当前 (m) | 边界参考最大误差 main→当前 (m) |
|---|---:|---:|---:|
| s85e80 | 0.08162 → 0.00072 | 2.326 → 2.491 | 11.000 → 11.000 |
| s5e130 | 69.49370 → 0.10647 | 24.910 → 17.283 | 210.172 → 189.235 |
| n0e0 | 72.45995 → 0.10411 | 29.060 → 17.387 | 183.535 → 188.771 |

独立解码并在固定点位对源数据参考面取样；这是本项目测试口径，不等于所有顶点或连续曲面的全局误差上界。南极内部 RMSE 略升，n0e0 边界最大误差增加约 5.24m，必须保留为精度权衡。n0e0 含源覆盖外点的全域接缝最大差仍约 5224.52m；不能用有效区 <0.11m 掩盖外推/零填充交界。跨层 LOD 接缝未在本轮证明。

## 验证与证据

- 完整回归：159 passed，2 条第三方弃用警告。新增横纵共享边独立解码测试、只读输入不变性测试。
- 3548 个当前瓦片全部解码成功，均含法线，与 main 路径集合一致；三次运行每个样例的瓦片集合哈希完全一致。
- 对真实 Cesium 1.120 官方构建执行 Node 契约测试，验证半球无 horizon、有限点不变、节流、错误及版本约束。构建 SHA256：`3d661eea94bcafe24040e11773200dce720b507603491a4a8caa82e082cb37e8`。
- 实际浏览器读取两个根瓦片并经真实 worker 建网，网格坐标有限；WebGL 运行 180 帧，无 terrain/render 错误。该项仅为冒烟检查，未证明全部详细地形已加载、跨视角剔除正确或视觉精度通过。
- 所有样例的 `0/0/0.terrain` 二进制仍含非有限 horizon；适配器不改写该文件。

本轮证据：`data/tiling_edges_20260903/{timings,summary,accuracy,readiness,cesium_smoke}.json` 和 `native_sha256.txt`。旧轮证据保留在 `data/tiling_production_20260903`。完整瓦片在 Docker 数据卷 `/data/tiling_production/canonicaledges{1,2,3}`。这些大体积本地证据受 data 忽略规则管理，不自动随代码提交。

复现工具：`benchmarks/run-tiling-edge-rounds.ps1`，随后运行 `export_tiling_production.py --prefix canonicaledges` 并挂载新导出目录。输出目录必须未被占用；先编译当前原生扩展。浏览器工具：`serve_cesium_acceptance.py`、`cesium_acceptance.html`、`test_cesium_horizon.cjs`；浏览器测试需要取得官方 Cesium 构建及 worker 资源，测试资源不是新增生产依赖。

## 下一阶段顺序

1. 固定覆盖外有效性和跨层衔接规则，增加跨边界样例及误差门槛，避免用零填充或不一致外推产生深裂缝。
2. 解决空根的通用二进制/客户端兼容策略，覆盖完整视角和根到高层级加载，不能将本次预览适配替代通用验收。
3. 保持上述精度约束，剖析并优化规范折线激活导致的内部顶点传播、序列化、压缩及持久化开销，再运行同期三轮比较。
4. 完成投影/NODATA/旋转栅格矩阵、故障恢复、并发长稳和生产容量验收。

官方依据：[Cesium 1.120 EllipsoidalOccluder](https://github.com/CesiumGS/cesium/blob/1.120/packages/engine/Source/Core/EllipsoidalOccluder.js)、[QuantizedMeshTerrainData](https://github.com/CesiumGS/cesium/blob/1.120/packages/engine/Source/Core/QuantizedMeshTerrainData.js)、[GlobeSurfaceTileProvider](https://github.com/CesiumGS/cesium/blob/1.120/packages/engine/Source/Scene/GlobeSurfaceTileProvider.js)。消费端行为依据固定版本源码及上述实测，不推断未来版本兼容。
