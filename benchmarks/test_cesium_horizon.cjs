/* Run with Node 22 and the official pinned Cesium 1.120 browser bundle. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

async function main() {
  const path = process.argv[2] || 'data/cesium_reference/Cesium.js';
  const bundle = fs.readFileSync(path);
  const context = {console, URL, setTimeout, clearTimeout, performance, atob, btoa,
    TextDecoder, TextEncoder, TransformStream, ReadableStream, WritableStream, AbortController};
  context.global = context;
  vm.createContext(context);
  vm.runInContext(bundle.toString(), context);
  vm.runInContext(fs.readFileSync('scripts/preview/terrain_compat.js', 'utf8'), context);
  const C = context.Cesium;
  assert.equal(C.VERSION, '1.120');
  const guard = context.OceanTerrainCompatibility.attachHorizonGuard;
  const occluder = new C.EllipsoidalOccluder(C.Ellipsoid.WGS84);
  const rootPoint = occluder.computeHorizonCullingPointFromRectangle(
    C.Rectangle.fromDegrees(-180,-90,0,90), C.Ellipsoid.WGS84);
  assert.equal(rootPoint, undefined);
  function terrain(point) {
    return new C.QuantizedMeshTerrainData({minimumHeight:0, maximumHeight:0,
      boundingSphere:new C.BoundingSphere(new C.Cartesian3(0,0,0),6378137),
      horizonOcclusionPoint:point, quantizedVertices:new Uint16Array(9),
      indices:new Uint16Array([0,1,2]), westIndices:[0], southIndices:[0],
      eastIndices:[1], northIndices:[2], westSkirtHeight:0, southSkirtHeight:0,
      eastSkirtHeight:0, northSkirtHeight:0});
  }
  const invalid = terrain(new C.Cartesian3(NaN,Infinity,NaN));
  const finite = terrain(new C.Cartesian3(1,2,3));
  let next = invalid;
  let seen;
  const provider = {requestTileGeometry(...args) { seen=args; return Promise.resolve(next); }};
  guard(provider,C);
  const wrapped = provider.requestTileGeometry;
  assert.equal(guard(provider,C).requestTileGeometry, wrapped);
  assert.equal(await provider.requestTileGeometry(0,0,0), invalid);
  assert.equal(invalid._horizonOcclusionPoint, undefined);
  assert.deepEqual(seen,[0,0,0]);
  next = finite;
  assert.equal((await provider.requestTileGeometry())._horizonOcclusionPoint, finite._horizonOcclusionPoint);
  assert.equal(finite._horizonOcclusionPoint.x,1);
  assert.equal(guard({requestTileGeometry() {return undefined;}},C).requestTileGeometry(),undefined);
  await assert.rejects(guard({requestTileGeometry() {return Promise.reject(new Error('network'));}},C).requestTileGeometry(),/network/);
  assert.throws(()=>guard(provider,{VERSION:'other'}),/requires validation/);
  console.log(JSON.stringify({cesium:C.VERSION, bundle_sha256:crypto.createHash('sha256').update(bundle).digest('hex'),
    hemisphere_has_no_horizon:rootPoint===undefined, adapter_checks:'passed',
    browser_rendering_verified:false}));
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
