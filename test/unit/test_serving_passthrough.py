"""Byte-fidelity of the jv2 envelope passthrough (plan §0 req 4)."""

from __future__ import annotations

from aioresponses import aioresponses

from test.unit.test_serving_common import FIXTURE_PATH, REAPI, serving_client


async def test_passthrough_bytes_identical() -> None:
    body = FIXTURE_PATH.read_bytes()
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=37.6,-122.4,250&jv2", status=200, body=body)
            response = await client.get("/v2/lat/37.6/lon/-122.4/dist/250")
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "application/json"


async def test_upstream_400_passed_through() -> None:
    # readsb rejects bad queries with plain 400 (api.c:1762-1767); non-5xx
    # upstream statuses pass through rather than being masked.
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=10,20,50&jv2", status=400, body=b"")
            response = await client.get("/v2/lat/10/lon/20/dist/50")
    assert response.status_code == 400
