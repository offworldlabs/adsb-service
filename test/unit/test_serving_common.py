"""Shared helpers for the serving-api unit tests (contains no tests itself)."""

from __future__ import annotations

import contextlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
from aiohttp import ClientResponse

import aioresponses.core as _aioresponses_core

from serving_api.app import create_app
from serving_api.config import RateLimitConfig, ServingConfig

# aioresponses 0.7.9 predates aiohttp 3.14, whose ClientResponse requires a
# keyword-only stream_writer; supply a stub so mock construction works. The
# signature guard makes this a no-op once the pinned libraries align.
if "stream_writer" in inspect.signature(ClientResponse.__init__).parameters:

    class _CompatClientResponse(ClientResponse):
        def __init__(self, method: str, url: Any, **kwargs: Any):
            kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
            super().__init__(method, url, **kwargs)

    _aioresponses_core.ClientResponse = _CompatClientResponse

REAPI = "http://127.0.0.1:30152/re-api/"
ADAPTER_HEALTHZ = "http://127.0.0.1:9101/healthz"
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "api" / "jv2_envelope.json"


def make_config(**overrides: Any) -> ServingConfig:
    # Generous default rate limit so tests never trip 429 unless they mean to.
    kwargs: dict[str, Any] = {
        "rate_limit": RateLimitConfig(rate_per_second=1000.0, burst=1000, lru_size=100),
    }
    kwargs.update(overrides)
    return ServingConfig(**kwargs)


@contextlib.asynccontextmanager
async def serving_client(
    config: ServingConfig | None = None, peer: str = "127.0.0.1"
) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI test client; ``peer`` sets the transport-level client address
    (what ``request.client.host`` reports — NOT spoofable via headers)."""
    app = create_app(config or make_config())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=(peer, 42000))
        async with httpx.AsyncClient(transport=transport, base_url="http://serving.test") as client:
            # tests may reach the app (e.g. app.state.limiter) via the client
            client.app = app  # type: ignore[attr-defined]
            yield client
