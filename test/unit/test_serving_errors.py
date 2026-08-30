"""Upstream failure mapping: connection errors -> 502, timeouts -> 504,
oversized bodies -> 502 (sec-3 byte budget)."""

from __future__ import annotations

import asyncio

import aiohttp
from aioresponses import aioresponses

from serving_api.reapi import ReAPI

from test.unit.test_serving_common import REAPI, make_config, serving_client


async def test_upstream_down_maps_to_502_json() -> None:
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(
                f"{REAPI}?circle=10,20,50&jv2",
                exception=aiohttp.ClientConnectionError("connection refused"),
            )
            response = await client.get("/v2/lat/10/lon/20/dist/50")
    assert response.status_code == 502
    assert response.json() == {"msg": "upstream unavailable"}
    assert response.headers["content-type"].startswith("application/json")


async def test_upstream_timeout_maps_to_504_json() -> None:
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=10,20,50&jv2", exception=asyncio.TimeoutError())
            response = await client.get("/v2/lat/10/lon/20/dist/50")
    assert response.status_code == 504
    assert response.json() == {"msg": "upstream timeout"}


async def test_upstream_500_masked_as_502() -> None:
    # DESIGN §5: never a bare 500 out of the serving tier.
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=10,20,50&jv2", status=500, body=b"boom")
            response = await client.get("/v2/lat/10/lon/20/dist/50")
    assert response.status_code == 502
    assert response.json() == {"msg": "upstream unavailable"}


async def test_unmocked_upstream_is_502_not_500() -> None:
    # aioresponses turns any unregistered URL into a ClientConnectionError,
    # standing in for "readsb not running".
    async with serving_client() as client:
        with aioresponses() as mock:  # noqa: F841
            response = await client.get("/v2/lat/1/lon/2/dist/3")
    assert response.status_code == 502


async def test_oversized_upstream_body_maps_to_502() -> None:
    # sec-3: a body beyond readsb_response_max_bytes must never be buffered
    # whole (256M-capped service); it maps to the upstream-unavailable path.
    config = make_config(readsb_response_max_bytes=1024)
    async with serving_client(config) as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=10,20,50&jv2", status=200, body=b"x" * 4096)
            response = await client.get("/v2/lat/10/lon/20/dist/50")
    assert response.status_code == 502
    assert response.json() == {"msg": "upstream unavailable"}


async def test_body_within_budget_passes_verbatim() -> None:
    config = make_config(readsb_response_max_bytes=4096)
    body = b'{"ac": [], "msg": "No error", "now": 1, "total": 0}'
    async with serving_client(config) as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=10,20,50&jv2", status=200, body=body)
            response = await client.get("/v2/lat/10/lon/20/dist/50")
    assert response.status_code == 200
    assert response.content == body


async def test_reapi_budget_boundary() -> None:
    # exactly at the budget is allowed; one byte over is refused
    reapi = ReAPI(REAPI, max_response_bytes=8)
    await reapi.start()
    try:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=1,2,3&jv2", status=200, body=b"12345678")
            assert await reapi.request(["circle=1,2,3"]) == (200, b"12345678")
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=1,2,3&jv2", status=200, body=b"123456789")
            assert await reapi.request(["circle=1,2,3"]) == (502, b"")
    finally:
        await reapi.close()
