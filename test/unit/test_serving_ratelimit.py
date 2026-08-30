"""Token-bucket limiter unit tests and endpoint 429 behavior."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from serving_api.ratelimit import TokenBucketLimiter

from test.unit.test_serving_common import FIXTURE_PATH, REAPI, make_config, serving_client
from serving_api.config import RateLimitConfig


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_burst_then_steady_rate() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate=2.0, burst=10, lru_size=16, clock=clock)

    for _ in range(10):
        allowed, retry_after = limiter.check("1.2.3.4")
        assert allowed
        assert retry_after == 0.0

    allowed, retry_after = limiter.check("1.2.3.4")
    assert not allowed
    assert retry_after == pytest.approx(0.5)

    clock.advance(0.5)
    assert limiter.check("1.2.3.4") == (True, 0.0)
    allowed, _ = limiter.check("1.2.3.4")
    assert not allowed

    for _ in range(5):
        clock.advance(0.5)
        allowed, _ = limiter.check("1.2.3.4")
        assert allowed


def test_tokens_capped_at_burst() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate=2.0, burst=3, lru_size=16, clock=clock)
    clock.advance(3600.0)
    results = [limiter.check("ip")[0] for _ in range(4)]
    assert results == [True, True, True, False]


def test_clients_are_independent() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate=1.0, burst=1, lru_size=16, clock=clock)
    assert limiter.check("a")[0]
    assert not limiter.check("a")[0]
    assert limiter.check("b")[0]


def test_lru_eviction() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate=1.0, burst=1, lru_size=2, clock=clock)
    assert limiter.check("a")[0]
    assert limiter.check("b")[0]
    assert limiter.check("c")[0]
    assert len(limiter) == 2
    assert "a" not in limiter
    # Retained bucket keeps its (empty) state...
    assert not limiter.check("b")[0]
    # ...while the evicted client returns to a fresh full bucket.
    assert limiter.check("a")[0]


def test_table_stays_bounded() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate=1.0, burst=1, lru_size=8, clock=clock)
    for i in range(100):
        limiter.check(f"10.0.0.{i}")
    assert len(limiter) == 8


def test_invalid_construction_rejected() -> None:
    for kwargs in (
        {"rate": 0.0, "burst": 1, "lru_size": 1},
        {"rate": 1.0, "burst": 0, "lru_size": 1},
        {"rate": 1.0, "burst": 1, "lru_size": 0},
        {"rate": 1.0, "burst": 1, "lru_size": 1, "new_key_rate": 0.0},
        {"rate": 1.0, "burst": 1, "lru_size": 1, "new_key_burst": 0.0},
    ):
        with pytest.raises(ValueError):
            TokenBucketLimiter(**kwargs)


class TestNewKeyAdmission:
    """sec-7: rotating > lru_size source keys (IPv6 /64) must not mint a
    fresh full burst per evicted key; the global admission bucket caps it."""

    def test_key_rotation_flood_rejected_established_keys_unaffected(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(rate=100.0, burst=10, lru_size=16, clock=clock,
                                     new_key_rate=1.0, new_key_burst=5)
        # two established, well-behaved clients (2 admission tokens spent)
        assert limiter.check("established-a")[0]
        assert limiter.check("established-b")[0]

        # rotation flood: only the remaining 3 admission tokens ever succeed
        results = [limiter.check(f"2001:db8::{n}")[0] for n in range(100)]
        assert results.count(True) == 3
        allowed, retry_after = limiter.check("2001:db8::fresh")
        assert not allowed
        assert retry_after > 0.0

        # rejected keys were not inserted: established clients still resident
        # and still served from their own buckets
        assert "established-a" in limiter
        assert "established-b" in limiter
        assert limiter.check("established-a")[0]
        assert limiter.check("established-b")[0]

    def test_admission_tokens_refill_over_time(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(rate=100.0, burst=10, lru_size=16, clock=clock,
                                     new_key_rate=2.0, new_key_burst=2)
        assert limiter.check("a")[0]
        assert limiter.check("b")[0]
        assert not limiter.check("c")[0]  # admission budget exhausted
        clock.advance(0.5)  # one token accrues at 2/s
        assert limiter.check("c")[0]
        assert not limiter.check("d")[0]

    def test_evicted_key_needs_readmission(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(rate=1.0, burst=1, lru_size=2, clock=clock,
                                     new_key_rate=1.0, new_key_burst=3)
        assert limiter.check("a")[0]
        assert limiter.check("b")[0]
        assert limiter.check("c")[0]  # evicts "a", spends the last admission token
        assert "a" not in limiter
        # the LRU-forgive path now goes through admission: no token, no burst
        assert not limiter.check("a")[0]


async def test_endpoint_429_with_retry_after() -> None:
    config = make_config(rate_limit=RateLimitConfig(rate_per_second=2.0, burst=2, lru_size=100))
    body = FIXTURE_PATH.read_bytes()
    async with serving_client(config) as client:
        with aioresponses() as mock:
            mock.get(f"{REAPI}?circle=10,20,50&jv2", status=200, body=body, repeat=True)
            r1 = await client.get("/v2/lat/10/lon/20/dist/50")
            r2 = await client.get("/v2/lat/10/lon/20/dist/50")
            r3 = await client.get("/v2/lat/10/lon/20/dist/50")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429
        assert int(r3.headers["Retry-After"]) >= 1
        assert r3.json() == {"msg": "rate limit exceeded"}

        metrics = await client.get("/metrics")
    assert "retina_adsb_http_ratelimit_drops_total 1.0" in metrics.text


class TestTrustedProxyKeying:
    """L3: behind our own nginx front every peer is the proxy; the bucket
    must key on the rightmost X-Forwarded-For entry (the hop appended by
    the trusted proxy) — and ONLY when the peer is in trusted_proxies."""

    URL = "/v2/lat/10/lon/20/dist/50"

    @staticmethod
    def _mock(mock: aioresponses) -> None:
        mock.get(f"{REAPI}?circle=10,20,50&jv2", status=200,
                 body=FIXTURE_PATH.read_bytes(), repeat=True)

    async def test_untrusted_peer_spoofed_xff_keyed_by_peer(self) -> None:
        config = make_config(rate_limit=RateLimitConfig(
            rate_per_second=0.001, burst=1, lru_size=100))  # no trusted proxies
        async with serving_client(config, peer="198.51.100.7") as client:
            with aioresponses() as mock:
                self._mock(mock)
                r1 = await client.get(self.URL, headers={"X-Forwarded-For": "10.0.0.1"})
                r2 = await client.get(self.URL, headers={"X-Forwarded-For": "10.0.0.2"})
            limiter = client.app.state.limiter
        # spoofed distinct XFF did NOT mint fresh buckets: both hit the peer's
        assert (r1.status_code, r2.status_code) == (200, 429)
        assert "198.51.100.7" in limiter
        assert "10.0.0.1" not in limiter
        assert "10.0.0.2" not in limiter

    async def test_trusted_peer_keyed_by_rightmost_xff(self) -> None:
        config = make_config(rate_limit=RateLimitConfig(
            rate_per_second=0.001, burst=1, lru_size=100,
            trusted_proxies=("127.0.0.1",)))
        async with serving_client(config, peer="127.0.0.1") as client:
            with aioresponses() as mock:
                self._mock(mock)
                # rightmost entry = the address our proxy appended; entries
                # left of it are client-supplied garbage and must not matter
                r1 = await client.get(self.URL, headers={"X-Forwarded-For": "6.6.6.6, 10.0.0.1"})
                r2 = await client.get(self.URL, headers={"X-Forwarded-For": "7.7.7.7, 10.0.0.2"})
                r3 = await client.get(self.URL, headers={"X-Forwarded-For": "8.8.8.8, 10.0.0.1"})
            limiter = client.app.state.limiter
        assert (r1.status_code, r2.status_code) == (200, 200)  # distinct rightmost -> distinct buckets
        assert r3.status_code == 429  # same rightmost as r1 -> same (empty) bucket
        assert "10.0.0.1" in limiter
        assert "10.0.0.2" in limiter
        assert "6.6.6.6" not in limiter  # spoofed left entries never become keys
        assert "127.0.0.1" not in limiter

    async def test_trusted_peer_without_xff_falls_back_to_peer(self) -> None:
        config = make_config(rate_limit=RateLimitConfig(
            rate_per_second=0.001, burst=1, lru_size=100,
            trusted_proxies=("127.0.0.1",)))
        async with serving_client(config, peer="127.0.0.1") as client:
            with aioresponses() as mock:
                self._mock(mock)
                r1 = await client.get(self.URL)
                r2 = await client.get(self.URL)
            limiter = client.app.state.limiter
        assert (r1.status_code, r2.status_code) == (200, 429)
        assert "127.0.0.1" in limiter


async def test_health_and_metrics_not_rate_limited() -> None:
    config = make_config(rate_limit=RateLimitConfig(rate_per_second=2.0, burst=1, lru_size=100))
    async with serving_client(config) as client:
        with aioresponses():
            for _ in range(5):
                response = await client.get("/health")
                assert response.status_code in (200, 503)
        for _ in range(5):
            response = await client.get("/metrics")
            assert response.status_code == 200
