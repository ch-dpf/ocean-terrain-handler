"""Local-only test server; no application publishing or existing service changes."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path("/code")
TILES = Path("/data/tiling_production/canonical/current/s5e130/tiles")
OUTPUT = Path("/data/tiling_production/cesium_acceptance.json")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = self.path.split("?")[0]
        paths = {
            "/": ROOT / "benchmarks/cesium_acceptance.html",
            "/Cesium.js": ROOT / "data/cesium_reference/Cesium.js",
            "/adapter.js": ROOT / "scripts/preview/terrain_compat.js",
        }
        path = paths.get(url)
        if url.startswith("/tiles/"):
            path = (TILES / url[len("/tiles/") :]).resolve()
            if not path.is_relative_to(TILES.resolve()):
                path = None
        if path is None or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/javascript"
            if path.suffix == ".js"
            else "application/json"
            if path.suffix == ".json"
            else "application/vnd.quantized-mesh"
            if path.suffix == ".terrain"
            else "text/html",
        )
        if path.suffix == ".terrain":
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/results":
            self.send_error(404)
            return
        data = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 65536))
        result = json.loads(data)
        OUTPUT.write_text(json.dumps(result, indent=2))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8841), Handler).serve_forever()
