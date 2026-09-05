"""Metrics exposition names/labels (DESIGN.md §4 exact set), the /healthz
contract (spec §14.4 row 1: 200 iff connected AND valid record within 30 s,
degraded 10-30 s), and /livez liveness (spec §23)."""

import aiohttp
import pytest

from adsbhub_adapter.metrics import (
    DELAY_BUCKETS,
    AdapterMetrics,
    HealthState,
    MetricsServer,
)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def exposition(metrics: AdapterMetrics) -> str:
    return metrics.exposition().decode("utf-8")


class TestMetricNames:
    def test_exact_names_and_labels(self):
        m = AdapterMetrics()
        m.add_bytes(0)
        m.reconnect("eof")
        m.observe_delay(0.5)
        m.set_build_info("0.1.0", "deadbeef")
        text = exposition(m)
        assert 'retina_adsb_provider_connected{provider="adsbhub"} 0.0' in text
        assert 'retina_adsb_provider_bytes_total{provider="adsbhub"} 0.0' in text
        for result in ("ok", "invalid_structure", "invalid_range", "stale", "future", "unparseable_ts"):
            assert f'retina_adsb_provider_records_total{{provider="adsbhub",result="{result}"}} 0.0' in text
        assert 'retina_adsb_provider_reconnects_total{provider="adsbhub",reason="eof"} 1.0' in text
        assert "retina_adsb_provider_record_delay_seconds_bucket" in text
        assert 'retina_adsb_provider_record_delay_seconds_count{provider="adsbhub"} 1.0' in text
        assert "retina_adsb_relay_connected_clients 0.0" in text
        assert "retina_adsb_relay_dropped_lines_total 0.0" in text
        # sc-5/ops-8: spec §22.1 name and label set (config_hash kept as extra)
        assert ('retina_adsb_build_info{commit="unknown",config_hash="deadbeef",'
                'schema_version="interim-v2",version="0.1.0"} 1.0') in text

    def test_build_info_commit_from_env(self, monkeypatch):
        monkeypatch.setenv("RETINA_ADSB_COMMIT", "abc1234")
        m = AdapterMetrics()
        m.set_build_info("0.1.0", "deadbeef")
        text = exposition(m)
        assert 'commit="abc1234"' in text
        assert 'schema_version="interim-v2"' in text

    def test_normalized_series_exists_before_any_record(self):
        # ADR-0007: the one normalization series must exist at 0 from startup,
        # so its ratio to records_total is readable across a restart
        m = AdapterMetrics()
        text = exposition(m)
        assert ('retina_adsb_provider_records_normalized_total{normalization="icao_zero_pad",'
                'provider="adsbhub"} 0.0') in text
        m.record_normalized("icao_zero_pad")
        assert ('retina_adsb_provider_records_normalized_total{normalization="icao_zero_pad",'
                'provider="adsbhub"} 1.0') in exposition(m)

    def test_last_record_timestamp_series_exists_before_any_record(self):
        # ops-7: after a restart with a connected-but-recordless upstream the
        # age-based alert clauses need a series, not a gap; 0 is honestly old
        text = exposition(AdapterMetrics())
        assert 'retina_adsb_provider_last_record_timestamp_seconds{provider="adsbhub"} 0.0' in text

    def test_last_record_and_connected_transitions(self):
        m = AdapterMetrics()
        m.set_connected(True)
        m.set_last_record(1756468805.0)
        text = exposition(m)
        assert 'retina_adsb_provider_connected{provider="adsbhub"} 1.0' in text
        assert 'retina_adsb_provider_last_record_timestamp_seconds{provider="adsbhub"} 1.756468805e+09' in text
        m.set_connected(False)
        assert 'retina_adsb_provider_connected{provider="adsbhub"} 0.0' in exposition(m)

    def test_delay_histogram_buckets_span_100ms_to_60s(self):
        assert DELAY_BUCKETS[0] == 0.1
        assert DELAY_BUCKETS[-1] == 60.0
        m = AdapterMetrics()
        m.observe_delay(3.0)
        m.observe_delay(-0.5)  # negative delay clamps to 0, still observed
        text = exposition(m)
        assert 'le="0.1"' in text and 'le="60.0"' in text and 'le="+Inf"' in text
        assert 'retina_adsb_provider_record_delay_seconds_count{provider="adsbhub"} 2.0' in text

    def test_record_counts_by_result(self):
        m = AdapterMetrics()
        m.record("ok")
        m.record("ok")
        m.record("stale")
        text = exposition(m)
        assert 'retina_adsb_provider_records_total{provider="adsbhub",result="ok"} 2.0' in text
        assert 'retina_adsb_provider_records_total{provider="adsbhub",result="stale"} 1.0' in text

    def test_isolated_registries(self):
        # two instances must not collide (prometheus duplicate registration)
        a = AdapterMetrics()
        b = AdapterMetrics()
        a.record("ok")
        assert 'result="ok"} 1.0' in exposition(a)
        assert 'result="ok"} 0.0' in exposition(b)


