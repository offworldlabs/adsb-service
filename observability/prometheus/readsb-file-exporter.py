#!/usr/bin/env python3
"""Serve readsb's stats.prom file as a Prometheus /metrics endpoint.

readsb never serves metrics over HTTP: --write-prom rewrites the FILE
/run/readsb/stats.prom about once per minute. This is the zero-dependency
fallback scrape path for hosts without a node_exporter textfile collector
(prometheus.yml job "readsb"; systemd unit: readsb-file-exporter.service).

Stdlib only; binds 127.0.0.1:9102 — loopback/monitoring surface only,
never public ingress (spec §21.3). While the file is missing or unreadable
(readsb down / not yet written) it still answers 200 with only
`readsb_stats_file_readable 0` — a non-2xx scrape would set up=0, so a
sentinel metric on a 200 is the only way to keep the target "up" while
making the readsb_* absence unambiguous. Successful responses append
`readsb_stats_file_readable 1`.
"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROM_FILE = Path(sys.argv[1] if len(sys.argv) > 1 else "/run/readsb/stats.prom")
BIND = ("127.0.0.1", 9102)
READABLE_OK = b"readsb_stats_file_readable 1\n"
READABLE_NO = b"readsb_stats_file_readable 0\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_error(404)
            return
        try:
            body = PROM_FILE.read_bytes()
            if body and not body.endswith(b"\n"):
                body += b"\n"
            body += READABLE_OK
        except OSError:
            # 200 on purpose: a 503 would make Prometheus mark the target
            # down (up=0). The sentinel-only body keeps up=1 and makes
            # "readsb down / not yet written" a queryable condition.
            body = READABLE_NO
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # per-scrape access lines would just flood journald


if __name__ == "__main__":
    ThreadingHTTPServer(BIND, MetricsHandler).serve_forever()
