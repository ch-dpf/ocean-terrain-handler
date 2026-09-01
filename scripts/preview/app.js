(function () {
  "use strict";

  const API_BASE = "/api/v1/terrain";
  const POLL_INTERVAL_MS = 2000;
  const WS_RECONNECT_BASE_MS = 1000;
  const WS_RECONNECT_MAX_MS = 15000;
  const WS_MAX_FAILURES_BEFORE_POLL = 3;

  if (typeof Cesium.Ion !== "undefined") {
    Cesium.Ion.defaultAccessToken = undefined;
  }

  const statusEl = document.getElementById("status");
  const coordsEl = document.getElementById("coords");
  const errorBanner = document.getElementById("errorBanner");
  const toastContainer = document.getElementById("toastContainer");

  const panels = {
    ingest: document.getElementById("ingestPanel"),
    progress: document.getElementById("progressPanel"),
    publish: document.getElementById("publishPanel"),
    layers: document.getElementById("layersPanel"),
  };
  const overlay = document.getElementById("panelOverlay");

  let viewer = null;
  let currentTileset = null;
  let terrainExaggeration = 1.0;
  let pollTimer = null;
  let progressSocket = null;
  let progressReconnectTimer = null;
  let progressWatchJobId = null;
  let progressWatchMode = null; // "ws" | "poll"
  let progressWsFailures = 0;
  let progressWatchGeneration = 0;
  let activePanel = null;
  let lastJobDetail = null;
  let activeSubmitTab = "upload";
  let workspaceRelativePath = "";
  let selectedWorkspaceFile = null;

  const visualizeState = {
    shadingMode: "none",
    elevationContour: false,
    rampPreset: "hypsometric",
    minHeight: -500,
    maxHeight: 3000,
    contourSpacing: 150,
    contourWidth: 2,
    elevationRange: null,
  };

  const RAMP_PRESETS = {
    hypsometric: {
      stops: [
        [0.0, "#00204d"],
        [0.15, "#1d6cb4"],
        [0.35, "#3cb371"],
        [0.55, "#ffd700"],
        [0.75, "#d33038"],
        [1.0, "#ffffff"],
      ],
    },
    ocean: {
      stops: [
        [0.0, "#0a1e5e"],
        [0.25, "#134e9a"],
        [0.5, "#2a9df4"],
        [0.75, "#7ec8e3"],
        [1.0, "#e8f7ff"],
      ],
    },
    terrain: {
      stops: [
        [0.0, "#1a4d1a"],
        [0.3, "#3d8b37"],
        [0.55, "#a67c52"],
        [0.8, "#8b4513"],
        [1.0, "#f5f5dc"],
      ],
    },
    // Cesium Sandcastle slope stops: flat → steep
    slope: {
      stops: [
        [0.0, "#000000"],
        [0.29, "#2747E0"],
        [0.5, "#D33B7D"],
        [0.7071, "#D33038"],
        [0.87, "#FF9742"],
        [0.91, "#ffd700"],
        [1.0, "#ffffff"],
      ],
    },
    // Cesium Sandcastle aspect stops: N→E→S→W→N
    aspect: {
      stops: [
        [0.0, "#000000"],
        [0.2, "#2747E0"],
        [0.4, "#D33B7D"],
        [0.6, "#D33038"],
        [0.8, "#FF9742"],
        [0.9, "#ffd700"],
        [1.0, "#ffffff"],
      ],
    },
  };

  const SHADING_LABELS = {
    none: "无",
    elevation: "高程着色",
    slope: "坡度着色",
    aspect: "坡向着色",
  };

  function createColorRampCanvas(presetName) {
    const preset = RAMP_PRESETS[presetName] || RAMP_PRESETS.hypsometric;
    const canvas = document.createElement("canvas");
    canvas.width = 256;
    canvas.height = 1;
    const ctx = canvas.getContext("2d");
    const gradient = ctx.createLinearGradient(0, 0, 256, 0);
    preset.stops.forEach(function (stop) {
      gradient.addColorStop(stop[0], stop[1]);
    });
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 256, 1);
    return canvas;
  }

  function drawLegendCanvas(presetName) {
    const canvas = document.getElementById("legendCanvas");
    const ctx = canvas.getContext("2d");
    const preset = RAMP_PRESETS[presetName] || RAMP_PRESETS.hypsometric;
    const gradient = ctx.createLinearGradient(0, 0, canvas.width, 0);
    preset.stops.forEach(function (stop) {
      gradient.addColorStop(stop[0], stop[1]);
    });
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }

  function syncVisualizeOptionVisibility() {
    const mode = document.getElementById("optShadingMode").value;
    const contourOn = document.getElementById("optElevationContour").checked;
    document.getElementById("elevationOptions").hidden = mode !== "elevation";
    document.getElementById("contourOptions").hidden = !contourOn;

    document.querySelectorAll(".shading-btn[data-shading]").forEach(function (btn) {
      const kind = btn.dataset.shading;
      if (kind === "contour") {
        btn.classList.toggle("active", contourOn);
      } else {
        btn.classList.toggle("active", mode === kind);
      }
    });
  }

  function setShadingMode(mode) {
    const current = document.getElementById("optShadingMode").value;
    document.getElementById("optShadingMode").value =
      current === mode ? "none" : mode;
    applyGlobeMaterial();
  }

  function toggleContour() {
    const el = document.getElementById("optElevationContour");
    el.checked = !el.checked;
    applyGlobeMaterial();
  }

  function readVisualizeControls() {
    visualizeState.shadingMode = document.getElementById("optShadingMode").value;
    visualizeState.elevationContour =
      document.getElementById("optElevationContour").checked;
    visualizeState.rampPreset = document.getElementById("optRampPreset").value;
    visualizeState.minHeight = parseNumber(
      document.getElementById("optMinHeight").value,
      visualizeState.minHeight,
    );
    visualizeState.maxHeight = parseNumber(
      document.getElementById("optMaxHeight").value,
      visualizeState.maxHeight,
    );
    visualizeState.contourSpacing = Math.max(
      1,
      parseNumber(document.getElementById("optContourSpacing").value, 150),
    );
    visualizeState.contourWidth = Cesium.Math.clamp(
      parseNumber(document.getElementById("optContourWidth").value, 2),
      1,
      8,
    );
  }

  function updateLegendDisplay() {
    const legend = document.getElementById("elevationLegend");
    const mode = visualizeState.shadingMode;

    if (mode === "none") {
      legend.hidden = true;
      return;
    }

    legend.hidden = false;
    if (mode === "elevation") {
      document.getElementById("legendLow").textContent =
        visualizeState.minHeight.toFixed(0) + " m";
      document.getElementById("legendHigh").textContent =
        visualizeState.maxHeight.toFixed(0) + " m";
      drawLegendCanvas(visualizeState.rampPreset);
    } else if (mode === "slope") {
      document.getElementById("legendLow").textContent = "0° 平";
      document.getElementById("legendHigh").textContent = "90° 陡";
      drawLegendCanvas("slope");
    } else if (mode === "aspect") {
      document.getElementById("legendLow").textContent = "N";
      document.getElementById("legendHigh").textContent = "N";
      drawLegendCanvas("aspect");
    }
  }

  function setBaseImageryAlpha(alpha) {
    if (!viewer || viewer.imageryLayers.length === 0) {
      return;
    }
    viewer.imageryLayers.get(0).alpha = alpha;
  }

  function applyGlobeLighting() {
    if (!viewer) {
      return;
    }
    const enabled = document.getElementById("optEnableLighting").checked;
    viewer.scene.globe.enableLighting = enabled;
  }

  function createShadingMaterial(mode) {
    if (mode === "elevation") {
      const material = Cesium.Material.fromType("ElevationRamp");
      material.uniforms.image = createColorRampCanvas(visualizeState.rampPreset);
      material.uniforms.minimumHeight = visualizeState.minHeight;
      material.uniforms.maximumHeight = visualizeState.maxHeight;
      return { material: material, type: "ElevationRamp" };
    }
    if (mode === "slope") {
      const material = Cesium.Material.fromType("SlopeRamp");
      material.uniforms.image = createColorRampCanvas("slope");
      return { material: material, type: "SlopeRamp" };
    }
    if (mode === "aspect") {
      const material = Cesium.Material.fromType("AspectRamp");
      material.uniforms.image = createColorRampCanvas("aspect");
      return { material: material, type: "AspectRamp" };
    }
    return null;
  }

  function applyContourUniforms(material) {
    material.uniforms.color = Cesium.Color.WHITE.withAlpha(0.95);
    material.uniforms.spacing = visualizeState.contourSpacing;
    material.uniforms.width = visualizeState.contourWidth;
  }

  function createCompositeShadingContour(shadingType, shadingMaterial) {
    const composite = new Cesium.Material({
      fabric: {
        type: shadingType + "ContourComposite",
        materials: {
          shadingMaterial: { type: shadingType },
          contourMaterial: { type: "ElevationContour" },
        },
        components: {
          diffuse:
            "contourMaterial.alpha == 0.0 ? shadingMaterial.diffuse : contourMaterial.diffuse",
          alpha: "max(contourMaterial.alpha, shadingMaterial.alpha)",
        },
      },
      translucent: false,
    });

    Object.keys(shadingMaterial.uniforms).forEach(function (key) {
      composite.materials.shadingMaterial.uniforms[key] =
        shadingMaterial.uniforms[key];
    });
    applyContourUniforms(composite.materials.contourMaterial);
    return composite;
  }

  function applyGlobeMaterial() {
    if (!viewer) {
      return;
    }

    readVisualizeControls();
    syncVisualizeOptionVisibility();

    const globe = viewer.scene.globe;
    const mode = visualizeState.shadingMode;
    const hasContour = visualizeState.elevationContour;
    const hasShading = mode !== "none";

    if (!hasShading && !hasContour) {
      globe.material = undefined;
      setBaseImageryAlpha(1.0);
      updateLegendDisplay();
      document.getElementById("visualizeStatus").innerHTML = "";
      return;
    }

    if (mode === "elevation" && visualizeState.maxHeight <= visualizeState.minHeight) {
      showToast("最高高程必须大于最低高程", "error");
      return;
    }

    let material;
    if (hasShading && hasContour) {
      const shading = createShadingMaterial(mode);
      material = createCompositeShadingContour(shading.type, shading.material);
    } else if (hasShading) {
      material = createShadingMaterial(mode).material;
    } else {
      material = Cesium.Material.fromType("ElevationContour");
      applyContourUniforms(material);
      material.uniforms.color = Cesium.Color.YELLOW.withAlpha(0.95);
    }

    globe.material = material;
    setBaseImageryAlpha(hasShading ? 0.25 : 0.85);
    updateLegendDisplay();

    const parts = [];
    if (hasShading) {
      parts.push(SHADING_LABELS[mode] || mode);
    }
    if (hasContour) {
      parts.push("等高线 " + visualizeState.contourSpacing + " m");
    }
    document.getElementById("visualizeStatus").innerHTML =
      '<p class="success-text">已启用: ' + parts.join(" + ") + "</p>";
  }

  function resetVisualization() {
    document.getElementById("optShadingMode").value = "none";
    document.getElementById("optElevationContour").checked = false;
    applyGlobeMaterial();
    showToast("已清除着色", "success");
  }

  async function autoComputeElevationRange() {
    if (!viewer || !currentTileset) {
      showToast("请先加载地形 tileset", "error");
      return;
    }

    const terrainUrl = "/tilesets/" + encodeURIComponent(currentTileset);
    const statusBox = document.getElementById("visualizeStatus");
    statusBox.innerHTML = '<p class="empty-hint">正在采样高程范围…</p>';

    try {
      await viewer.terrainProvider.readyPromise;

      let rectangle = null;
      const layerRes = await fetch(terrainUrl + "/layer.json");
      if (layerRes.ok) {
        const layer = await layerRes.json();
        rectangle = rectangleFromLayerJson(layer);
      }

      const positions = [];
      const grid = 10;
      if (rectangle) {
        for (let i = 0; i <= grid; i++) {
          for (let j = 0; j <= grid; j++) {
            const lon = Cesium.Math.lerp(rectangle.west, rectangle.east, i / grid);
            const lat = Cesium.Math.lerp(rectangle.south, rectangle.north, j / grid);
            positions.push(new Cesium.Cartographic(lon, lat));
          }
        }
      } else {
        const cam = viewer.camera.positionCartographic;
        const span = 0.05;
        for (let i = 0; i <= grid; i++) {
          for (let j = 0; j <= grid; j++) {
            positions.push(
              new Cesium.Cartographic(
                cam.longitude - span / 2 + (span * i) / grid,
                cam.latitude - span / 2 + (span * j) / grid,
              ),
            );
          }
        }
      }

      const sampled = await Cesium.sampleTerrainMostDetailed(
        viewer.terrainProvider,
        positions,
      );

      let minH = Infinity;
      let maxH = -Infinity;
      sampled.forEach(function (carto) {
        if (!Cesium.defined(carto.height) || !isFinite(carto.height)) {
          return;
        }
        minH = Math.min(minH, carto.height);
        maxH = Math.max(maxH, carto.height);
      });

      if (!isFinite(minH) || !isFinite(maxH) || minH === maxH) {
        throw new Error("无法从当前地形采样有效高程");
      }

      const pad = Math.max(10, (maxH - minH) * 0.08);
      visualizeState.minHeight = Math.floor(minH - pad);
      visualizeState.maxHeight = Math.ceil(maxH + pad);
      visualizeState.elevationRange = { min: minH, max: maxH };

      document.getElementById("optMinHeight").value = String(visualizeState.minHeight);
      document.getElementById("optMaxHeight").value = String(visualizeState.maxHeight);

      statusBox.innerHTML =
        '<p class="success-text">采样范围: ' +
        minH.toFixed(1) +
        " ~ " +
        maxH.toFixed(1) +
        " m（已加边距）</p>";
      applyGlobeMaterial();
      showToast("高程范围已自动计算", "success");
    } catch (err) {
      statusBox.innerHTML =
        '<p class="error-text">自动计算失败: ' + err.message + "</p>";
      showToast("自动计算失败: " + err.message, "error");
    }
  }

  function bindVisualizeUi() {
    document.querySelectorAll(".shading-btn[data-shading]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const kind = btn.dataset.shading;
        if (kind === "contour") {
          toggleContour();
        } else {
          setShadingMode(kind);
        }
      });
    });

    const visualizeInputs = [
      "optRampPreset",
      "optContourSpacing",
      "optContourWidth",
      "optMinHeight",
      "optMaxHeight",
    ];

    visualizeInputs.forEach(function (id) {
      const el = document.getElementById(id);
      el.addEventListener("change", function () {
        applyGlobeMaterial();
      });
      if (el.type === "number") {
        el.addEventListener("input", function () {
          applyGlobeMaterial();
        });
      }
    });

    document.getElementById("autoHeightRangeBtn").addEventListener("click", function () {
      autoComputeElevationRange();
    });

    document.getElementById("resetVisualizeBtn").addEventListener("click", function () {
      resetVisualization();
    });

    document.getElementById("optEnableLighting").addEventListener("change", function () {
      applyGlobeLighting();
    });

    document.querySelectorAll(".sidebar-section-header[data-section]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const section = btn.closest(".sidebar-section");
        if (!section) {
          return;
        }
        const open = section.classList.toggle("open");
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      });
    });

    const sidebar = document.getElementById("rightSidebar");
    const toggle = document.getElementById("rightSidebarToggle");
    toggle.addEventListener("click", function () {
      const collapsed = sidebar.classList.toggle("collapsed");
      toggle.textContent = collapsed ? "«" : "»";
      toggle.setAttribute(
        "aria-label",
        collapsed ? "展开右侧栏" : "收起右侧栏",
      );
    });

    syncVisualizeOptionVisibility();
  }

  function getQueryParam(name) {
    return new URLSearchParams(window.location.search).get(name);
  }

  function hasQueryParam(name) {
    return new URLSearchParams(window.location.search).has(name);
  }

  function parseNumber(value, fallback) {
    if (value === null || value === undefined || value === "") {
      return fallback;
    }
    const n = Number(value);
    return isFinite(n) ? n : fallback;
  }

  function showToast(message, type) {
    const toast = document.createElement("div");
    toast.className = "toast" + (type ? " " + type : "");
    toast.textContent = message;
    toastContainer.appendChild(toast);
    setTimeout(function () {
      toast.remove();
    }, 4000);
  }

  function showError(message) {
    errorBanner.textContent = message;
    errorBanner.style.display = "block";
  }

  function hideError() {
    errorBanner.style.display = "none";
    errorBanner.textContent = "";
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function formatDeg(value, posLabel, negLabel) {
    if (!isFinite(value)) {
      return "—";
    }
    const abs = Math.abs(value).toFixed(5);
    return (value >= 0 ? posLabel : negLabel) + abs + "°";
  }

  function formatEllipsoidHeight(meters) {
    if (!isFinite(meters)) {
      return "—";
    }
    if (meters < 0) {
      return meters.toFixed(1) + " m（椭球面下）";
    }
    if (Math.abs(meters) < 0.05) {
      return "0.0 m（椭球面）";
    }
    return meters.toFixed(1) + " m";
  }

  function renderCoords(extra) {
    coordsEl.textContent = extra || "tileset: —";
  }

  function geodeticTileRangeToRectangle(zoom, range) {
    const numX = Math.pow(2, zoom + 1);
    const numY = Math.pow(2, zoom);
    const west = -180 + range.startX * (360 / numX);
    const east = -180 + (range.endX + 1) * (360 / numX);
    const south = -90 + range.startY * (180 / numY);
    const north = -90 + (range.endY + 1) * (180 / numY);
    return Cesium.Rectangle.fromDegrees(west, south, east, north);
  }

  function rectangleFromLayerJson(layer) {
    const available = layer && layer.available;
    if (!available || !available.length) {
      return null;
    }

    let zoom = -1;
    let range = null;
    for (let z = available.length - 1; z >= 0; z--) {
      if (available[z] && available[z].length) {
        zoom = z;
        range = available[z][0];
        break;
      }
    }
    if (zoom < 0 || !range) {
      return null;
    }

    const projection = (layer.projection || "EPSG:4326").toUpperCase();
    if (projection !== "EPSG:4326") {
      const b = layer.bounds;
      if (b && b.length === 4) {
        return Cesium.Rectangle.fromDegrees(b[0], b[1], b[2], b[3]);
      }
      return null;
    }

    return geodeticTileRangeToRectangle(zoom, range);
  }

  function rectangleSpanMeters(rectangle) {
    const center = Cesium.Rectangle.center(rectangle);
    const widthRad = rectangle.east - rectangle.west;
    const heightRad = rectangle.north - rectangle.south;
    const widthM = widthRad * 6378137.0 * Math.cos(center.latitude);
    const heightM = heightRad * 6378137.0;
    return Math.max(Math.abs(widthM), Math.abs(heightM));
  }

  function defaultCameraHeightForSpan(spanMeters) {
    return Cesium.Math.clamp(spanMeters * 0.55, 3000, 120000);
  }

  function flyToView(lon, lat, cameraHeight, heading, pitch, animate) {
    const orientation = {
      heading: Cesium.Math.toRadians(heading),
      pitch: Cesium.Math.toRadians(pitch),
      roll: 0.0,
    };
    const destination = Cesium.Cartesian3.fromDegrees(lon, lat, cameraHeight);
    if (animate) {
      return viewer.camera.flyTo({
        destination: destination,
        orientation: orientation,
        duration: 1.2,
      });
    }
    viewer.camera.setView({
      destination: destination,
      orientation: orientation,
    });
  }

  function flyToRectangleTopDown(rectangle, heading, pitch, animate) {
    const orientation = {
      heading: Cesium.Math.toRadians(heading),
      pitch: Cesium.Math.toRadians(pitch),
      roll: 0.0,
    };
    if (animate) {
      return viewer.camera.flyTo({
        destination: rectangle,
        orientation: orientation,
        duration: 1.2,
      });
    }
    viewer.camera.setView({
      destination: rectangle,
      orientation: orientation,
    });
  }

  async function fitCameraToTileset(name, animate) {
    const heading = parseNumber(getQueryParam("heading"), 0.0);
    const pitch = parseNumber(getQueryParam("pitch"), -90.0);
    const terrainUrl = "/tilesets/" + encodeURIComponent(name);

    if (hasQueryParam("lon") || hasQueryParam("lat")) {
      flyToView(
        parseNumber(getQueryParam("lon"), 102.5),
        parseNumber(getQueryParam("lat"), 42.5),
        parseNumber(getQueryParam("height"), 8000.0),
        heading,
        pitch,
        animate,
      );
      return;
    }

    try {
      const response = await fetch(terrainUrl + "/layer.json");
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      const layer = await response.json();
      const rectangle = rectangleFromLayerJson(layer);
      if (!rectangle) {
        throw new Error("无法从 layer.json 推导覆盖范围");
      }

      if (hasQueryParam("height")) {
        const center = Cesium.Rectangle.center(rectangle);
        flyToView(
          Cesium.Math.toDegrees(center.longitude),
          Cesium.Math.toDegrees(center.latitude),
          parseNumber(getQueryParam("height"), 8000.0),
          heading,
          pitch,
          animate,
        );
      } else {
        flyToRectangleTopDown(rectangle, heading, pitch, animate);
      }
    } catch (err) {
      showError(
        "读取 layer.json 失败，使用默认视角: " +
          (err && err.message ? err.message : String(err)),
      );
      flyToView(102.5, 42.5, 8000.0, heading, pitch, animate);
    }
  }

  function applyTerrainExaggeration(value) {
    terrainExaggeration = Cesium.Math.clamp(value, 1, 50);
    if (viewer && viewer.scene && viewer.scene.globe) {
      viewer.scene.globe.terrainExaggeration = terrainExaggeration;
      viewer.scene.globe.terrainExaggerationRelativeHeight = 0.0;
    }
    return terrainExaggeration;
  }

  async function apiFetch(path, options) {
    const response = await fetch(API_BASE + path, options);
    let payload = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.indexOf("application/json") !== -1) {
      payload = await response.json();
    } else {
      payload = await response.text();
    }
    if (!response.ok) {
      const detail =
        payload && payload.detail
          ? payload.detail
          : typeof payload === "string"
            ? payload
            : "HTTP " + response.status;
      throw new Error(
        typeof detail === "string" ? detail : JSON.stringify(detail),
      );
    }
    return payload;
  }

  function setJobIdFields(jobId) {
    document.getElementById("jobIdInput").value = jobId;
    document.getElementById("publishJobIdInput").value = jobId;
  }

  function updateUrlTileset(name) {
    const url = new URL(window.location.href);
    url.searchParams.set("tileset", name);
    window.history.replaceState({}, "", url.toString());
  }

  function validateTilesetName(name) {
    const trimmed = (name || "").trim();
    if (!trimmed) {
      throw new Error("无效的 tileset 名称");
    }
    // Align with backend: reject path segments only; allow Unicode (e.g. 中文名).
    if (
      trimmed.includes("/") ||
      trimmed.includes("\\") ||
      trimmed.includes("..")
    ) {
      throw new Error("无效的 tileset 名称");
    }
  }

  async function loadTileset(name, options) {
    if (!viewer) {
      return;
    }

    validateTilesetName(name);
    const shouldFly = options && options.flyTo === true;
    const terrainUrl = "/tilesets/" + encodeURIComponent(name);

    hideError();
    setStatus("Loading terrain: " + name + "…");

    const provider = await Cesium.CesiumTerrainProvider.fromUrl(terrainUrl, {
      requestVertexNormals: true,
    });

    viewer.terrainProvider = provider;
    viewer.scene.globe.depthTestAgainstTerrain = true;
    viewer.scene.globe.showWaterEffect = false;

    currentTileset = name;
    updateUrlTileset(name);

    const exagLine =
      terrainExaggeration === 1.0
        ? ""
        : "\n垂直夸大: " + terrainExaggeration.toFixed(1) + "x";
    renderCoords("tileset: " + name + exagLine);
    setStatus("Loaded terrain: " + name);

    if (shouldFly) {
      await fitCameraToTileset(name, true);
    }

    if (visualizeState.shadingMode !== "none" || visualizeState.elevationContour) {
      applyGlobeMaterial();
    }
  }

  function clearLoadedTileset() {
    if (!viewer) {
      return;
    }
    viewer.terrainProvider = new Cesium.EllipsoidTerrainProvider();
    currentTileset = null;
    const url = new URL(window.location.href);
    url.searchParams.delete("tileset");
    window.history.replaceState({}, "", url.toString());
    renderCoords("tileset: —");
    setStatus("已下架当前地形");
  }

  async function unpublishTilesetByName(name) {
    validateTilesetName(name);
    if (
      !window.confirm(
        "确认下架 tileset「" + name + "」？\n仅移除发布链接，不删除源瓦片。",
      )
    ) {
      return;
    }

    try {
      await apiFetch("/tilesets/" + encodeURIComponent(name), {
        method: "DELETE",
      });
      if (currentTileset === name) {
        clearLoadedTileset();
      }
      showToast("已下架: " + name, "success");
      await refreshTilesets();
    } catch (err) {
      showToast("下架失败: " + err.message, "error");
    }
  }

  function formatZoomRange(minZoom, maxZoom) {
    if (minZoom == null || maxZoom == null) {
      return "—";
    }
    if (minZoom === maxZoom) {
      return String(minZoom);
    }
    return minZoom + "–" + maxZoom;
  }

  function formatClassForItem(item) {
    const raw = (item.format || "").toLowerCase();
    if (raw.indexOf("quantized-mesh") === 0 || raw.indexOf("mesh") !== -1) {
      return "format-mesh";
    }
    if (raw.indexOf("heightmap") === 0 || raw.indexOf("terrain") !== -1) {
      return "format-terrain";
    }
    return "";
  }

  function renderTilesetMeta(item) {
    const metaEl = document.createElement("div");
    metaEl.className = "meta";

    const row = document.createElement("div");
    row.className = "meta-row";

    function addTag(label, className) {
      const tag = document.createElement("span");
      tag.className = "meta-tag" + (className ? " " + className : "");
      tag.textContent = label;
      row.appendChild(tag);
    }

    addTag(
      "格式: " + (item.format_label || item.format || "—"),
      formatClassForItem(item),
    );
    addTag("坐标系: " + (item.crs || item.projection || "—"));
    addTag("层级: " + formatZoomRange(item.min_zoom, item.max_zoom));

    metaEl.appendChild(row);
    return metaEl;
  }

  function renderTilesetList(tilesets) {
    const listEl = document.getElementById("tilesetList");
    listEl.innerHTML = "";

    if (!tilesets.length) {
      listEl.innerHTML =
        '<p class="empty-hint">暂无已发布地形。请先完成任务并发布 tileset。</p>';
      return;
    }

    tilesets.forEach(function (item) {
      const li = document.createElement("li");
      li.className = "tileset-item";
      if (item.name === currentTileset) {
        li.classList.add("active");
      }

      const headerEl = document.createElement("div");
      headerEl.className = "tileset-header";

      const nameEl = document.createElement("div");
      nameEl.className = "name";
      nameEl.textContent = item.name;

      const unpublishBtn = document.createElement("button");
      unpublishBtn.type = "button";
      unpublishBtn.className = "tileset-unpublish-btn";
      unpublishBtn.textContent = "下架";
      unpublishBtn.title = "按名称下架（移除发布链接）";
      unpublishBtn.addEventListener("click", function (event) {
        event.stopPropagation();
        unpublishBtn.disabled = true;
        unpublishTilesetByName(item.name).finally(function () {
          unpublishBtn.disabled = false;
        });
      });

      headerEl.appendChild(nameEl);
      headerEl.appendChild(unpublishBtn);

      const urlEl = document.createElement("div");
      urlEl.className = "url";
      urlEl.textContent = item.terrain_url || "";

      li.appendChild(headerEl);
      li.appendChild(urlEl);
      li.appendChild(renderTilesetMeta(item));
      li.addEventListener("click", function () {
        loadTileset(item.name, { flyTo: true })
          .then(function () {
            renderTilesetList(tilesets);
            showToast("已加载地形: " + item.name, "success");
          })
          .catch(function (err) {
            showError("加载失败: " + err.message);
            showToast("加载失败: " + err.message, "error");
          });
      });

      listEl.appendChild(li);
    });
  }

  async function refreshTilesets() {
    const listEl = document.getElementById("tilesetList");
    listEl.innerHTML = '<p class="empty-hint">加载中…</p>';

    try {
      const data = await apiFetch("/tilesets");
      renderTilesetList(data.tilesets || []);
    } catch (err) {
      listEl.innerHTML =
        '<p class="error-text">获取图层列表失败: ' + err.message + "</p>";
    }
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function stopProgressWatch() {
    progressWatchGeneration += 1;
    progressWatchJobId = null;
    progressWatchMode = null;
    progressWsFailures = 0;
    if (progressReconnectTimer) {
      clearTimeout(progressReconnectTimer);
      progressReconnectTimer = null;
    }
    stopPolling();
    if (progressSocket) {
      const socket = progressSocket;
      progressSocket = null;
      try {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      } catch (_err) {
        /* ignore */
      }
    }
  }

  function jobWsUrl(jobId) {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return (
      proto +
      "//" +
      window.location.host +
      API_BASE +
      "/jobs/" +
      encodeURIComponent(jobId) +
      "/ws"
    );
  }

  function handleTerminalJob(job) {
    if (job.status === "completed") {
      showToast("任务已完成: " + job.job_id, "success");
      refreshTilesets();
      if (job.published && job.tileset_name) {
        loadTileset(job.tileset_name, { flyTo: true }).catch(function () {
          /* optional auto-preview */
        });
      }
      return true;
    }
    if (job.status === "failed") {
      showToast("任务失败: " + (job.error || job.job_id), "error");
      return true;
    }
    return false;
  }

  function applyJobUpdate(job) {
    renderJobDetail(job);
    if (handleTerminalJob(job)) {
      stopProgressWatch();
      return true;
    }
    return false;
  }

  function startPollingFallback(jobId) {
    stopPolling();
    progressWatchMode = "poll";
    progressWatchJobId = jobId;

    async function tick() {
      try {
        const job = await fetchJob(jobId);
        applyJobUpdate(job);
      } catch (err) {
        stopProgressWatch();
        document.getElementById("jobDetail").innerHTML =
          '<p class="error-text">查询失败: ' + err.message + "</p>";
        updatePublishControls(null);
      }
    }

    tick();
    pollTimer = setInterval(tick, POLL_INTERVAL_MS);
  }

  function scheduleWsReconnect(jobId, generation) {
    if (generation !== progressWatchGeneration || progressWatchJobId !== jobId) {
      return;
    }
    progressWsFailures += 1;
    if (progressWsFailures >= WS_MAX_FAILURES_BEFORE_POLL) {
      showToast("WebSocket 不可用，已回退到定时查询", "info");
      startPollingFallback(jobId);
      return;
    }
    const delay = Math.min(
      WS_RECONNECT_MAX_MS,
      WS_RECONNECT_BASE_MS * Math.pow(2, progressWsFailures - 1),
    );
    progressReconnectTimer = setTimeout(function () {
      progressReconnectTimer = null;
      if (generation !== progressWatchGeneration || progressWatchJobId !== jobId) {
        return;
      }
      fetchJob(jobId)
        .then(function (job) {
          if (applyJobUpdate(job)) return;
          openJobWebSocket(jobId, generation);
        })
        .catch(function () {
          openJobWebSocket(jobId, generation);
        });
    }, delay);
  }

  function openJobWebSocket(jobId, generation) {
    if (generation !== progressWatchGeneration || progressWatchJobId !== jobId) {
      return;
    }
    if (progressSocket) {
      try {
        progressSocket.onclose = null;
        progressSocket.close();
      } catch (_err) {
        /* ignore */
      }
      progressSocket = null;
    }

    progressWatchMode = "ws";
    let socket;
    try {
      socket = new WebSocket(jobWsUrl(jobId));
    } catch (_err) {
      scheduleWsReconnect(jobId, generation);
      return;
    }
    progressSocket = socket;

    socket.onmessage = function (event) {
      if (generation !== progressWatchGeneration) return;
      let job;
      try {
        job = JSON.parse(event.data);
      } catch (_err) {
        return;
      }
      if (job && job.detail && !job.job_id) {
        stopProgressWatch();
        document.getElementById("jobDetail").innerHTML =
          '<p class="error-text">查询失败: ' + job.detail + "</p>";
        updatePublishControls(null);
        return;
      }
      progressWsFailures = 0;
      applyJobUpdate(job);
    };

    socket.onerror = function () {
      /* onclose handles reconnect */
    };

    socket.onclose = function () {
      if (progressSocket === socket) {
        progressSocket = null;
      }
      if (generation !== progressWatchGeneration || progressWatchJobId !== jobId) {
        return;
      }
      if (progressWatchMode !== "ws") {
        return;
      }
      scheduleWsReconnect(jobId, generation);
    };
  }

  function startProgressWatch(jobId) {
    stopProgressWatch();
    setJobIdFields(jobId);
    progressWatchJobId = jobId;
    progressWsFailures = 0;
    const generation = progressWatchGeneration;

    if (typeof WebSocket === "undefined") {
      startPollingFallback(jobId);
      return;
    }
    openJobWebSocket(jobId, generation);
  }

  function statusBadgeClass(status) {
    return "status-badge " + (status || "queued");
  }

  function clampPercent(value) {
    const num = Number(value);
    if (Number.isNaN(num)) return 0;
    return Math.max(0, Math.min(100, num));
  }

  function formatProgressPercent(value) {
    const percent = clampPercent(value);
    return (Math.round(percent * 10) / 10).toFixed(percent % 1 === 0 ? 0 : 1) + "%";
  }

  function progressPhaseLabel(phase) {
    const labels = {
      queued: "排队中",
      initializing: "初始化",
      gdal_preprocess: "预处理",
      ctb_tile: "切片",
      register_tileset: "注册发布",
      done: "完成",
      preprocessing: "预处理",
      tiling: "切片",
      publishing: "发布",
      failed: "失败",
    };
    return labels[phase] || phase || "进度";
  }

  function clearJobProgress() {
    const progressEl = document.getElementById("jobProgress");
    const fillEl = document.getElementById("jobProgressFill");
    const barEl = document.getElementById("jobProgressBar");
    progressEl.hidden = true;
    fillEl.style.width = "0%";
    fillEl.className = "job-progress-fill";
    barEl.setAttribute("aria-valuenow", "0");
    document.getElementById("jobProgressPercent").textContent = "0%";
    document.getElementById("jobProgressLabel").textContent = "进度";
    document.getElementById("jobProgressMessage").textContent = "";
  }

  function formatElapsed(seconds) {
    if (seconds == null || Number.isNaN(Number(seconds))) return "—";
    const total = Math.max(0, Math.floor(Number(seconds)));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) {
      return h + "小时" + m + "分" + s + "秒";
    }
    if (m > 0) {
      return m + "分" + s + "秒";
    }
    return s + "秒";
  }

  function renderJobProgress(job) {
    const progressEl = document.getElementById("jobProgress");
    const fillEl = document.getElementById("jobProgressFill");
    const barEl = document.getElementById("jobProgressBar");
    const progress = job && job.progress ? job.progress : null;

    if (!progress && !job) {
      clearJobProgress();
      return;
    }

    let percent = progress ? clampPercent(progress.percent) : 0;
    if (job.status === "completed") percent = 100;
    if (job.status === "queued" && !progress) percent = 0;

    progressEl.hidden = false;
    fillEl.style.width = percent + "%";
    fillEl.className =
      "job-progress-fill" +
      (job.status === "failed"
        ? " is-failed"
        : job.status === "completed"
          ? " is-completed"
          : "");
    barEl.setAttribute("aria-valuenow", String(Math.round(percent)));
    document.getElementById("jobProgressPercent").textContent =
      formatProgressPercent(percent);
    document.getElementById("jobProgressLabel").textContent = "进度";
    document.getElementById("jobProgressMessage").textContent = "";
  }

  function updatePublishControls(job, options) {
    lastJobDetail = job || null;
    const publishBtn = document.getElementById("publishJobBtn");
    const unpublishBtn = document.getElementById("unpublishJobBtn");
    const jobIdPresent = !!(options && options.jobIdPresent);

    if (!job) {
      // Redis miss: still allow job-scoped publish/unpublish (disk fallback).
      publishBtn.disabled = !jobIdPresent;
      unpublishBtn.disabled = !jobIdPresent;
      return;
    }

    publishBtn.disabled = !(job.status === "completed" && !job.published);
    unpublishBtn.disabled = !job.published;
  }

  function renderJobDetail(job) {
    const detailEl = document.getElementById("jobDetail");

    renderJobProgress(job);

    const phase = (job.progress && job.progress.phase) || job.stage || job.status;
    const lines = [
      ["状态", job.status],
      ["阶段", progressPhaseLabel(phase)],
      ["耗时", formatElapsed(job.elapsed_seconds)],
    ];

    detailEl.innerHTML = lines
      .map(function (pair) {
        return (
          "<dt>" +
          pair[0] +
          '</dt><dd><span class="' +
          (pair[0] === "状态" ? statusBadgeClass(job.status) : "") +
          '">' +
          pair[1] +
          "</span></dd>"
        );
      })
      .join("");

    updatePublishControls(job);
  }

  async function fetchJob(jobId) {
    return apiFetch("/jobs/" + encodeURIComponent(jobId));
  }

  async function lookupJob() {
    const jobId = document.getElementById("jobIdInput").value.trim();
    if (!jobId) {
      showToast("请输入任务 ID", "error");
      return;
    }
    startProgressWatch(jobId);
  }

  async function refreshJobOnce(jobId) {
    const job = await fetchJob(jobId);
    renderJobDetail(job);
    return job;
  }

  async function publishJob() {
    const jobId = document.getElementById("publishJobIdInput").value.trim();
    const tilesetName = document.getElementById("tilesetNameInput").value.trim();
    const statusBox = document.getElementById("publishStatus");
    const publishBtn = document.getElementById("publishJobBtn");

    if (!jobId) {
      showToast("请输入任务 ID", "error");
      return;
    }

    publishBtn.disabled = true;
    statusBox.innerHTML = '<p class="empty-hint">发布中…</p>';

    try {
      const body = tilesetName ? { tileset_name: tilesetName } : {};
      const job = await apiFetch("/jobs/" + encodeURIComponent(jobId) + "/publish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      setJobIdFields(jobId);
      renderJobDetail(job);
      statusBox.innerHTML =
        '<p class="success-text">已发布 tileset: ' +
        (job.tileset_name || jobId) +
        "</p>";
      showToast("发布成功", "success");
      refreshTilesets();
    } catch (err) {
      statusBox.innerHTML =
        '<p class="error-text">发布失败: ' + err.message + "</p>";
      showToast("发布失败: " + err.message, "error");
      try {
        await refreshJobOnce(jobId);
      } catch (_) {
        updatePublishControls(lastJobDetail);
      }
    }
  }

  async function unpublishJob() {
    const jobId = document.getElementById("publishJobIdInput").value.trim();
    const statusBox = document.getElementById("publishStatus");
    const unpublishBtn = document.getElementById("unpublishJobBtn");

    if (!jobId) {
      showToast("请输入任务 ID", "error");
      return;
    }

    unpublishBtn.disabled = true;
    statusBox.innerHTML = '<p class="empty-hint">下架中…</p>';

    try {
      const job = await apiFetch("/jobs/" + encodeURIComponent(jobId) + "/publish", {
        method: "DELETE",
      });

      setJobIdFields(jobId);
      renderJobDetail(job);
      statusBox.innerHTML = '<p class="success-text">已下架 tileset</p>';
      showToast("下架成功", "success");
      refreshTilesets();
    } catch (err) {
      statusBox.innerHTML =
        '<p class="error-text">下架失败: ' + err.message + "</p>";
      showToast("下架失败: " + err.message, "error");
      try {
        await refreshJobOnce(jobId);
      } catch (_) {
        updatePublishControls(lastJobDetail);
      }
    }
  }

  function readOptionalInt(id) {
    const el = document.getElementById(id);
    if (!el) {
      return undefined;
    }
    const raw = el.value.trim();
    if (!raw) {
      return undefined;
    }
    const value = parseInt(raw, 10);
    return Number.isNaN(value) ? undefined : value;
  }

  function collectJobOptions() {
    const preprocess = {
      target_crs:
        document.getElementById("optTargetCrs").value.trim() || "EPSG:4326",
      build_overviews: document.getElementById("optBuildOverviews").checked,
      block_size: parseInt(document.getElementById("optBlockSize").value, 10) || 256,
    };

    const ctb_options = {
      output_format: document.getElementById("optOutputFormat").value,
      profile: document.getElementById("optProfile").value,
      end_zoom: readOptionalInt("optEndZoom") ?? 0,
    };

    const startZoom = readOptionalInt("optStartZoom");
    if (startZoom !== undefined) {
      ctb_options.start_zoom = startZoom;
    }

    const publish = {
      auto_publish: document.getElementById("optAutoPublish").checked,
    };

    const tilesetName = document.getElementById("optTilesetName").value.trim();
    if (tilesetName) {
      publish.tileset_name = tilesetName;
    }

    return { preprocess: preprocess, ctb_options: ctb_options, publish: publish };
  }

  function afterJobSubmitted(jobId, message) {
    document.getElementById("submitStatus").innerHTML =
      '<p class="success-text">' + message + "，任务 ID: " + jobId + "</p>";
    showToast(message, "success");
    setJobIdFields(jobId);
    openPanel("progress");
    startProgressWatch(jobId);
  }

  async function submitUpload() {
    const fileInput = document.getElementById("uploadFile");
    const submitBtn = document.getElementById("uploadSubmitBtn");
    const statusBox = document.getElementById("submitStatus");

    if (!fileInput.files || !fileInput.files[0]) {
      showToast("请选择 GeoTIFF 文件", "error");
      return;
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    const opts = collectJobOptions();
    formData.append("preprocess_json", JSON.stringify(opts.preprocess));
    formData.append("ctb_options_json", JSON.stringify(opts.ctb_options));
    formData.append("publish_json", JSON.stringify(opts.publish));

    submitBtn.disabled = true;
    statusBox.innerHTML = '<p class="empty-hint">上传中，请稍候…</p>';

    try {
      const result = await apiFetch("/jobs/upload", {
        method: "POST",
        body: formData,
      });
      afterJobSubmitted(result.job_id, "上传成功，已开始处理");
    } catch (err) {
      statusBox.innerHTML =
        '<p class="error-text">上传失败: ' + err.message + "</p>";
      showToast("上传失败: " + err.message, "error");
    } finally {
      submitBtn.disabled = false;
    }
  }

  function formatFileSize(bytes) {
    if (bytes == null) {
      return "";
    }
    if (bytes < 1024) {
      return bytes + " B";
    }
    if (bytes < 1024 * 1024) {
      return (bytes / 1024).toFixed(1) + " KB";
    }
    if (bytes < 1024 * 1024 * 1024) {
      return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  }

  async function submitWorkspaceJob() {
    const submitBtn = document.getElementById("workspaceSubmitBtn");
    const statusBox = document.getElementById("submitStatus");

    if (!selectedWorkspaceFile) {
      showToast("请选择服务器上的 DEM 文件", "error");
      return;
    }

    submitBtn.disabled = true;
    statusBox.innerHTML = '<p class="empty-hint">提交任务中…</p>';

    try {
      const opts = collectJobOptions();
      const result = await apiFetch("/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          input_path: selectedWorkspaceFile.absolute_path,
          preprocess: opts.preprocess,
          ctb_options: opts.ctb_options,
          publish: opts.publish,
        }),
      });
      afterJobSubmitted(result.job_id, "任务已提交");
    } catch (err) {
      statusBox.innerHTML =
        '<p class="error-text">提交失败: ' + err.message + "</p>";
      showToast("提交失败: " + err.message, "error");
    } finally {
      submitBtn.disabled = false;
    }
  }

  function renderWorkspaceBreadcrumb(listing) {
    const breadcrumbEl = document.getElementById("workspaceBreadcrumb");
    breadcrumbEl.innerHTML = "";

    const rootBtn = document.createElement("button");
    rootBtn.type = "button";
    rootBtn.textContent = "工作区";
    rootBtn.addEventListener("click", function () {
      loadWorkspace("");
    });
    breadcrumbEl.appendChild(rootBtn);

    if (!listing.relative_path) {
      return;
    }

    const parts = listing.relative_path.split("/");
    parts.forEach(function (_part, index) {
      const segmentPath = parts.slice(0, index + 1).join("/");
      const sep = document.createElement("span");
      sep.textContent = "/";
      sep.style.color = "#777";
      breadcrumbEl.appendChild(sep);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = parts[index];
      btn.addEventListener("click", function () {
        loadWorkspace(segmentPath);
      });
      breadcrumbEl.appendChild(btn);
    });
  }

  function renderWorkspaceList(listing) {
    const listEl = document.getElementById("workspaceList");
    listEl.innerHTML = "";
    selectedWorkspaceFile = null;
    document.getElementById("workspaceSubmitBtn").disabled = true;

    document.getElementById("workspacePath").textContent =
      "当前目录: " + listing.absolute_path;

    renderWorkspaceBreadcrumb(listing);

    if (!listing.entries.length) {
      listEl.innerHTML = '<li class="empty-hint">此目录下没有可浏览的内容</li>';
      return;
    }

    listing.entries.forEach(function (entry) {
      const li = document.createElement("li");
      li.className = "workspace-item";

      const label = document.createElement("div");
      label.className = "label";
      label.textContent =
        (entry.entry_type === "directory" ? "📁 " : "📄 ") + entry.name;

      const meta = document.createElement("div");
      meta.className = "meta";
      if (entry.entry_type === "directory") {
        meta.textContent = "目录";
      } else if (entry.selectable) {
        const sizeLabel = formatFileSize(entry.size_bytes);
        meta.textContent = sizeLabel ? sizeLabel : "DEM";
      } else {
        meta.textContent = "不可选";
        li.classList.add("disabled");
      }

      li.appendChild(label);
      li.appendChild(meta);

      if (entry.entry_type === "directory") {
        li.addEventListener("click", function () {
          loadWorkspace(entry.relative_path);
        });
      } else if (entry.selectable) {
        li.addEventListener("click", function () {
          listEl.querySelectorAll(".workspace-item.selected").forEach(function (node) {
            node.classList.remove("selected");
          });
          li.classList.add("selected");
          selectedWorkspaceFile = entry;
          document.getElementById("workspaceSubmitBtn").disabled = false;
        });
      }

      listEl.appendChild(li);
    });
  }

  async function loadWorkspace(relativePath) {
    const listEl = document.getElementById("workspaceList");
    listEl.innerHTML = '<li class="empty-hint">加载中…</li>';
    workspaceRelativePath = relativePath;

    try {
      const query = relativePath
        ? "?path=" + encodeURIComponent(relativePath)
        : "";
      const listing = await apiFetch("/workspace" + query);
      renderWorkspaceList(listing);
    } catch (err) {
      listEl.innerHTML =
        '<li class="error-text">加载失败: ' + err.message + "</li>";
    }
  }

  function setSubmitTab(tabName) {
    activeSubmitTab = tabName;
    document.querySelectorAll(".panel-subtab[data-submit-tab]").forEach(function (tab) {
      tab.classList.toggle("active", tab.dataset.submitTab === tabName);
    });
    document.getElementById("uploadTabPanel").classList.toggle(
      "active",
      tabName === "upload",
    );
    document.getElementById("workspaceTabPanel").classList.toggle(
      "active",
      tabName === "workspace",
    );

    if (tabName === "workspace") {
      loadWorkspace(workspaceRelativePath);
    }
  }

  function preparePublishPanel() {
    const queryJobId = document.getElementById("jobIdInput").value.trim();
    const publishJobIdInput = document.getElementById("publishJobIdInput");
    const publishJobId = publishJobIdInput.value.trim();
    if (queryJobId && !publishJobId) {
      publishJobIdInput.value = queryJobId;
    }
    const resolvedId = publishJobIdInput.value.trim();
    if (lastJobDetail && lastJobDetail.job_id === resolvedId) {
      updatePublishControls(lastJobDetail);
    } else if (resolvedId) {
      refreshJobOnce(resolvedId).catch(function () {
        updatePublishControls(null, { jobIdPresent: true });
      });
    } else {
      updatePublishControls(null);
    }
  }

  function setNavActive(panelName) {
    document.querySelectorAll(".side-nav-item[data-panel]").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.panel === panelName);
    });
  }

  function openPanel(name) {
    Object.keys(panels).forEach(function (key) {
      panels[key].classList.toggle("open", key === name);
    });
    overlay.classList.add("open");
    activePanel = name;
    setNavActive(name);

    if (name === "layers") {
      refreshTilesets();
    }
    if (name === "ingest" && activeSubmitTab === "workspace") {
      loadWorkspace(workspaceRelativePath);
    }
    if (name === "publish") {
      preparePublishPanel();
    }
  }

  function closePanel() {
    Object.keys(panels).forEach(function (key) {
      panels[key].classList.remove("open");
    });
    overlay.classList.remove("open");
    activePanel = null;
    setNavActive(null);
  }

  function bindUi() {
    document.querySelectorAll(".side-nav-item[data-panel]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const panelName = btn.dataset.panel;
        if (activePanel === panelName) {
          closePanel();
        } else {
          openPanel(panelName);
        }
      });
    });

    document.getElementById("refreshTilesetsBtn").addEventListener("click", function () {
      refreshTilesets();
    });

    document.getElementById("lookupJobBtn").addEventListener("click", function () {
      lookupJob();
    });

    document.getElementById("uploadSubmitBtn").addEventListener("click", function () {
      submitUpload();
    });

    document.getElementById("workspaceSubmitBtn").addEventListener("click", function () {
      submitWorkspaceJob();
    });

    document.querySelectorAll(".panel-subtab[data-submit-tab]").forEach(function (tab) {
      tab.addEventListener("click", function () {
        setSubmitTab(tab.dataset.submitTab);
      });
    });

    document.getElementById("publishJobBtn").addEventListener("click", function () {
      publishJob();
    });

    document.getElementById("unpublishJobBtn").addEventListener("click", function () {
      unpublishJob();
    });

    overlay.addEventListener("click", closePanel);

    document.querySelectorAll(".panel-close").forEach(function (btn) {
      btn.addEventListener("click", closePanel);
    });

    document.getElementById("jobIdInput").addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        lookupJob();
      }
    });

    document.getElementById("publishJobIdInput").addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        publishJob();
      }
    });

    bindVisualizeUi();
  }

  async function initViewer() {
    let baseProvider;
    if (Cesium.ArcGisMapServerImageryProvider.fromUrl) {
      baseProvider = await Cesium.ArcGisMapServerImageryProvider.fromUrl(
        "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer",
      );
    } else {
      baseProvider = new Cesium.UrlTemplateImageryProvider({
        url:
          "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        maximumLevel: 19,
      });
    }

    viewer = new Cesium.Viewer("cesiumContainer", {
      baseLayer: new Cesium.ImageryLayer(baseProvider),
      terrainProvider: new Cesium.EllipsoidTerrainProvider(),
      animation: false,
      timeline: false,
      baseLayerPicker: false,
      geocoder: false,
      homeButton: false,
      sceneModePicker: false,
      navigationHelpButton: false,
      infoBox: false,
      selectionIndicator: false,
    });

    const lightingParam = getQueryParam("lighting");
    if (lightingParam !== null) {
      document.getElementById("optEnableLighting").checked =
        lightingParam === "1" || lightingParam.toLowerCase() === "true";
    }
    applyGlobeLighting();
    applyTerrainExaggeration(parseNumber(getQueryParam("exaggeration"), 1.0));

    const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    handler.setInputAction(function (movement) {
      let cartesian = viewer.scene.pickPosition(movement.endPosition);
      if (!Cesium.defined(cartesian)) {
        const ray = viewer.camera.getPickRay(movement.endPosition);
        cartesian = viewer.scene.globe.pick(ray, viewer.scene);
      }

      const camH = viewer.camera.positionCartographic.height;

      if (!Cesium.defined(cartesian)) {
        const exagLine =
          terrainExaggeration === 1.0
            ? ""
            : "\n垂直夸大: " + terrainExaggeration.toFixed(1) + "x";
        renderCoords(
          (currentTileset ? "tileset: " + currentTileset : "tileset: —") +
            "\n经度: —\n纬度: —\n椭球高: —\n相机高: " +
            (isFinite(camH) ? camH.toFixed(0) + " m" : "—") +
            exagLine,
        );
        return;
      }

      const carto = Cesium.Cartographic.fromCartesian(cartesian);
      const lon = Cesium.Math.toDegrees(carto.longitude);
      const lat = Cesium.Math.toDegrees(carto.latitude);
      const exagLine =
        terrainExaggeration === 1.0
          ? ""
          : "\n垂直夸大: " + terrainExaggeration.toFixed(1) + "x";

      renderCoords(
        (currentTileset ? "tileset: " + currentTileset : "tileset: —") +
          "\n经度: " +
          formatDeg(lon, "E", "W") +
          "\n纬度: " +
          formatDeg(lat, "N", "S") +
          "\n椭球高: " +
          formatEllipsoidHeight(carto.height) +
          "\n相机高: " +
          (isFinite(camH) ? camH.toFixed(0) + " m" : "—") +
          exagLine,
      );
    }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
  }

  async function boot() {
    bindUi();
    await initViewer();

    const params = new URLSearchParams(window.location.search);
    const jobId = params.get("job");
    const tileset = params.get("tileset");

    setStatus("请通过「数据接入」提交 DEM，「图层管理」选择预览");

    if (jobId) {
      openPanel("progress");
      setJobIdFields(jobId);
      startProgressWatch(jobId);
    }

    if (tileset) {
      try {
        validateTilesetName(tileset);
        await loadTileset(tileset, { flyTo: true });
      } catch (err) {
        showError("无法加载 tileset: " + err.message);
      }
    }
  }

  boot().catch(function (err) {
    setStatus("Preview init failed: " + err.message);
    showToast("初始化失败: " + err.message, "error");
  });
})();
