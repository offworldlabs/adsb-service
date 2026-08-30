"""Strict YAML configuration for the serving-api.

Conventions per spec §18.1 (shared with the adsbhub-adapter, DESIGN.md §4/§5):
explicit ``config_version: 1``, unknown keys fatal at every level,
``--check-config`` validates without opening sockets, and the process exposes
a redacted configuration hash.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_VERSION = 1


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class BindConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class TimeoutsConfig:
    total_seconds: float = 5.0
    connect_seconds: float = 1.0


@dataclass(frozen=True)
class RateLimitConfig:
    rate_per_second: float = 2.0
    burst: int = 10
    lru_size: int = 10000
    # sec-7: global admission budget for previously-unseen client keys, so
    # rotating > lru_size sources cannot mint a fresh burst per evicted key
    new_key_rate: float = 50.0
    new_key_burst: int = 200
    # Peer IPs (the deploy/nginx proxies) whose X-Forwarded-For rightmost
    # entry keys the bucket instead of the peer address itself. Empty by
    # default: XFF from anything not listed here is IGNORED (spoofable).
    trusted_proxies: tuple[str, ...] = ()


@dataclass(frozen=True)
class FreshnessConfig:
    # DESIGN §5: envelope considered stale when wall - now > factor * write-json-every.
    write_json_every_seconds: float = 1.0
    staleness_factor: float = 5.0

    @property
    def stale_after_seconds(self) -> float:
        return self.staleness_factor * self.write_json_every_seconds


@dataclass(frozen=True)
class ServingConfig:
    bind: BindConfig = field(default_factory=BindConfig)
    # sec: mirrors the adapter loader — a non-loopback bind must be explicit
    # (spec §6.1 S1 / AC-S-01: external exposure only via the Q5 boundary).
    allow_nonloopback_bind: bool = False
    reapi_endpoint: str = "http://127.0.0.1:30152/re-api/"
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    adapter_health_url: str = "http://127.0.0.1:9101/healthz"
    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    # sec-3: upstream body budget; oversize maps to 502 upstream-unavailable
    readsb_response_max_bytes: int = 32 * 1024 * 1024


_TOP_LEVEL_KEYS = {
    "config_version",
    "bind",
    "allow_nonloopback_bind",
    "reapi_endpoint",
    "timeouts",
    "rate_limit",
    "adapter_health_url",
    "freshness",
    "readsb_response_max_bytes",
}

# spec §18.1: the exposed config hash must be redacted. This schema carries no
# secrets today; the generic redaction keeps the convention if any are added.
_SECRET_KEY_RE = re.compile(r"secret|token|password|passphrase|credential|private", re.IGNORECASE)


def _reject_unknown(section: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in {context}: {', '.join(unknown)}")


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _string(section: dict[str, Any], key: str, default: str, context: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value


def _number(
    section: dict[str, Any],
    key: str,
    default: float,
    context: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{context}.{key} must be a number")
    value = float(value)
    if minimum is not None and value < minimum:
        raise ConfigError(f"{context}.{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{context}.{key} must be <= {maximum}")
    return value


def _integer(
    section: dict[str, Any],
    key: str,
    default: int,
    context: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context}.{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{context}.{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{context}.{key} must be <= {maximum}")
    return value


def _bool(section: dict[str, Any], key: str, default: bool, context: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{context}.{key} must be a boolean")
    return value


def _ip_list(section: dict[str, Any], key: str, context: str) -> tuple[str, ...]:
    value = section.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{context}.{key} must be a list of IP addresses")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{context}.{key} entries must be IP address strings")
        try:
            # Normalize (e.g. IPv6 zero-compression) so runtime peer
            # comparison is textual-representation independent.
            normalized.append(str(ipaddress.ip_address(item)))
        except ValueError as exc:
            raise ConfigError(f"{context}.{key}: {item!r} is not an IP address") from exc
    return tuple(normalized)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback  # 127.0.0.0/8, ::1
    except ValueError:
        return False


def _url(section: dict[str, Any], key: str, default: str, context: str) -> str:
    value = _string(section, key, default, context)
    if not value.startswith(("http://", "https://")):
        raise ConfigError(f"{context}.{key} must be an http(s) URL")
    return value


def _parse(raw: dict[str, Any]) -> ServingConfig:
    if "config_version" not in raw:
        raise ConfigError("config_version is required")
    version = raw["config_version"]
    if version != CONFIG_VERSION:
        raise ConfigError(f"unsupported config_version {version!r} (expected {CONFIG_VERSION})")
    _reject_unknown(raw, _TOP_LEVEL_KEYS, "top level")

    bind_raw = _section(raw, "bind")
    _reject_unknown(bind_raw, {"host", "port"}, "bind")
    bind = BindConfig(
        host=_string(bind_raw, "host", "127.0.0.1", "bind"),
        port=_integer(bind_raw, "port", 8080, "bind", minimum=1, maximum=65535),
    )
    allow_nonloopback = _bool(raw, "allow_nonloopback_bind", False, "top level")
    if not allow_nonloopback and not _is_loopback_host(bind.host):
        # Mirrors services/adsbhub_adapter/config.py: loopback-only by
        # default (spec §6.1 S1 / AC-S-01 — external exposure happens only
        # through the operator-enabled Q5 boundary, never a direct bind).
        raise ConfigError(
            f"bind.host: {bind.host!r} is not a loopback address; the serving-api must stay "
            "loopback-only (AC-S-01 default-closed; off-host access goes through the Q5 "
            "nginx boundary) — set allow_nonloopback_bind: true to bind a private-network "
            "address deliberately"
        )

    timeouts_raw = _section(raw, "timeouts")
    _reject_unknown(timeouts_raw, {"total_seconds", "connect_seconds"}, "timeouts")
    total = _number(timeouts_raw, "total_seconds", 5.0, "timeouts", minimum=0.001)
    connect = _number(timeouts_raw, "connect_seconds", 1.0, "timeouts", minimum=0.001)
    if connect > total:
        raise ConfigError("timeouts.connect_seconds must not exceed timeouts.total_seconds")
    timeouts = TimeoutsConfig(total_seconds=total, connect_seconds=connect)

    rate_raw = _section(raw, "rate_limit")
    _reject_unknown(rate_raw, {"rate_per_second", "burst", "lru_size",
                               "new_key_rate", "new_key_burst", "trusted_proxies"}, "rate_limit")
    rate_limit = RateLimitConfig(
        rate_per_second=_number(rate_raw, "rate_per_second", 2.0, "rate_limit", minimum=0.001),
        burst=_integer(rate_raw, "burst", 10, "rate_limit", minimum=1),
        lru_size=_integer(rate_raw, "lru_size", 10000, "rate_limit", minimum=1),
        new_key_rate=_number(rate_raw, "new_key_rate", 50.0, "rate_limit", minimum=0.001),
        new_key_burst=_integer(rate_raw, "new_key_burst", 200, "rate_limit", minimum=1),
        trusted_proxies=_ip_list(rate_raw, "trusted_proxies", "rate_limit"),
    )

    freshness_raw = _section(raw, "freshness")
    _reject_unknown(freshness_raw, {"write_json_every_seconds", "staleness_factor"}, "freshness")
    freshness = FreshnessConfig(
        write_json_every_seconds=_number(
            freshness_raw, "write_json_every_seconds", 1.0, "freshness", minimum=0.1
        ),
        staleness_factor=_number(freshness_raw, "staleness_factor", 5.0, "freshness", minimum=1.0),
    )

    return ServingConfig(
        bind=bind,
        allow_nonloopback_bind=allow_nonloopback,
        reapi_endpoint=_url(raw, "reapi_endpoint", "http://127.0.0.1:30152/re-api/", "top level"),
        timeouts=timeouts,
        rate_limit=rate_limit,
        adapter_health_url=_url(raw, "adapter_health_url", "http://127.0.0.1:9101/healthz", "top level"),
        freshness=freshness,
        readsb_response_max_bytes=_integer(
            raw, "readsb_response_max_bytes", 32 * 1024 * 1024, "top level", minimum=1
        ),
    )


def load_config(path: str | Path | None = None) -> ServingConfig:
    """Load and validate config; ``None`` yields the documented defaults."""
    if path is None:
        return _parse({"config_version": CONFIG_VERSION})
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("top-level config must be a mapping")
    return _parse(raw)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _SECRET_KEY_RE.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def redacted_config_hash(config: ServingConfig) -> str:
    """sha256 over the canonical redacted effective config (spec §18.1)."""
    effective = {"config_version": CONFIG_VERSION, **asdict(config)}
    canonical = json.dumps(_redact(effective), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
