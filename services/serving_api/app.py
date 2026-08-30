"""FastAPI app factory for the interim v2 serving tier (DESIGN.md §5)."""

from __future__ import annotations

import asyncio
import importlib.metadata
import ipaddress
import json
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator

import aiohttp
from fastapi import FastAPI, Path, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from serving_api.config import ServingConfig, redacted_config_hash
from serving_api.ratelimit import TokenBucketLimiter
from serving_api.reapi import UPSTREAM_TIMEOUT, ReAPI

MAX_RADIUS_NM = 250.0

# sec-1: /health fans out up to three upstream probes; concurrent/looping
# callers share one composed result for this long (monotonic clock).
HEALTH_CACHE_SECONDS = 1.0

# spec §22.1: retina_adsb_build_info{version,commit,schema_version}; the
# Phase A interim v2 contract fixes schema_version (sc-5/ops-8)
SCHEMA_VERSION = "interim-v2"

# re-api requires exactly one "main option" per request (readsb
# api.c:1478-1494); an empty 1 nm circle at null island is the cheapest
# well-formed query that still carries the cache-generation "now" the
# freshness check needs.
FRESHNESS_PROBE_PARAM = "circle=0,0,1"

try:
    _VERSION = importlib.metadata.version("retina-adsb")
except importlib.metadata.PackageNotFoundError:
    _VERSION = "unknown"


def _fmt(value: float) -> str:
    # Integral values render bare ("250" not "250.0") to match the vendored
    # integer-radius query form; everything else keeps full repr precision.
    if value.is_integer():
        return str(int(value))
    return repr(value)


Lat = Annotated[float, Path(ge=-90.0, le=90.0)]
Lon = Annotated[float, Path(ge=-180.0, le=180.0)]
# Negative radius -> 422; values above 250 are accepted and clamped in the
# handler. The vendored code clamped min(int(radius), 250) with NO validation
# (code-adsblol-api.md flag-broken-validation: float radii 500'd, garbage
# lat/lon leaked to readsb); we take floats and validate for real.
Radius = Annotated[float, Path(ge=0.0)]


