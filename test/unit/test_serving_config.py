"""Config strictness: config_version, unknown keys fatal, --check-config, hash."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from serving_api.__main__ import main
from serving_api.config import ConfigError, load_config, redacted_config_hash


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "serving.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_defaults_from_minimal_config(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "config_version: 1\n"))
    assert config.bind.host == "127.0.0.1"
    assert config.bind.port == 8080
    assert config.reapi_endpoint == "http://127.0.0.1:30152/re-api/"
    assert config.timeouts.total_seconds == 5.0
    assert config.timeouts.connect_seconds == 1.0
    assert config.rate_limit.rate_per_second == 2.0
    assert config.rate_limit.burst == 10
    assert config.rate_limit.lru_size == 10000
    assert config.adapter_health_url == "http://127.0.0.1:9101/healthz"
    assert config.freshness.write_json_every_seconds == 1.0
    assert config.freshness.staleness_factor == 5.0
    assert config.freshness.stale_after_seconds == 5.0
    assert config.rate_limit.new_key_rate == 50.0
    assert config.rate_limit.new_key_burst == 200
    assert config.readsb_response_max_bytes == 32 * 1024 * 1024


def test_defaults_without_file() -> None:
    config = load_config(None)
    assert config.bind.port == 8080
    assert config.rate_limit.burst == 10


def test_full_config_round_trip(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            """\
            config_version: 1
            bind:
              host: "127.0.0.2"
              port: 8081
            reapi_endpoint: "http://127.0.0.1:30153/re-api/"
            timeouts:
              total_seconds: 4.0
              connect_seconds: 0.5
            rate_limit:
              rate_per_second: 1.0
              burst: 5
              lru_size: 500
            adapter_health_url: "http://127.0.0.1:9102/healthz"
            freshness:
              write_json_every_seconds: 0.5
              staleness_factor: 6
            """,
        )
    )
    assert config.bind.host == "127.0.0.2"
    assert config.bind.port == 8081
    assert config.reapi_endpoint == "http://127.0.0.1:30153/re-api/"
    assert config.timeouts.total_seconds == 4.0
    assert config.timeouts.connect_seconds == 0.5
    assert config.rate_limit.burst == 5
    assert config.freshness.stale_after_seconds == 3.0


def test_missing_config_version_fatal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config_version"):
        load_config(_write(tmp_path, "bind:\n  port: 8080\n"))


def test_wrong_config_version_fatal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config_version"):
        load_config(_write(tmp_path, "config_version: 2\n"))


def test_unknown_top_level_key_fatal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="bindd"):
        load_config(_write(tmp_path, "config_version: 1\nbindd: {}\n"))


def test_unknown_nested_key_fatal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="prot"):
        load_config(_write(tmp_path, "config_version: 1\nbind:\n  prot: 8080\n"))
    with pytest.raises(ConfigError, match="bursty"):
        load_config(_write(tmp_path, "config_version: 1\nrate_limit:\n  bursty: 3\n"))


def test_invalid_values_fatal(tmp_path: Path) -> None:
    cases = {
        "bad port": "config_version: 1\nbind:\n  port: 0\n",
        "port too big": "config_version: 1\nbind:\n  port: 70000\n",
        "port string": 'config_version: 1\nbind:\n  port: "8080"\n',
        "port bool": "config_version: 1\nbind:\n  port: true\n",
        "negative rate": "config_version: 1\nrate_limit:\n  rate_per_second: -1\n",
        "zero lru": "config_version: 1\nrate_limit:\n  lru_size: 0\n",
        "zero new-key rate": "config_version: 1\nrate_limit:\n  new_key_rate: 0\n",
        "zero new-key burst": "config_version: 1\nrate_limit:\n  new_key_burst: 0\n",
        "zero response budget": "config_version: 1\nreadsb_response_max_bytes: 0\n",
        "string response budget": 'config_version: 1\nreadsb_response_max_bytes: "1M"\n',
        "connect beyond total": "config_version: 1\ntimeouts:\n  total_seconds: 1\n  connect_seconds: 2\n",
        "bad endpoint": "config_version: 1\nreapi_endpoint: 'not a url'\n",
        "bind not mapping": "config_version: 1\nbind: 42\n",
    }
    for name, text in cases.items():
        with pytest.raises(ConfigError):
            load_config(_write(tmp_path, text))
            pytest.fail(f"case did not raise: {name}")


def test_new_keys_round_trip(tmp_path: Path) -> None:
    config = load_config(_write(
        tmp_path,
        "config_version: 1\n"
        "readsb_response_max_bytes: 1048576\n"
        "rate_limit:\n  new_key_rate: 10\n  new_key_burst: 25\n",
    ))
    assert config.readsb_response_max_bytes == 1048576
    assert config.rate_limit.new_key_rate == 10.0
    assert config.rate_limit.new_key_burst == 25


def test_nonloopback_bind_rejected_by_default(tmp_path: Path) -> None:
    """audit-L8: mirror the adapter loader — 0.0.0.0 must not load cleanly."""
    for host in ("0.0.0.0", "10.0.0.5", "::", "192.0.2.1", "example.com"):
        with pytest.raises(ConfigError, match="allow_nonloopback_bind"):
            load_config(_write(tmp_path, f'config_version: 1\nbind:\n  host: "{host}"\n'))


def test_loopback_binds_accepted(tmp_path: Path) -> None:
    for host in ("127.0.0.1", "127.1.2.3", "::1", "localhost", "LOCALHOST"):
        config = load_config(_write(tmp_path, f'config_version: 1\nbind:\n  host: "{host}"\n'))
        assert config.bind.host == host
        assert config.allow_nonloopback_bind is False


def test_allow_nonloopback_bind_escape_hatch(tmp_path: Path) -> None:
    config = load_config(_write(
        tmp_path,
        'config_version: 1\nallow_nonloopback_bind: true\nbind:\n  host: "10.0.0.5"\n',
    ))
    assert config.bind.host == "10.0.0.5"
    assert config.allow_nonloopback_bind is True


def test_allow_nonloopback_bind_must_be_bool(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="boolean"):
        load_config(_write(tmp_path, 'config_version: 1\nallow_nonloopback_bind: "yes"\n'))


def test_trusted_proxies_default_empty(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "config_version: 1\n"))
    assert config.rate_limit.trusted_proxies == ()


def test_trusted_proxies_round_trip_and_normalization(tmp_path: Path) -> None:
    config = load_config(_write(
        tmp_path,
        "config_version: 1\n"
        "rate_limit:\n  trusted_proxies:\n    - 127.0.0.1\n    - 0:0:0:0:0:0:0:1\n",
    ))
    # IPv6 normalized so runtime peer comparison is representation-independent
    assert config.rate_limit.trusted_proxies == ("127.0.0.1", "::1")


def test_trusted_proxies_invalid_fatal(tmp_path: Path) -> None:
    cases = {
        "not a list": "config_version: 1\nrate_limit:\n  trusted_proxies: 127.0.0.1\n",
        "not an ip": "config_version: 1\nrate_limit:\n  trusted_proxies: [nginx]\n",
        "cidr not allowed": "config_version: 1\nrate_limit:\n  trusted_proxies: [10.0.0.0/24]\n",
        "non-string entry": "config_version: 1\nrate_limit:\n  trusted_proxies: [42]\n",
    }
    for name, text in cases.items():
        with pytest.raises(ConfigError):
            load_config(_write(tmp_path, text))
            pytest.fail(f"case did not raise: {name}")


def test_top_level_not_mapping_fatal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(_write(tmp_path, "- 1\n- 2\n"))


def test_missing_file_fatal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")


def test_redacted_hash_stable_and_sensitive(tmp_path: Path) -> None:
    path = _write(tmp_path, "config_version: 1\n")
    first = redacted_config_hash(load_config(path))
    second = redacted_config_hash(load_config(path))
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    other = redacted_config_hash(load_config(_write(tmp_path, "config_version: 1\nbind:\n  port: 8081\n")))
    assert other != first


def test_check_config_cli_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "config_version: 1\n")
    assert main(["--config", str(path), "--check-config"]) == 0
    out = capsys.readouterr().out
    assert "config ok" in out
    assert "sha256:" in out


def test_check_config_cli_rejects_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "config_version: 1\nsurprise_key: 1\n")
    assert main(["--config", str(path), "--check-config"]) == 2
    err = capsys.readouterr().err
    assert "config error" in err
    assert "surprise_key" in err
