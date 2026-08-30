"""Config strictness tests (spec §18.1 via DESIGN.md §4): config_version
required, unknown keys fatal at any nesting, defaults, durations, and the
--check-config redacted hash."""

import re

import pytest

from adsbhub_adapter import __main__ as adapter_main
from adsbhub_adapter.config import (
    ConfigError,
    load_config,
    load_config_dict,
    parse_duration,
    redacted_config_hash,
)

MINIMAL = {"config_version": 1}


class TestDefaults:
    def test_minimal_config_gets_design_defaults(self):
        cfg = load_config_dict(MINIMAL)
        assert cfg.config_version == 1
        assert cfg.adsbhub.host == "data.adsbhub.org"
        assert cfg.adsbhub.port == 5002
        assert cfg.adsbhub.connect_timeout == 10.0
        assert cfg.adsbhub.idle_timeout == 30.0
        assert cfg.adsbhub.line_max_bytes == 4096
        assert cfg.adsbhub.future_tolerance == 1.0
        assert cfg.adsbhub.max_record_age == 60.0
        assert cfg.adsbhub.altitude_min_ft == -2000.0
        assert cfg.adsbhub.altitude_max_ft == 100000.0
        assert cfg.allow_nonloopback_bind is False
        assert cfg.adsbhub.reconnect.initial == 1.0
        assert cfg.adsbhub.reconnect.maximum == 60.0
        assert cfg.adsbhub.reconnect.stable_reset == 60.0
        assert cfg.relay.host == "127.0.0.1"
        assert cfg.relay.port == 31003
        assert cfg.relay.max_clients == 2
        assert cfg.relay.client_queue_max_lines == 20000
        assert cfg.relay.heartbeat_interval == 25.0
        assert cfg.metrics.host == "127.0.0.1"
        assert cfg.metrics.port == 9101
        assert cfg.diagnostics.sample_enabled is False

    def test_spec_18_2_example_keys_accepted(self):
        # NOTE: spec §18.2 also lists timestamp_timezone; it is deliberately
        # NOT accepted — readsb hardcodes UTC (timegm, net_io.c:3153), so the
        # knob was removed for relay parity (ADR-0006 §8, covered below).
        cfg = load_config_dict({
            "config_version": 1,
            "adsbhub": {
                "host": "data.adsbhub.org",
                "port": 5002,
                "connect_timeout": "10s",
                "idle_timeout": "30s",
                "line_max_bytes": 4096,
                "provider_age_penalty": "1s",
                "ingest_uncertainty_floor": "1s",
                "reconnect": {"initial": "1s", "maximum": "60s", "jitter_ratio": 0.20},
            },
        })
        assert cfg.adsbhub.provider_age_penalty == 1.0
        assert cfg.adsbhub.reconnect.jitter_ratio == 0.20

    @pytest.mark.parametrize("tz", ["UTC", "Europe/Berlin"])
    def test_timestamp_timezone_knob_removed(self, tz):
        # ADR-0006 §8: even an explicit 'UTC' fails loudly rather than being
        # silently accepted-and-ignored — the validator's UTC is not a config
        with pytest.raises(ConfigError, match="timestamp_timezone"):
            load_config_dict({"config_version": 1,
                              "adsbhub": {"timestamp_timezone": tz}})


class TestVersionGate:
    def test_missing_version_fatal(self):
        with pytest.raises(ConfigError, match="config_version"):
            load_config_dict({})

    @pytest.mark.parametrize("version", [2, 0, "1", 1.0, True, None])
    def test_wrong_version_fatal(self, version):
        with pytest.raises(ConfigError, match="config_version"):
            load_config_dict({"config_version": version})


class TestUnknownKeys:
    @pytest.mark.parametrize("data,fragment", [
        ({"config_version": 1, "bogus": 1}, "bogus"),
        ({"config_version": 1, "adsbhub": {"bogus_key": 1}}, "bogus_key"),
        ({"config_version": 1, "adsbhub": {"reconnect": {"jitter": 0.2}}}, "jitter"),
        ({"config_version": 1, "relay": {"listen_port": 31003}}, "listen_port"),
        ({"config_version": 1, "metrics": {"path": "/metrics"}}, "path"),
        ({"config_version": 1, "diagnostics": {"verbose": True}}, "verbose"),
    ])
    def test_unknown_key_fatal_at_any_nesting(self, data, fragment):
        with pytest.raises(ConfigError, match=fragment):
            load_config_dict(data)