def create_app(config: ServingConfig | None = None) -> FastAPI:
    cfg = config if config is not None else ServingConfig()
    reapi = ReAPI(
        cfg.reapi_endpoint,
        timeout_total=cfg.timeouts.total_seconds,
        timeout_connect=cfg.timeouts.connect_seconds,
        max_response_bytes=cfg.readsb_response_max_bytes,
    )
    limiter = TokenBucketLimiter(
        rate=cfg.rate_limit.rate_per_second,
        burst=cfg.rate_limit.burst,
        lru_size=cfg.rate_limit.lru_size,
        new_key_rate=cfg.rate_limit.new_key_rate,
        new_key_burst=cfg.rate_limit.new_key_burst,
    )
    config_hash = redacted_config_hash(cfg)
    # L3: peers listed in rate_limit.trusted_proxies (our own deploy/nginx
    # fronts) collapse every client into one bucket unless the limiter keys
    # on the address the proxy itself appended to X-Forwarded-For.
    trusted_proxies = frozenset(cfg.rate_limit.trusted_proxies)

    def _rate_limit_key(request: Request) -> str:
        peer = request.client.host if request.client is not None else "unknown"
        if trusted_proxies:
            try:
                normalized_peer = str(ipaddress.ip_address(peer))
            except ValueError:
                normalized_peer = peer
            if normalized_peer in trusted_proxies:
                xff = request.headers.get("x-forwarded-for", "")
                # RIGHTMOST entry only: that is the hop appended by our own
                # trusted proxy (the address that connected to it). Every
                # entry left of it arrived from the outside and is
                # client-controlled — never trusted.
                rightmost = xff.rsplit(",", 1)[-1].strip()
                if rightmost:
                    return rightmost
                return peer  # trusted proxy, but no XFF: fall back to peer
        # Untrusted peer: X-Forwarded-For is ignored entirely (spoofable);
        # the bucket keys on the direct peer address.
        return peer

    # Per-app registry so repeated create_app() calls (tests) never collide.
    registry = CollectorRegistry()
    requests_total = Counter(
        "retina_adsb_http_requests_total",
        "HTTP requests served, by route template and response status.",
        ["route", "status"],
        registry=registry,
    )
    upstream_latency = Histogram(
        "retina_adsb_http_upstream_latency_seconds",
        "Latency of readsb re-api requests issued for /v2 queries.",
        registry=registry,
        buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    )
    ratelimit_drops = Counter(
        "retina_adsb_http_ratelimit_drops_total",
        "Requests rejected with 429 by the per-client token bucket.",
        registry=registry,
    )
    build_info = Gauge(
        "retina_adsb_build_info",
        "Build identity (spec §22.1/§18.1); value is always 1.",
        ["version", "commit", "schema_version", "config_hash"],
        registry=registry,
    )
    build_info.labels(
        version=_VERSION,
        # never shell out to git at runtime; deploy stamps RETINA_ADSB_COMMIT
        commit=os.environ.get("RETINA_ADSB_COMMIT", "unknown"),
        schema_version=SCHEMA_VERSION,
        config_hash=config_hash,
    ).set(1)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await reapi.start()
        try:
            yield
        finally:
            await reapi.close()

    app = FastAPI(title="retina-serving-api", version=_VERSION, lifespan=lifespan)
    app.state.config = cfg
    app.state.reapi = reapi
    app.state.limiter = limiter
    app.state.registry = registry

    @app.middleware("http")
    async def count_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        # Route template only — never the raw path (bounded label cardinality,
        # spec §21.6; also keeps queried coordinates out of the metrics).
        route = request.scope.get("route")
        label = getattr(route, "path_format", None) or "unmatched"
        requests_total.labels(route=label, status=str(response.status_code)).inc()
        return response

    async def v2_circle(request: Request, lat: Lat, lon: Lon, radius: Radius) -> Response:
        allowed, retry_after = limiter.check(_rate_limit_key(request))
        if not allowed:
            ratelimit_drops.inc()
            return JSONResponse(
                {"msg": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
        clamped = min(radius, MAX_RADIUS_NM)
        started = time.perf_counter()
        status, body = await reapi.request([f"circle={_fmt(lat)},{_fmt(lon)},{_fmt(clamped)}"])
        upstream_latency.observe(time.perf_counter() - started)
        if status == UPSTREAM_TIMEOUT:
            return JSONResponse({"msg": "upstream timeout"}, status_code=504)
        if status >= 500:
            # DESIGN §5: never a bare 5xx from readsb; non-timeout failures
            # collapse to 502 upstream-unavailable.
            return JSONResponse({"msg": "upstream unavailable"}, status_code=502)
        # Verbatim passthrough: the jv2 envelope is built by readsb and must
        # reach the node byte-identical (plan §0 req 4).
        return Response(content=body, status_code=status, media_type="application/json")

    app.add_api_route(
        "/v2/lat/{lat}/lon/{lon}/dist/{radius}",
        v2_circle,
        methods=["GET"],
        name="v2_lat_lon_dist",
        operation_id="v2_lat_lon_dist",
    )
    app.add_api_route(
        "/v2/point/{lat}/{lon}/{radius}",
        v2_circle,
        methods=["GET"],
        name="v2_point",
        operation_id="v2_point",
    )

    async def _adapter_health() -> dict[str, Any]:
        # Best-effort only: the adapter being down means no fresh data soon,
        # but serving stays up on whatever readsb still holds (DESIGN §5).
        if reapi.session is None:
            return {"reachable": False, "status": None, "healthy": False}
        try:
            async with reapi.session.get(cfg.adapter_health_url) as response:
                return {"reachable": True, "status": response.status, "healthy": response.status == 200}
        except (TimeoutError, aiohttp.ClientError):
            return {"reachable": False, "status": None, "healthy": False}

    health_lock = asyncio.Lock()
    health_cached: tuple[float, int, dict[str, Any]] | None = None

    async def _composed_health() -> tuple[int, dict[str, Any]]:
        probe = await reapi.status_probe()
        readsb_reachable = probe == 200
        fresh = False
        age_seconds: float | None = None
        if readsb_reachable:
            status, body = await reapi.request([FRESHNESS_PROBE_PARAM])
            if status == 200:
                try:
                    now_ms = json.loads(body)["now"]
                except (ValueError, KeyError, TypeError):
                    now_ms = None
                if isinstance(now_ms, (int, float)) and not isinstance(now_ms, bool):
                    age_seconds = time.time() - float(now_ms) / 1000.0
                    fresh = age_seconds <= cfg.freshness.stale_after_seconds
        adapter = await _adapter_health()
        ok = readsb_reachable and fresh
        detail = {
            "status": "ok" if ok else "unhealthy",
            "readsb": {"reachable": probe is not None, "status": probe},
            "freshness": {
                "age_seconds": age_seconds,
                "stale_after_seconds": cfg.freshness.stale_after_seconds,
                "fresh": fresh,
            },
            "adapter": adapter,
            # spec §18.1: process exposes build version + redacted config hash.
            "version": _VERSION,
            "config_hash": config_hash,
        }
        return (200 if ok else 503), detail

    @app.get("/health")
    async def health() -> JSONResponse:
        nonlocal health_cached
        # sec-1: the lock also serializes concurrent first callers so a probe
        # burst can never fan out more than once per cache window
        async with health_lock:
            if health_cached is not None and time.monotonic() - health_cached[0] < HEALTH_CACHE_SECONDS:
                _, status_code, detail = health_cached
            else:
                status_code, detail = await _composed_health()
                health_cached = (time.monotonic(), status_code, detail)
        return JSONResponse(detail, status_code=status_code)

    @app.get("/livez")
    async def livez() -> JSONResponse:
        # spec §23: liveness must not depend on upstream health — answering
        # at all proves the event loop is serving
        return JSONResponse({"live": True})

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
