"""Log-hygiene property for the serving tier (spec §21.5 / §22.3; DESIGN §5).

DESIGN §5 forbids client-IP + full-URL request logging: queried /v2 paths
embed client positions, so neither the caller's address nor the request URL
(or the upstream circle= query derived from it) may appear in any log record.
The app runs with uvicorn access_log=False in production; this test asserts
the property for everything the application itself logs, captured at DEBUG,
during a real /v2 request served through a mocked readsb upstream.
"""

from __future__ import annotations

import logging

import pytest
from aioresponses import aioresponses

from test.unit.test_serving_common import FIXTURE_PATH, REAPI, make_config, serving_client

CLIENT_IP = "203.0.113.77"


async def test_v2_request_logs_carry_no_client_ip_or_query_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    body = FIXTURE_PATH.read_bytes()
    async with serving_client(make_config(), peer=CLIENT_IP) as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=10.5,20.25,50&jv2", status=200, body=body)
            response = await client.get("/v2/lat/10.5/lon/20.25/dist/50")
    assert response.status_code == 200

    rendered = "\n".join(
        f"{record.name} {record.levelname} {record.getMessage()}"
        for record in caplog.records
        # the httpx logger belongs to the TEST client issuing the request
        # (it logs its own outgoing URL); it never runs in the serving
        # process and is not part of the property under test
        if not record.name.startswith("httpx")
    )
    # the caller's address never appears in any record, at any level
    assert CLIENT_IP not in rendered
    # neither the served URL path (embeds the queried position)...
    assert "/v2/lat/10.5" not in rendered
    assert "10.5/lon/20.25" not in rendered
    # ...nor the upstream query string derived from it
    assert "circle=10.5" not in rendered
    assert "jv2" not in rendered