class TestHealthz:
    async def _serve(self, health: HealthState, clock: FakeClock):
        m = AdapterMetrics()
        server = MetricsServer(m, health, "127.0.0.1", 0, clock=clock)
        await server.start()
        return m, server

    async def _get(self, port: int, path: str):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}{path}") as resp:
                if path == "/healthz":
                    return resp.status, await resp.json()
                return resp.status, await resp.text()

    async def test_503_when_not_connected(self):
        clock = FakeClock()
        health = HealthState(connected=False, last_valid_monotonic=clock.now)
        _, server = await self._serve(health, clock)
        try:
            status, body = await self._get(server.port, "/healthz")
            assert status == 503
            assert body["healthy"] is False
            assert body["state"] == "unhealthy"
            assert body["connected"] is False
            assert body["degraded_after_seconds"] == 10.0
            assert body["unhealthy_after_seconds"] == 30.0
        finally:
            await server.stop()

    async def test_503_when_connected_but_no_valid_record_yet(self):
        clock = FakeClock()
        health = HealthState(connected=True, last_valid_monotonic=None)
        _, server = await self._serve(health, clock)
        try:
            status, body = await self._get(server.port, "/healthz")
            assert status == 503
            assert body["state"] == "unhealthy"
            assert body["last_valid_record_age_seconds"] is None
        finally:
            await server.stop()

    async def test_200_within_30s_then_503_after(self):
        clock = FakeClock()
        health = HealthState(connected=True, last_valid_monotonic=clock.now)
        _, server = await self._serve(health, clock)
        try:
            status, body = await self._get(server.port, "/healthz")
            assert status == 200 and body["healthy"] is True
            assert body["state"] == "healthy"

            # sc-8, spec §14.4 row 1: 10-30 s old = degraded, still HTTP 200
            clock.now += 10.0  # exactly at the degraded threshold: healthy
            status, body = await self._get(server.port, "/healthz")
            assert status == 200 and body["state"] == "healthy"

            clock.now += 0.5
            status, body = await self._get(server.port, "/healthz")
            assert status == 200
            assert body["healthy"] is True
            assert body["state"] == "degraded"

            clock.now += 19.5  # exactly at 30 s: still degraded, still 200
            status, body = await self._get(server.port, "/healthz")
            assert status == 200 and body["state"] == "degraded"

            clock.now += 0.5  # past 30 s without a valid record (spec §14.4 row 1)
            status, body = await self._get(server.port, "/healthz")
            assert status == 503
            assert body["healthy"] is False
            assert body["state"] == "unhealthy"
            assert body["last_valid_record_age_seconds"] == pytest.approx(30.5)
        finally:
            await server.stop()

    async def test_livez_200_even_while_unhealthy(self):
        # sc-7, spec §23: liveness never depends on upstream health — a
        # liveness probe must not restart the adapter during an outage
        clock = FakeClock()
        health = HealthState(connected=False, last_valid_monotonic=None)
        _, server = await self._serve(health, clock)
        try:
            status, _ = await self._get(server.port, "/healthz")
            assert status == 503
            status, body = await self._get(server.port, "/livez")
            assert status == 200
        finally:
            await server.stop()

    async def test_metrics_endpoint_serves_prometheus_text(self):
        clock = FakeClock()
        m, server = await self._serve(HealthState(), clock)
        try:
            m.record("ok")
            status, text = await self._get(server.port, "/metrics")
            assert status == 200
            assert 'retina_adsb_provider_records_total{provider="adsbhub",result="ok"} 1.0' in text
        finally:
            await server.stop()
