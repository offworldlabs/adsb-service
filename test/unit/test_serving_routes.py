"""Route validation and radius-clamp behavior of the /v2 circle endpoints."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from test.unit.test_serving_common import FIXTURE_PATH, REAPI, serving_client


@pytest.mark.parametrize(
    "path",
    [
        "/v2/lat/91/lon/0/dist/50",
        "/v2/lat/-90.001/lon/0/dist/50",
        "/v2/lat/abc/lon/0/dist/50",
        "/v2/point/91/0/50",
    ],
)
async def test_bad_lat_rejected(path: str) -> None:
    async with serving_client() as client:
        response = await client.get(path)
        assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/v2/lat/10/lon/181/dist/50",
        "/v2/lat/10/lon/-180.5/dist/50",
        "/v2/lat/10/lon/xyz/dist/50",
        "/v2/point/10/181/50",
    ],
)
async def test_bad_lon_rejected(path: str) -> None:
    async with serving_client() as client:
        response = await client.get(path)
        assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/v2/lat/10/lon/20/dist/-1",
        "/v2/lat/10/lon/20/dist/-0.5",
        "/v2/lat/10/lon/20/dist/notanumber",
        "/v2/point/10/20/-1",
    ],
)
async def test_bad_radius_rejected(path: str) -> None:
    async with serving_client() as client:
        response = await client.get(path)
        assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    ["/v2/lat/10/lon/20/dist/300", "/v2/point/10/20/300"],
)
async def test_radius_over_250_clamped(path: str) -> None:
    body = FIXTURE_PATH.read_bytes()
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=10,20,250&jv2", status=200, body=body)
            response = await client.get(path)
            assert response.status_code == 200
            assert len(mock.requests) == 1
            ((method, url),) = mock.requests
            assert method == "GET"
            assert "circle=10,20,250" in str(url)
            assert "300" not in str(url)


async def test_float_radius_accepted() -> None:
    # Regression vs vendored api_v2.py:126: min(int(radius),250) 500'd on "2.5".
    body = FIXTURE_PATH.read_bytes()
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=10,20,2.5&jv2", status=200, body=body)
            response = await client.get("/v2/lat/10/lon/20/dist/2.5")
            assert response.status_code == 200
            ((_, url),) = mock.requests
            assert "circle=10,20,2.5" in str(url)


async def test_full_precision_coordinates_forwarded() -> None:
    body = FIXTURE_PATH.read_bytes()
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=51.89508,2.79437,250&jv2", status=200, body=body)
            response = await client.get("/v2/lat/51.89508/lon/2.79437/dist/250")
            assert response.status_code == 200


async def test_point_alias_equivalent_to_lat_lon_dist() -> None:
    body = FIXTURE_PATH.read_bytes()
    async with serving_client() as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=-33.5,151.25,100&jv2", status=200, body=body, repeat=True)
            r1 = await client.get("/v2/lat/-33.5/lon/151.25/dist/100")
            r2 = await client.get("/v2/point/-33.5/151.25/100")
            assert r1.status_code == r2.status_code == 200
            assert r1.content == r2.content
