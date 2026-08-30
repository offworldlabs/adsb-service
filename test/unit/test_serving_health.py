"""/health states: readsb up/down/stale, adapter best-effort (DESIGN.md §5)."""

from __future__ import annotations

import json
import time

import aiohttp
from aioresponses import aioresponses

from test.unit.test_serving_common import ADAPTER_HEALTHZ, FIXTURE_PATH, REAPI, serving_client

PROBE_URL = f"{REAPI}?circle=0,0,1&jv2"
STATUS_URL = f"{REAPI}?status"


def _envelope(now_ms: int) -> bytes:
    envelope = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    envelope["now"] = now_ms
    envelope["ctime"] = now_ms
    return json.dumps(envelope).encode()


async def test_health_ok_when_readsb_up_and_fresh() -> None:
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(STATUS_URL, status=200)
            mock.get(PROBE_URL, status=200, body=_envelope(int(time.time() * 1000)))
            mock.get(ADAPTER_HEALTHZ, status=200, body=b'{"state": "healthy"}')
            response = await client.get("/health")
    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "ok"
    assert detail["readsb"] == {"reachable": True, "status": 200}
    assert detail["freshness"]["fresh"] is True
    assert detail["adapter"]["healthy"] is True
    # spec §18.1: redacted config hash + version exposed on detailed status.
    assert len(detail["config_hash"]) == 64


async def test_health_503_when_readsb_down() -> None:
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(STATUS_URL, exception=aiohttp.ClientConnectionError("refused"))
            response = await client.get("/health")
    assert response.status_code == 503
    detail = response.json()
    assert detail["status"] == "unhealthy"
    assert detail["readsb"]["reachable"] is False
    assert detail["freshness"]["fresh"] is False


async def test_health_503_when_envelope_stale() -> None:
    # Default freshness: stale beyond 5 x 1 s write-json-every (DESIGN §5).
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(STATUS_URL, status=200)
            stale_ms = int(time.time() * 1000) - 10_000
            mock.get(PROBE_URL, status=200, body=_envelope(stale_ms))
            mock.get(ADAPTER_HEALTHZ, status=200)
            response = await client.get("/health")
    assert response.status_code == 503
    detail = response.json()
    assert detail["readsb"]["reachable"] is True
    assert detail["freshness"]["fresh"] is False
    assert detail["freshness"]["age_seconds"] > 5.0


async def test_health_ok_when_adapter_unreachable() -> None:
    # Adapter healthz is reported best-effort; it never gates overall health.
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(STATUS_URL, status=200)
            mock.get(PROBE_URL, status=200, body=_envelope(int(time.time() * 1000)))
            # adapter healthz deliberately not mocked -> connection error
            response = await client.get("/health")
    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "ok"
    assert detail["adapter"] == {"reachable": False, "status": None, "healthy": False}


async def test_health_503_when_readsb_shutting_down() -> None:
    # readsb answers ?status with 503 while exit is pending (api.c:1750-1759).
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(STATUS_URL, status=503)
            response = await client.get("/health")
    assert response.status_code == 503
    detail = response.json()
    assert detail["readsb"] == {"reachable": True, "status": 503}


async def test_health_result_cached_one_probe_set_for_two_calls() -> None:
    # sec-1: each mock below answers exactly once (no repeat); an uncached
    # second /health would hit an exhausted mock and report readsb down.
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(STATUS_URL, status=200)
            mock.get(PROBE_URL, status=200, body=_envelope(int(time.time() * 1000)))
            mock.get(ADAPTER_HEALTHZ, status=200)
            first = await client.get("/health")
            second = await client.get("/health")
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


async def test_livez_200_while_upstreams_down() -> None:
    # sc-7, spec §23: liveness never depends on upstream health — a liveness
    # probe on /health would restart the service during a readsb outage.
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(STATUS_URL, exception=aiohttp.ClientConnectionError("refused"))
            health = await client.get("/health")
            live = await client.get("/livez")
    assert health.status_code == 503
    assert live.status_code == 200
    assert live.json() == {"live": True}
