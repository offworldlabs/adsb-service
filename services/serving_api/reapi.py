"""readsb re-api client.

Adapted from vendor/api/src/adsb_api/utils/reapi.py (adsblol/api, shallow
commit 3c969c8, BSD-3-Clause, Copyright 2023 Katia Esposito).

Divergences per DESIGN.md §5:
- one shared aiohttp.ClientSession for the process lifetime (the vendored
  client opened a session per request, reapi.py:31);
- upstream failures return (502/504, b"") markers instead of leaking an
  unhandled exception into a bare 500;
- no logging of client IPs or full request URLs (the vendored reapi.py:27
  printed both on every request — a query-history leak; request accounting
  lives in Prometheus counters instead).
"""

from __future__ import annotations

import re

import aiohttp

# Vendored param-safety regex (reapi.py:12), unchanged.
_ALLOWED_PARAM = re.compile(r"^[a-zA-Z0-9,=_\.-]+$")

# Marker statuses returned in-band; readsb itself never emits 502/504
# (its error paths are plain 400/405/500/503, api.c:1129-1160), so these
# are unambiguous to the caller.
UPSTREAM_UNAVAILABLE = 502
UPSTREAM_TIMEOUT = 504

# sec-3: bound the buffered upstream body — an unbounded read() could OOM
# the 256M-capped service. Configurable via readsb_response_max_bytes.
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_READ_CHUNK_BYTES = 65536


class ReAPI:
    def __init__(self, endpoint: str, timeout_total: float = 5.0, timeout_connect: float = 1.0,
                 max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES):
        self.endpoint = endpoint
        self._timeout = aiohttp.ClientTimeout(
            total=timeout_total, connect=timeout_connect, sock_connect=timeout_connect
        )
        self._max_response_bytes = max_response_bytes
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    def params_valid(self, params: list[str]) -> bool:
        return all(_ALLOWED_PARAM.match(param) for param in params)

    async def request(self, params: list[str]) -> tuple[int, bytes]:
        """GET {endpoint}?{params}&jv2 -> (status, body bytes).

        Upstream connection failure maps to (502, b""), timeout to (504, b"");
        a body larger than the configured budget also maps to (502, b"").
        """
        if self.session is None:
            raise RuntimeError("ReAPI.start() must be called before request()")
        if not self.params_valid(params):
            return (400, b'{"msg": "invalid upstream parameters"}')
        url = self.endpoint + "?" + "&".join([*params, "jv2"])
        try:
            async with self.session.get(url) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(_READ_CHUNK_BYTES):
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        # oversize is an upstream fault, not a client one (sec-3)
                        return (UPSTREAM_UNAVAILABLE, b"")
                return (response.status, bytes(body))
        except TimeoutError:
            # aiohttp raises asyncio.TimeoutError (== builtins.TimeoutError on
            # 3.12) on total-timeout; ServerTimeoutError also lands here.
            return (UPSTREAM_TIMEOUT, b"")
        except aiohttp.ClientError:
            return (UPSTREAM_UNAVAILABLE, b"")

    async def status_probe(self) -> int | None:
        """GET ?status — readsb api.c:1750-1759: 200 healthy, 503 exit pending.

        Returns the HTTP status, or None when readsb is unreachable/timing out.
        """
        if self.session is None:
            raise RuntimeError("ReAPI.start() must be called before status_probe()")
        try:
            async with self.session.get(self.endpoint + "?status") as response:
                return response.status
        except (TimeoutError, aiohttp.ClientError):
            return None