class TestTypesAndBounds:
    @pytest.mark.parametrize("data", [
        {"config_version": 1, "adsbhub": {"port": "5002"}},
        {"config_version": 1, "adsbhub": {"port": 70000}},
        {"config_version": 1, "adsbhub": {"host": 5}},
        {"config_version": 1, "adsbhub": {"line_max_bytes": 100}},  # < max SBS line
        {"config_version": 1, "adsbhub": {"idle_timeout": "0s"}},
        {"config_version": 1, "adsbhub": {"idle_timeout": "-5s"}},
        {"config_version": 1, "adsbhub": {"reconnect": {"initial": "2m", "maximum": "60s"}}},
        {"config_version": 1, "adsbhub": {"reconnect": {"jitter_ratio": 1.5}}},
        {"config_version": 1, "relay": {"max_clients": 0}},
        {"config_version": 1, "relay": {"client_queue_max_lines": 0}},
        # altitude window (sc-3): must be numeric with min < max
        {"config_version": 1, "adsbhub": {"altitude_min_ft": "low"}},
        {"config_version": 1, "adsbhub": {"altitude_max_ft": True}},
        {"config_version": 1, "adsbhub": {"altitude_min_ft": 500, "altitude_max_ft": 500}},
        {"config_version": 1, "adsbhub": {"altitude_min_ft": 1000, "altitude_max_ft": -1000}},
        {"config_version": 1, "adsbhub": "not-a-mapping"},
        {"config_version": 1, "log_level": "CHATTY"},
        {"config_version": 1, "diagnostics": {"sample_enabled": True}},  # no reason (§21.5)
        "not a mapping at all",
        ["also", "not", "a", "mapping"],
    ])
    def test_bad_values_fatal(self, data):
        with pytest.raises(ConfigError):
            load_config_dict(data)

    def test_none_config_fatal(self):
        with pytest.raises(ConfigError):
            load_config_dict(None)

    def test_altitude_bounds_configurable(self):
        cfg = load_config_dict({
            "config_version": 1,
            "adsbhub": {"altitude_min_ft": -3000, "altitude_max_ft": 150000.5},
        })
        assert cfg.adsbhub.altitude_min_ft == -3000.0
        assert cfg.adsbhub.altitude_max_ft == 150000.5


class TestLoopbackBindGuard:
    """sc-9: spec §6.1 S1 mandates a loopback-only relay; non-loopback binds
    need the explicit allow_nonloopback_bind escape hatch."""

    @pytest.mark.parametrize("section,host", [
        ("relay", "0.0.0.0"),
        ("relay", "192.168.1.5"),
        ("relay", "::"),
        ("metrics", "0.0.0.0"),
        ("metrics", "10.0.0.7"),
        ("relay", "example.com"),  # non-address hostnames are not provably loopback
    ])
    def test_nonloopback_bind_fatal_by_default(self, section, host):
        with pytest.raises(ConfigError, match=r"§6\.1"):
            load_config_dict({"config_version": 1, section: {"host": host}})

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "127.255.0.1", "::1", "localhost"])
    def test_loopback_hosts_accepted(self, host):
        cfg = load_config_dict({"config_version": 1, "relay": {"host": host},
                                "metrics": {"host": host}})
        assert cfg.relay.host == host

    def test_escape_hatch_allows_private_net_bind(self):
        cfg = load_config_dict({
            "config_version": 1,
            "allow_nonloopback_bind": True,
            "relay": {"host": "192.168.1.5"},
            "metrics": {"host": "0.0.0.0"},
        })
        assert cfg.allow_nonloopback_bind is True
        assert cfg.relay.host == "192.168.1.5"

    def test_escape_hatch_must_be_boolean(self):
        with pytest.raises(ConfigError):
            load_config_dict({"config_version": 1, "allow_nonloopback_bind": "yes"})


class TestDurations:
    @pytest.mark.parametrize("value,expected", [
        ("250ms", 0.25), ("10s", 10.0), ("1m", 60.0), ("1.5s", 1.5),
        (30, 30.0), (0.5, 0.5), ("60 s", 60.0),
    ])
    def test_parse(self, value, expected):
        assert parse_duration(value, "k") == expected

    @pytest.mark.parametrize("value", ["fast", "10x", "-1s", "", None, True, [1]])
    def test_unparseable_fatal(self, value):
        with pytest.raises(ConfigError):
            parse_duration(value, "k")


class TestConfigHash:
    def test_stable_and_hex(self):
        a = redacted_config_hash(load_config_dict({"config_version": 1}))
        b = redacted_config_hash(load_config_dict({"config_version": 1, "adsbhub": {"port": 5002}}))
        assert a == b  # explicit default == default
        assert re.fullmatch(r"[0-9a-f]{64}", a)

    def test_changes_with_values(self):
        a = redacted_config_hash(load_config_dict({"config_version": 1}))
        b = redacted_config_hash(load_config_dict({"config_version": 1, "adsbhub": {"port": 5003}}))
        assert a != b


class TestFileAndCheckConfig:
    def test_load_config_file(self, tmp_path):
        path = tmp_path / "adapter.yaml"
        path.write_text("config_version: 1\nadsbhub:\n  idle_timeout: 30s\n")
        cfg = load_config(path)
        assert cfg.adsbhub.idle_timeout == 30.0

    def test_missing_file_fatal(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot read"):
            load_config(tmp_path / "nope.yaml")

    def test_invalid_yaml_fatal(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("config_version: [unclosed\n")
        with pytest.raises(ConfigError, match="YAML"):
            load_config(path)

    def test_check_config_prints_hash_and_exits_zero(self, tmp_path, capsys):
        path = tmp_path / "adapter.yaml"
        path.write_text("config_version: 1\n")
        rc = adapter_main.main(["--config", str(path), "--check-config"])
        assert rc == 0
        out = capsys.readouterr().out
        match = re.search(r"config_hash: sha256:([0-9a-f]{64})", out)
        assert match
        assert match.group(1) == redacted_config_hash(load_config(path))

    def test_check_config_bad_file_exits_nonzero(self, tmp_path, capsys):
        path = tmp_path / "adapter.yaml"
        path.write_text("config_version: 1\nsurprise_key: true\n")
        rc = adapter_main.main(["--config", str(path), "--check-config"])
        assert rc == 2
        assert "surprise_key" in capsys.readouterr().err
