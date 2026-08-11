#!/usr/bin/env python3
import argparse
import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

from live_calibrate import fetch_pair
from live_depth import StereoDepthEstimator, make_dashboard, obstacle_state

HOST = "127.0.0.1"
PORT = 8081
allowed_clients = {"127.0.0.1", "::1"}


def parse_args():
    parser = argparse.ArgumentParser(description="RoverVision web dashboard")
    parser.add_argument("--host", default=HOST, help="address to listen on")
    parser.add_argument(
        "--allow-client",
        action="append",
        default=[],
        help="client IP allowed to view the camera result (repeatable)",
    )
    return parser.parse_args()


class LatestDepth:
    def __init__(self):
        self.lock = threading.Lock()
        self.jpeg = None
        self.stats = {"status": "waiting for iPhone frames"}

    def update(self, jpeg, stats):
        with self.lock:
            self.jpeg = jpeg
            self.stats = stats

    def snapshot(self):
        with self.lock:
            return self.jpeg, dict(self.stats)


latest = LatestDepth()


def process_depth():
    estimator = StereoDepthEstimator("calibration/stereo_calibration.npz")
    last_sequence = None
    frame_count = 0
    fps = 0.0
    started = time.monotonic()
    while True:
        try:
            sequence, _, wide, ultra, _, _ = fetch_pair("http://127.0.0.1:8080", timeout=0.8)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            latest.update(None, {"status": f"waiting: {error}"})
            time.sleep(0.1)
            continue
        if sequence == last_sequence:
            time.sleep(0.02)
            continue
        last_sequence = sequence

        rectified_wide, rectified_ultra, disparity, depth, valid = estimator.estimate(wide, ultra)
        traversability = estimator.analyze_traversability(depth, valid)
        frame_count += 1
        elapsed = time.monotonic() - started
        if elapsed >= 1.0:
            fps = frame_count / elapsed
            frame_count = 0
            started = time.monotonic()
        dashboard, distance, valid_ratio = make_dashboard(
            rectified_wide.copy(), rectified_ultra, depth, valid, fps, traversability
        )
        ok, encoded = cv2.imencode(".jpg", dashboard, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            continue
        latest.update(
            encoded.tobytes(),
            {
                "status": "running",
                "sequence": int(sequence),
                "center_distance_m": distance,
                "obstacle_state": obstacle_state(distance),
                "ground_plane_found": traversability["ground_plane_found"],
                "traversability_counts": traversability["counts"],
                "valid_depth_percent": round(valid_ratio * 100, 1),
                "processing_fps": round(fps, 1),
            },
        )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.client_address[0] not in allowed_clients:
            self.send_error(403, "This device is not allowed")
            return
        path = self.path.split("?", 1)[0]
        image, stats = latest.snapshot()
        if path == "/dashboard.jpg":
            if image is None:
                self.send_error(404, "No depth frame yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            try:
                self.wfile.write(image)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path == "/stats.json":
            payload = json.dumps(stats).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/":
            payload = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def log_message(self, format, *args):
        pass


PAGE = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RoverVision Live Depth</title>
<style>
body{margin:0;background:#0b0b0b;color:#fff;font-family:system-ui;text-align:center}
header{padding:14px}h1{margin:0;font-size:24px}#state{font-size:34px;font-weight:800;margin-top:8px}#stats,#terrain{color:#ddd;margin-top:4px}#terrain{font-weight:700}
img{width:min(100vw,1280px);height:auto;background:#000;display:block;margin:auto}
</style>
<header><h1>RoverVision Live Stereo Depth</h1><div id="state">WAITING</div><div id="stats">Waiting...</div><div id="terrain"></div></header>
<img id="view" alt="Waiting for depth frames">
<script>
async function refresh(){const t=Date.now();view.src='/dashboard.jpg?t='+t;
try{const s=await fetch('/stats.json?t='+t).then(r=>r.json());
if(s.status==='running'){
state.textContent=s.obstacle_state;state.style.color=s.obstacle_state==='OBSTACLE'?'#ff4242':s.obstacle_state==='CLEAR'?'#39e75f':'#ffae42';
stats.textContent=`Center: ${s.center_distance_m?.toFixed(2) ?? 'NO DEPTH'} m | Limit: 0.60 m | Valid: ${s.valid_depth_percent}% | ${s.processing_fps} fps`
const c=s.traversability_counts;terrain.textContent=`Ground: ${s.ground_plane_found?'FOUND':'NOT FOUND'} | Passable: ${c.PASSABLE} | Blocked: ${c.BLOCKED} | Unknown: ${c.UNKNOWN}`
}else{state.textContent='WAITING';state.style.color='#ffae42';stats.textContent=s.status}}catch(e){}}
setInterval(refresh,200);refresh();
</script>"""


if __name__ == "__main__":
    args = parse_args()
    allowed_clients.add(args.host)
    allowed_clients.update(args.allow_client)
    threading.Thread(target=process_depth, daemon=True).start()
    print(f"RoverVision depth dashboard: http://{args.host}:{PORT}", flush=True)
    print(f"Allowed clients: {', '.join(sorted(allowed_clients))}", flush=True)
    ThreadingHTTPServer((args.host, PORT), Handler).serve_forever()
