"""Strict YAML configuration for the adsbhub-adapter.

Rules per spec §18.1 (via DESIGN.md §4): explicit ``config_version``, unknown
keys fatal at any nesting depth, ``--check-config`` never opens sockets, and
the process exposes a redacted configuration hash.  Key names and defaults
follow the spec §18.2 adsbhub block; behavioral resolutions (full jitter,
idle-timeout semantics, timestamp guard bounds) are DESIGN.md §4 / ADR-0006.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_VERSION = 1

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_DURATION_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s|m)?\s*$")
_REDACT_MARKERS = ("secret", "password", "token", "credential", "api_key", "apikey")
_REDACTED = "**REDACTED**"


class ConfigError(Exception):
    """Fatal configuration problem (spec §18.1: bad or unknown config is fatal)."""


def parse_duration(value: Any, key: str) -> float:
    """Parse a duration into seconds: bare numbers or '250ms' / '10s' / '1m'."""
    if isinstance(value, bool):
        raise ConfigError(f"{key}: expected a duration, got a boolean")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        m = _DURATION_RE.match(value)
        if not m:
            raise ConfigError(f"{key}: unparseable duration {value!r} (use e.g. 250ms, 10s, 1m)")
        number = float(m.group(1))
        unit = m.group(2) or "s"
        seconds = {"ms": number / 1000.0, "s": number, "m": number * 60.0}[unit]
    else:
        raise ConfigError(f"{key}: expected a duration, got {type(value).__name__}")
    if not math.isfinite(seconds) or seconds < 0:
        raise ConfigError(f"{key}: duration must be finite and >= 0, got {value!r}")
    return seconds


def _require_mapping(value: Any, path: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected a mapping, got {type(value).__name__}")
    return value


def _reject_unknown(section: dict, allowed: tuple[str, ...], path: str) -> None:
    unknown = sorted(str(k) for k in section if k not in allowed)
    if unknown:
        raise ConfigError(
            f"unknown key(s) under {path}: {', '.join(unknown)} (unknown keys are fatal, spec §18.1)"
        )


def _get_str(section: dict, key: str, default: str, path: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{path}.{key}: expected a string, got {type(value).__name__}")
    return value


def _get_int(section: dict, key: str, default: int, path: str, *, minimum: int, maximum: int | None = None) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key}: expected an integer, got {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        hi = maximum if maximum is not None else "inf"
        raise ConfigError(f"{path}.{key}: must be in [{minimum}, {hi}], got {value}")
    return value


def _get_bool(section: dict, key: str, default: bool, path: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key}: expected a boolean, got {value!r}")
    return value


def _get_number(section: dict, key: str, default: float, path: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}.{key}: expected a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{path}.{key}: must be finite, got {value!r}")
    return number


def _get_duration(section: dict, key: str, default: float, path: str, *, minimum: float = 0.0,
                  minimum_exclusive: bool = False) -> float:
    raw = section.get(key)
    seconds = default if raw is None else parse_duration(raw, f"{path}.{key}")
    if seconds < minimum or (minimum_exclusive and seconds == minimum):
        op = ">" if minimum_exclusive else ">="
        raise ConfigError(f"{path}.{key}: must be {op} {minimum}s, got {seconds}s")
    return seconds


@dataclass(frozen=True)
class ReconnectConfig:
    initial: float = 1.0
    maximum: float = 60.0
    # Accepted for spec §18.2 key compatibility; jitter mode is FULL jitter
    # regardless (DESIGN.md §4 / ADR-0006), so the ratio is not consumed.
    jitter_ratio: float = 0.20
    stable_reset: float = 60.0


@dataclass(frozen=True)
class UpstreamConfig:
    host: str = "data.adsbhub.org"
    port: int = 5002
    connect_timeout: float = 10.0
    idle_timeout: float = 30.0
    line_max_bytes: int = 4096
    # NOTE: no timestamp_timezone knob — SBS timestamps are always UTC because
    # pinned readsb hardcodes UTC via timegm (net_io.c:3153); a configurable
    # timezone would make the validator read the same relayed bytes
    # differently than readsb does (ADR-0006 §8).
    future_tolerance: float = 1.0
    max_record_age: float = 60.0
    # spec §12.2: altitude window is configurable (sc-3)
    altitude_min_ft: float = -2000.0
    altitude_max_ft: float = 100000.0
    # spec §18.2 keys, consumed by the (deferred) canonical serving layer,
    # not by the adapter data path; accepted so operator configs validate.
    provider_age_penalty: float = 1.0
    ingest_uncertainty_floor: float = 1.0
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)


@dataclass(frozen=True)
class RelayConfig:
    host: str = "127.0.0.1"
    port: int = 31003
    max_clients: int = 2
    client_queue_max_lines: int = 20000
    heartbeat_interval: float = 25.0


@dataclass(frozen=True)
class MetricsConfig:
    host: str = "127.0.0.1"
    port: int = 9101


@dataclass(frozen=True)
class DiagnosticsConfig:
    # spec §21.5 / §6.1 S1: bounded, redacted diagnostic sample, opt-in only,
    # with operator reason and duration limit.
    sample_enabled: bool = False
    sample_reason: str = ""
    sample_duration: float = 60.0
    sample_max_lines: int = 100


@dataclass(frozen=True)
class AdapterConfig:
    config_version: int = CONFIG_VERSION
    log_level: str = "INFO"
    # spec §6.1 S1 escape hatch: allow a deliberate private-net bind (sc-9)
    allow_nonloopback_bind: bool = False
    adsbhub: UpstreamConfig = field(default_factory=UpstreamConfig)
    relay: RelayConfig = field(default_factory=RelayConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)

    def to_dict(self) -> dict:
        return {
            "config_version": self.config_version,
            "log_level": self.log_level,
            "allow_nonloopback_bind": self.allow_nonloopback_bind,
            "adsbhub": {
                "host": self.adsbhub.host,
                "port": self.adsbhub.port,
                "connect_timeout": self.adsbhub.connect_timeout,
                "idle_timeout": self.adsbhub.idle_timeout,
                "line_max_bytes": self.adsbhub.line_max_bytes,
                "future_tolerance": self.adsbhub.future_tolerance,
                "max_record_age": self.adsbhub.max_record_age,
                "altitude_min_ft": self.adsbhub.altitude_min_ft,
                "altitude_max_ft": self.adsbhub.altitude_max_ft,
                "provider_age_penalty": self.adsbhub.provider_age_penalty,
                "ingest_uncertainty_floor": self.adsbhub.ingest_uncertainty_floor,
                "reconnect": {
                    "initial": self.adsbhub.reconnect.initial,
                    "maximum": self.adsbhub.reconnect.maximum,
                    "jitter_ratio": self.adsbhub.reconnect.jitter_ratio,
                    "stable_reset": self.adsbhub.reconnect.stable_reset,
                },
            },
            "relay": {
                "host": self.relay.host,
                "port": self.relay.port,
                "max_clients": self.relay.max_clients,
                "client_queue_max_lines": self.relay.client_queue_max_lines,
                "heartbeat_interval": self.relay.heartbeat_interval,
            },
            "metrics": {
                "host": self.metrics.host,
                "port": self.metrics.port,
            },
            "diagnostics": {
                "sample_enabled": self.diagnostics.sample_enabled,
                "sample_reason": self.diagnostics.sample_reason,
                "sample_duration": self.diagnostics.sample_duration,
                "sample_max_lines": self.diagnostics.sample_max_lines,
            },
        }


def _load_reconnect(section: dict, path: str) -> ReconnectConfig:
    _reject_unknown(section, ("initial", "maximum", "jitter_ratio", "stable_reset"), path)
    initial = _get_duration(section, "initial", 1.0, path, minimum=0.0, minimum_exclusive=True)
    maximum = _get_duration(section, "maximum", 60.0, path, minimum=0.0, minimum_exclusive=True)
    if maximum < initial:
        raise ConfigError(f"{path}.maximum ({maximum}s) must be >= {path}.initial ({initial}s)")
    jitter_raw = section.get("jitter_ratio", 0.20)
    if isinstance(jitter_raw, bool) or not isinstance(jitter_raw, (int, float)):
        raise ConfigError(f"{path}.jitter_ratio: expected a number, got {jitter_raw!r}")
    jitter = float(jitter_raw)
    if not 0.0 <= jitter <= 1.0:
        raise ConfigError(f"{path}.jitter_ratio: must be in [0, 1], got {jitter}")
    stable_reset = _get_duration(section, "stable_reset", 60.0, path)
    return ReconnectConfig(initial=initial, maximum=maximum, jitter_ratio=jitter, stable_reset=stable_reset)


def _load_adsbhub(section: dict, path: str) -> UpstreamConfig:
    # timestamp_timezone (spec §18.2) is deliberately NOT accepted: readsb
    # hardcodes UTC (timegm, net_io.c:3153), so the validator does too, and a
    # config carrying the key fails loudly instead of being silently ignored
    # (ADR-0006 §8).
    _reject_unknown(section, (
        "host", "port", "connect_timeout", "idle_timeout", "line_max_bytes",
        "future_tolerance", "max_record_age",
        "altitude_min_ft", "altitude_max_ft",
        "provider_age_penalty", "ingest_uncertainty_floor", "reconnect",
    ), path)
    altitude_min = _get_number(section, "altitude_min_ft", -2000.0, path)
    altitude_max = _get_number(section, "altitude_max_ft", 100000.0, path)
    if altitude_min >= altitude_max:
        raise ConfigError(
            f"{path}.altitude_min_ft ({altitude_min}) must be < {path}.altitude_max_ft ({altitude_max})")
    return UpstreamConfig(
        host=_get_str(section, "host", "data.adsbhub.org", path),
        port=_get_int(section, "port", 5002, path, minimum=1, maximum=65535),
        connect_timeout=_get_duration(section, "connect_timeout", 10.0, path, minimum=0.0, minimum_exclusive=True),
        idle_timeout=_get_duration(section, "idle_timeout", 30.0, path, minimum=0.0, minimum_exclusive=True),
        # must at least hold readsb's 199-byte max valid SBS line
        line_max_bytes=_get_int(section, "line_max_bytes", 4096, path, minimum=200),
        future_tolerance=_get_duration(section, "future_tolerance", 1.0, path),
        max_record_age=_get_duration(section, "max_record_age", 60.0, path, minimum=0.0, minimum_exclusive=True),
        altitude_min_ft=altitude_min,
        altitude_max_ft=altitude_max,
        provider_age_penalty=_get_duration(section, "provider_age_penalty", 1.0, path),
        ingest_uncertainty_floor=_get_duration(section, "ingest_uncertainty_floor", 1.0, path),
        reconnect=_load_reconnect(_require_mapping(section.get("reconnect"), f"{path}.reconnect"), f"{path}.reconnect"),
    )


def _load_relay(section: dict, path: str) -> RelayConfig:
    _reject_unknown(section, ("host", "port", "max_clients", "client_queue_max_lines", "heartbeat_interval"), path)
    return RelayConfig(
        host=_get_str(section, "host", "127.0.0.1", path),
        port=_get_int(section, "port", 31003, path, minimum=0, maximum=65535),
        max_clients=_get_int(section, "max_clients", 2, path, minimum=1),
        client_queue_max_lines=_get_int(section, "client_queue_max_lines", 20000, path, minimum=1),
        heartbeat_interval=_get_duration(section, "heartbeat_interval", 25.0, path, minimum=0.0, minimum_exclusive=True),
    )


def _load_metrics(section: dict, path: str) -> MetricsConfig:
    _reject_unknown(section, ("host", "port"), path)
    return MetricsConfig(
        host=_get_str(section, "host", "127.0.0.1", path),
        port=_get_int(section, "port", 9101, path, minimum=0, maximum=65535),
    )


def _load_diagnostics(section: dict, path: str) -> DiagnosticsConfig:
    _reject_unknown(section, ("sample_enabled", "sample_reason", "sample_duration", "sample_max_lines"), path)
    cfg = DiagnosticsConfig(
        sample_enabled=_get_bool(section, "sample_enabled", False, path),
        sample_reason=_get_str(section, "sample_reason", "", path),
        sample_duration=_get_duration(section, "sample_duration", 60.0, path, minimum=0.0, minimum_exclusive=True),
        sample_max_lines=_get_int(section, "sample_max_lines", 100, path, minimum=1),
    )
    if cfg.sample_enabled and not cfg.sample_reason.strip():
        # spec §21.5: diagnostic captures require an operator-supplied reason
        raise ConfigError(f"{path}.sample_reason is required when {path}.sample_enabled is true")
    return cfg


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback  # 127.0.0.0/8, ::1
    except ValueError:
        return False


def load_config_dict(data: Any) -> AdapterConfig:
    root = _require_mapping(data, "top level")
    _reject_unknown(root, ("config_version", "log_level", "allow_nonloopback_bind",
                           "adsbhub", "relay", "metrics", "diagnostics"), "top level")
    if "config_version" not in root:
        raise ConfigError("config_version is required (spec §18.1)")
    version = root["config_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != CONFIG_VERSION:
        raise ConfigError(f"config_version must be {CONFIG_VERSION}, got {version!r}")
    log_level = _get_str(root, "log_level", "INFO", "top level").upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"log_level must be one of {', '.join(_LOG_LEVELS)}, got {log_level!r}")
    allow_nonloopback = _get_bool(root, "allow_nonloopback_bind", False, "top level")
    relay = _load_relay(_require_mapping(root.get("relay"), "relay"), "relay")
    metrics = _load_metrics(_require_mapping(root.get("metrics"), "metrics"), "metrics")
    if not allow_nonloopback:
        for key, host in (("relay.host", relay.host), ("metrics.host", metrics.host)):
            if not _is_loopback_host(host):
                raise ConfigError(
                    f"{key}: {host!r} is not a loopback address; spec §6.1 S1 mandates a "
                    "loopback-only relay and forbids an Internet-reachable listener — set "
                    "allow_nonloopback_bind: true to bind a private-network address deliberately"
                )
    return AdapterConfig(
        config_version=version,
        log_level=log_level,
        allow_nonloopback_bind=allow_nonloopback,
        adsbhub=_load_adsbhub(_require_mapping(root.get("adsbhub"), "adsbhub"), "adsbhub"),
        relay=relay,
        metrics=metrics,
        diagnostics=_load_diagnostics(_require_mapping(root.get("diagnostics"), "diagnostics"), "diagnostics"),
    )


def load_config(path: str | Path) -> AdapterConfig:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    return load_config_dict(data)


def _redact(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if any(marker in key.lower() for marker in _REDACT_MARKERS):
        return _REDACTED
    return value


def redacted_config_hash(cfg: AdapterConfig) -> str:
    """SHA-256 over the canonical, redacted effective configuration (spec §18.1)."""
    blob = json.dumps(_redact(cfg.to_dict()), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
