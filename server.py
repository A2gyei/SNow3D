"""Loopback-only local viewer and isolated AvaFrame task runner (stdlib server)."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlparse
import uuid

ROOT = Path(__file__).resolve().parent
RUN_LOCK = threading.Lock()
CURRENT = None
ZONES = ROOT / "data" / "analysis" / "jiangjunshan" / "release-zones.json"
TERRAINS = ("jiangjunshan", "parabola")


def load_zones():
    """Published release-zone screening for the Jiangjunshan terrain."""
    if not ZONES.exists():
        return {"model": None, "candidates": [], "missing": True}
    return json.loads(ZONES.read_text(encoding="utf-8"))


def simulate_command(job_id, terrain, zone, scenario, thickness):
    """Solver invocation for one job; built outside the worker thread so it is testable."""
    command = [sys.executable, "-B", "-u", str(ROOT / "scripts" / "simulate.py"),
               "--id", job_id, "--terrain", terrain, "--thickness", str(thickness)]
    if terrain == "jiangjunshan":
        command += ["--zone", zone, "--scenario", "ent"]
    else:
        command += ["--scenario", scenario]
    return command


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/jobs/"):
            super().log_message(fmt, *args)

    def json_response(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        # Local development: never let the browser reuse a stale bundle, texture
        # or manifest after the packer rewrites it.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/scenarios":
            index = ROOT / "data" / "public" / "index.json"
            return self.json_response(json.loads(index.read_text(encoding="utf-8")) if index.exists() else [])
        if route == "/api/status":
            return self.json_response({"engine": "AvaFrame com1DFA", "python": sys.version.split()[0],
                                       "project": str(ROOT), "activeJob": CURRENT,
                                       "zones": len(load_zones().get("candidates", [])),
                                       "ready": (ROOT / "node_modules" / "cesium" / "Build" / "Cesium" / "Cesium.js").exists()})
        if route == "/api/zones":
            return self.json_response(load_zones())
        if route.startswith("/api/jobs/"):
            job_id = route.rsplit("/", 1)[1]
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", job_id):
                return self.json_response({"error": "Invalid job id"}, 400)
            job_path = ROOT / "data" / "jobs" / f"{job_id}.json"
            if not job_path.exists():
                return self.json_response({"id": job_id, "phase": "queued", "message": "等待模型启动"})
            return self.json_response(json.loads(job_path.read_text(encoding="utf-8")))
        return super().do_GET()

    def do_POST(self):
        global CURRENT
        # Browser POSTs must be same-origin. The service binds exclusively to loopback.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self.json_response({"error": "Cross-origin requests are forbidden"}, 403)
        if urlparse(self.path).path != "/api/run":
            return self.json_response({"error": "Not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("Invalid request size")
            params = json.loads(self.rfile.read(length))
            thickness = float(params.get("thickness", 1))
            terrain = params.get("terrain", "jiangjunshan")
            scenario = params.get("scenario", "null")
            zone = str(params.get("zone", ""))
            if not math.isfinite(thickness) or not 0.2 <= thickness <= 3:
                raise ValueError("雪厚范围为 0.2–3 米")
            if terrain not in TERRAINS:
                raise ValueError("地形必须为 jiangjunshan 或 parabola")
            if terrain == "jiangjunshan":
                # The simulation always runs on a terrain-derived release zone;
                # without a choice the highest ranked candidate is used.
                available = [item["id"] for item in load_zones().get("candidates", [])]
                if not available:
                    raise ValueError("尚未识别起崩区，请先运行 scripts/release_zones.py")
                if not zone:
                    zone = available[0]
                if zone not in available:
                    raise ValueError(f"未识别的起崩区: {zone}")
                scenario = "ent"
            elif scenario not in ("null", "res"):
                raise ValueError("情景必须为 null 或 res")
        except (ValueError, TypeError, AttributeError) as exc:
            return self.json_response({"error": str(exc)}, 400)
        if not RUN_LOCK.acquire(blocking=False):
            return self.json_response({"error": "已有模拟正在运行，请等待完成", "id": CURRENT}, 409)
        job_id = "run_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        CURRENT = job_id
        command = simulate_command(job_id, terrain, zone, scenario, thickness)

        def task():
            global CURRENT
            try:
                log_dir = ROOT / "logs"
                log_dir.mkdir(exist_ok=True)
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                with (log_dir / f"{job_id}.log").open("w", encoding="utf-8") as log:
                    result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                job_path = ROOT / "data" / "jobs" / f"{job_id}.json"
                status = json.loads(job_path.read_text(encoding="utf-8")) if job_path.exists() else {}
                if result.returncode and status.get("phase") != "failed":
                    job_path.parent.mkdir(parents=True, exist_ok=True)
                    job_path.write_text(json.dumps({"id": job_id, "phase": "failed",
                                                   "message": f"计算进程退出（{result.returncode}），请查看 logs/{job_id}.log"},
                                                  ensure_ascii=False), encoding="utf-8")
            except Exception as exc:
                job_path = ROOT / "data" / "jobs" / f"{job_id}.json"
                job_path.parent.mkdir(parents=True, exist_ok=True)
                job_path.write_text(json.dumps({"id": job_id, "phase": "failed", "message": str(exc)}, ensure_ascii=False), encoding="utf-8")
            finally:
                CURRENT = None
                RUN_LOCK.release()

        threading.Thread(target=task, daemon=True).start()
        self.json_response({"id": job_id, "phase": "queued"}, 202)

    def translate_path(self, path):
        path = unquote(urlparse(path).path)
        if path.startswith("/vendor/cesium/"):
            base, relative = ROOT / "node_modules" / "cesium" / "Build" / "Cesium", path[len("/vendor/cesium/"):]
        elif path.startswith("/data/"):
            base, relative = ROOT / "data" / "public", path[len("/data/"):]
        else:
            base, relative = ROOT / "web", path.lstrip("/") or "index.html"
        target = (base / relative).resolve()
        if not target.is_relative_to(base.resolve()):
            return str(ROOT / "web" / "_not_found_")
        return str(target)

    def list_directory(self, path):
        self.send_error(404, "Not found")
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Snowwork 3D: http://127.0.0.1:{args.port}", flush=True)
    if not args.no_browser:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
