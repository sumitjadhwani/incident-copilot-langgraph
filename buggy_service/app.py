"""An intentionally buggy microservice for the live demo (stdlib only).

Behaviour is driven by environment variables:
  CACHE_MB=200            warm an in-memory "product cache" to this size (MiB) at ~CACHE_GROWTH_MB_S per second.
                          With a 128Mi memory limit the container is OOMKilled (exit 137) and crash-loops.
  REQUIRE_SMTP=true       validate SMTP_PORT at startup; a non-integer value exits with code 1.
  ERROR_RATE=0.0          fraction of requests answered with HTTP 500.
  EXTRA_LATENCY_MS=0      added to every request.

Exposes GET / (work), GET /healthz, GET /metrics (Prometheus text format).
"""

from __future__ import annotations

import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.environ.get("SERVICE_NAME", "buggy-service")
VERSION = os.environ.get("SERVICE_VERSION", "dev")
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

_lock = threading.Lock()
_requests: dict[tuple[str, str], int] = {}
_hist = [0] * (len(BUCKETS) + 1)
_sum = 0.0
_cache: list[bytes] = []


def log(level: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} {level:<5} {msg}", flush=True)


def validate_config() -> None:
    if os.environ.get("REQUIRE_SMTP", "false").lower() != "true":
        return
    port = os.environ.get("SMTP_PORT", "")
    log("INFO", "loading configuration from environment")
    if not port.isdigit():
        log("ERROR", f"config validation failed: SMTP_PORT: value '{port}' is not a valid integer")
        log("FATAL", "refusing to start with invalid configuration (1 error)")
        sys.exit(1)
    log("INFO", f"smtp configured host={os.environ.get('SMTP_HOST', '?')} port={port}")


def warm_cache() -> None:
    target = int(os.environ.get("CACHE_MB", "0"))
    rate = max(1, int(os.environ.get("CACHE_GROWTH_MB_S", "8")))
    if target <= 0:
        log("INFO", "product cache disabled")
        return
    log("INFO", f"product cache enabled max_mb={target}")
    while len(_cache) < target:
        for _ in range(rate):
            if len(_cache) >= target:
                break
            _cache.append(os.urandom(1024) * 1024)  # 1 MiB of touched memory
        if len(_cache) % 16 < rate:
            log("INFO" if len(_cache) < target * 0.6 else "WARN", f"product cache size mb={len(_cache)}")
        time.sleep(1)
    log("INFO", f"product cache warm mb={len(_cache)}")


def record(path: str, status: int, seconds: float) -> None:
    global _sum
    with _lock:
        key = (path, str(status))
        _requests[key] = _requests.get(key, 0) + 1
        _sum += seconds
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                _hist[i] += 1
                break
        else:
            _hist[-1] += 1


def metrics_text() -> str:
    with _lock:
        lines = ["# TYPE http_requests_total counter"]
        lines += [f'http_requests_total{{path="{p}",status="{s}"}} {n}' for (p, s), n in sorted(_requests.items())]
        lines.append("# TYPE http_request_duration_seconds histogram")
        cumulative = 0
        for b, n in zip(BUCKETS, _hist, strict=False):
            cumulative += n
            lines.append(f'http_request_duration_seconds_bucket{{le="{b}"}} {cumulative}')
        cumulative += _hist[-1]
        lines.append(f'http_request_duration_seconds_bucket{{le="+Inf"}} {cumulative}')
        lines.append(f"http_request_duration_seconds_sum {_sum}")
        lines.append(f"http_request_duration_seconds_count {cumulative}")
        lines.append("# TYPE product_cache_megabytes gauge")
        lines.append(f"product_cache_megabytes {len(_cache)}")
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # silence default access log
        pass

    def _send(self, status: int, body: str, ctype: str = "text/plain") -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            return self._send(200, "ok")
        if self.path == "/metrics":
            return self._send(200, metrics_text(), "text/plain; version=0.0.4")
        start = time.perf_counter()
        time.sleep((int(os.environ.get("EXTRA_LATENCY_MS", "0")) + random.uniform(5, 25)) / 1000)
        status = 500 if random.random() < float(os.environ.get("ERROR_RATE", "0")) else 200
        self._send(status, f"{SERVICE} {VERSION}\n")
        elapsed = time.perf_counter() - start
        record("/", status, elapsed)
        if status >= 500:
            log("ERROR", f"GET / {status} {elapsed * 1000:.0f}ms")


def main() -> None:
    log("INFO", f"starting {SERVICE} {VERSION}")
    validate_config()
    threading.Thread(target=warm_cache, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    log("INFO", "listening on :8080")
    server.serve_forever()


if __name__ == "__main__":
    main()
