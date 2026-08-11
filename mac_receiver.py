#!/usr/bin/env python3
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TCP_HOST = "0.0.0.0"
TCP_PORT = 5050
HTTP_HOST = "0.0.0.0"
HTTP_PORT = 8080
HEADER = struct.Struct("!4sIQQII")
MAX_JPEG_BYTES = 5 * 1024 * 1024


class LatestPair:
    def __init__(self):
        self.lock = threading.Lock()
        self.wide = None
        self.ultra = None
        self.sequence = 0
        self.timestamp_us = 0
        self.received_at = 0.0
        self.pairs = 0
        self.started_at = time.monotonic()

    def update(self, sequence, timestamp_us, wide, ultra):
        with self.lock:
            self.wide = wide
            self.ultra = ultra
            self.sequence = sequence
            self.timestamp_us = timestamp_us
            self.received_at = time.time()
            self.pairs += 1

    def image(self, camera):
        with self.lock:
            return self.wide if camera == "wide" else self.ultra

    def snapshot(self):
        with self.lock:
            if self.wide is None or self.ultra is None:
                return None
            return self.sequence, self.timestamp_us, self.wide, self.ultra

    def stats(self):
        with self.lock:
            elapsed = max(0.001, time.monotonic() - self.started_at)
            return {
                "sequence": self.sequence,
                "timestamp_us": self.timestamp_us,
                "received_at": self.received_at,
                "pairs": self.pairs,
                "average_pairs_per_second": round(self.pairs / elapsed, 2),
            }


latest = LatestPair()


def receive_exact(connection, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("iPhone connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def handle_iphone(connection, address):
    print(f"iPhone connected: {address}", flush=True)
    try:
        while True:
            magic, version, sequence, timestamp_us, wide_size, ultra_size = HEADER.unpack(
                receive_exact(connection, HEADER.size)
            )
            if magic != b"MCAM" or version != 1:
                raise ValueError("invalid MultiCam packet header")
            if wide_size > MAX_JPEG_BYTES or ultra_size > MAX_JPEG_BYTES:
                raise ValueError("JPEG payload is too large")
            wide = receive_exact(connection, wide_size)
            ultra = receive_exact(connection, ultra_size)
            latest.update(sequence, timestamp_us, wide, ultra)
            if latest.pairs % 30 == 0:
                print(f"received {latest.pairs} synchronized pairs", flush=True)
    except (ConnectionError, OSError, ValueError) as error:
        print(f"iPhone disconnected: {error}", flush=True)
    finally:
        connection.close()


def run_tcp_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((TCP_HOST, TCP_PORT))
        server.listen()
        print(f"Frame receiver: tcp://{TCP_HOST}:{TCP_PORT}", flush=True)
        while True:
            connection, address = server.accept()
            threading.Thread(target=handle_iphone, args=(connection, address), daemon=True).start()


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/wide.jpg", "/ultra.jpg"):
            image = latest.image("wide" if path == "/wide.jpg" else "ultra")
            if image is None:
                self.send_error(404, "No frame received yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)
            return
        if path == "/pair.bin":
            pair = latest.snapshot()
            if pair is None:
                self.send_error(404, "No synchronized pair received yet")
                return
            sequence, timestamp_us, wide, ultra = pair
            payload = HEADER.pack(b"MCAM", 1, sequence, timestamp_us, len(wide), len(ultra)) + wide + ultra
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/stats.json":
            payload = json.dumps(latest.stats()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/":
            payload = DASHBOARD.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def log_message(self, format, *args):
        pass


DASHBOARD = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MultiCam Wi-Fi Receiver</title>
<style>
body{font-family:system-ui;margin:24px;background:#111;color:#eee}h1{font-size:24px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:#222;padding:12px;border-radius:12px}
img{display:block;width:100%;min-height:180px;object-fit:contain;background:#000}pre{color:#8fda9c}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
</style>
<h1>MultiCam Wi-Fi Receiver</h1><pre id="stats">Waiting for iPhone...</pre>
<div class="grid"><div class="card"><h2>Wide</h2><img id="wide"></div>
<div class="card"><h2>Ultra Wide</h2><img id="ultra"></div></div>
<script>
async function refresh(){const t=Date.now();wide.src='/wide.jpg?t='+t;ultra.src='/ultra.jpg?t='+t;
try{const s=await fetch('/stats.json?t='+t).then(r=>r.json());stats.textContent=JSON.stringify(s,null,2)}catch(e){}}
setInterval(refresh,250);refresh();
</script>"""


if __name__ == "__main__":
    threading.Thread(target=run_tcp_server, daemon=True).start()
    print(f"Dashboard: http://127.0.0.1:{HTTP_PORT}", flush=True)
    ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), DashboardHandler).serve_forever()
