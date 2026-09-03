/* Consumer adapter verified against the pinned CesiumJS 1.120 build.
 * A hemisphere can have no finite horizon point. Keep the binary untouched;
 * let Cesium use its documented undefined-point / bounding-volume fallback.
 */
(function (root) {
  "use strict";

  function attachHorizonGuard(provider, Cesium) {
    if (Cesium.VERSION !== "1.120") {
      throw new Error("Terrain horizon adapter requires validation for this Cesium version");
    }
    if (provider._oceanHorizonGuard) return provider;
    const request = provider.requestTileGeometry.bind(provider);
    provider.requestTileGeometry = function () {
      const pending = request.apply(null, arguments);
      // undefined signals scheduler throttling; it must not become a Promise.
      if (pending === undefined) return undefined;
      return Promise.resolve(pending).then(function (data) {
        if (data instanceof Cesium.QuantizedMeshTerrainData) {
          const point = data._horizonOcclusionPoint;
          if (point && ![point.x, point.y, point.z].every(Number.isFinite)) {
            // Verified 1.120 private field: createMesh falls back to this field
            // only if the worker could not compute a valid under-ellipsoid point.
            data._horizonOcclusionPoint = undefined;
          }
        }
        return data;
      });
    };
    provider._oceanHorizonGuard = true;
    return provider;
  }

  root.OceanTerrainCompatibility = { attachHorizonGuard: attachHorizonGuard };
})(globalThis);
