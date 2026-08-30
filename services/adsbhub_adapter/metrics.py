"""Prometheus metrics and the /metrics + /healthz + /livez HTTP surface.

Metric names/labels are the exact §22.1 set fixed in DESIGN.md §4; the
/healthz contract is spec §14.4 row 1: 200 iff upstream connected AND a
valid record was seen within the last 30 s (degraded 10-30 s, still 200),
503 with JSON detail otherwise. /livez is liveness only and never depends
on upstream health (spec §23).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable

from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .sbs import RESULT_LABELS

PROVIDER = "adsbhub"
# spec §14.4 row 1: 'ADSBHub no valid record' degraded at 10 s, unhealthy at 30 s
DEGRADED_AFTER_SECONDS = 10.0
UNHEALTHY_AFTER_SECONDS = 30.0
DELAY_BUCKETS = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0)
# spec §22.1: retina_adsb_build_info{version,commit,schema_version}; the
# Phase A interim v2 contract fixes schema_version (sc-5/ops-8)
SCHEMA_VERSION = "interim-v2"


def build_commit() -> str:
    # never shell out to git at runtime; deploy stamps RETINA_ADSB_COMMIT
    return os.environ.get("RETINA_ADSB_COMMIT", "unknown")


@dataclass
class HealthState:
    """Mutable view shared between the adapter data path and /healthz."""

    connected: bool = False
    last_valid_monotonic: float | None = None


class AdapterMetrics:
    """The DESIGN.md §4 metric set on an isolated registry."""

    def __init__(self, provider: str = PROVIDER, registry: CollectorRegistry | None = None) -> None:
        self.provider = provider
        self.registry = registry if registry is not None else CollectorRegistry()
        reg = self.registry
        self.connected = Gauge(
            "retina_adsb_provider_connected",
            "1 while the upstream provider TCP session is established",
            ["provider"], registry=reg)
        self.bytes_total = Counter(
            "retina_adsb_provider_bytes_total",
            "Bytes received from the upstream provider",
            ["provider"], registry=reg)
        self.records_total = Counter(
            "retina_adsb_provider_records_total",
            "SBS records seen, by validation result",
            ["provider", "result"], registry=reg)
        self.last_record_timestamp_seconds = Gauge(
            "retina_adsb_provider_last_record_timestamp_seconds",
            "Unix time of the last valid record",
            ["provider"], registry=reg)
        self.reconnects_total = Counter(
            "retina_adsb_provider_reconnects_total",
            "Upstream session terminations, by reason",
            ["provider", "reason"], registry=reg)
        self.record_delay_seconds = Histogram(
            "retina_adsb_provider_record_delay_seconds",
            "Per-record delay: adapter wall clock minus provider generated timestamp",
            ["provider"], registry=reg, buckets=DELAY_BUCKETS)
        self.relay_connected_clients = Gauge(
            "retina_adsb_relay_connected_clients",
            "Currently connected relay clients",
            registry=reg)
        self.relay_dropped_lines_total = Counter(
            "retina_adsb_relay_dropped_lines_total",
            "Lines lost to relay clients dropped on queue overflow",
            registry=reg)
        self.build_info = Gauge(
            "retina_adsb_build_info",
            "Build identity (spec §22.1/§18.1); value is always 1",
            ["version", "commit", "schema_version", "config_hash"], registry=reg)

        self.connected.labels(provider).set(0)
        self.relay_connected_clients.set(0)
        # ops-7: expose the series from startup so age-based alert clauses
        # (ADSBHubDown silent feed) see an honestly ancient timestamp, not
        # a missing series, after a restart with a recordless upstream
        self.last_record_timestamp_seconds.labels(provider).set(0)
        for result in RESULT_LABELS:
            self.records_total.labels(provider, result).inc(0)

    def set_connected(self, up: bool) -> None:
        self.connected.labels(self.provider).set(1 if up else 0)

    def add_bytes(self, count: int) -> None:
        self.bytes_total.labels(self.provider).inc(count)

    def record(self, result: str) -> None:
        self.records_total.labels(self.provider, result).inc()

    def set_last_record(self, unix_seconds: float) -> None:
        self.last_record_timestamp_seconds.labels(self.provider).set(unix_seconds)

    def reconnect(self, reason: str) -> None:
        self.reconnects_total.labels(self.provider, reason).inc()

    def observe_delay(self, delay_seconds: float) -> None:
        # tiny negative delays are clock skew within future_tolerance; clamp to 0
        self.record_delay_seconds.labels(self.provider).observe(max(0.0, delay_seconds))

    def set_build_info(self, version: str, config_hash: str, *,
                       commit: str | None = None,
                       schema_version: str = SCHEMA_VERSION) -> None:
        self.build_info.labels(
            version=version,
            commit=commit if commit is not None else build_commit(),
            schema_version=schema_version,
            config_hash=config_hash,
        ).set(1)

    def exposition(self) -> bytes:
        return generate_latest(self.registry)


class MetricsServer:
    """Loopback HTTP server for /metrics and /healthz (DESIGN.md §2: 127.0.0.1:9101)."""

    def __init__(
        self,
        metrics: AdapterMetrics,
        health: HealthState,
        host: str = "127.0.0.1",
        port: int = 9101,
        *,
        degraded_after: float = DEGRADED_AFTER_SECONDS,
        unhealthy_after: float = UNHEALTHY_AFTER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._metrics = metrics
        self._health = health
        self._host = host
        self.port = port
        self._degraded_after = degraded_after
        self._unhealthy_after = unhealthy_after
        self._clock = clock
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get("/metrics", self._handle_metrics)
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/livez", self._handle_livez)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self.port)
        await site.start()
        self._runner = runner
        for address in runner.addresses:
            self.port = address[1]
            break

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()

    def health_status(self) -> tuple[bool, dict]:
        age: float | None = None
        if self._health.last_valid_monotonic is not None:
            age = max(0.0, self._clock() - self._health.last_valid_monotonic)
        healthy = bool(self._health.connected and age is not None and age <= self._unhealthy_after)
        # sc-8, spec §14.4 row 1: degraded 10-30 s without a valid record is
        # still HTTP 200 — only the state string distinguishes it
        if not healthy:
            state = "unhealthy"
        elif age > self._degraded_after:
            state = "degraded"
        else:
            state = "healthy"
        detail = {
            "healthy": healthy,
            "state": state,
            "provider": self._metrics.provider,
            "connected": self._health.connected,
            "last_valid_record_age_seconds": age,
            "degraded_after_seconds": self._degraded_after,
            "unhealthy_after_seconds": self._unhealthy_after,
        }
        return healthy, detail

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        return web.Response(body=self._metrics.exposition(),
                            headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        healthy, detail = self.health_status()
        return web.json_response(detail, status=200 if healthy else 503)

    async def _handle_livez(self, request: web.Request) -> web.Response:
        # spec §23: liveness must not depend on upstream health — answering
        # at all proves the event loop is serving
        return web.json_response({"live": True})
