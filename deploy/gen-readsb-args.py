#!/usr/bin/env python3
"""Generate the readsb-hub argument vector from config/readsb.yaml.

THE GENERATED ARGUMENTS ARE THE CONTRACT (spec §18.3): the deployment MUST
generate the readsb arguments for the pinned build from configuration and
verify them. Never hand-edit the readsb invocation in
deploy/systemd/readsb-hub.service — change config/readsb.yaml, rerun this
script with --systemd, and paste the emitted ExecStart line. Two gates hold
the contract: test/unit/test_deploy_args.py asserts the generated vector is
the DESIGN.md §3 set and that --systemd output equals the unit's frozen
ExecStart line; install.sh re-verifies the unit line against this generator
(over /etc/retina-adsb/readsb.yaml) and refuses to install on mismatch.

Output modes:
  (default)   one argument per line — the exact argv the unit must carry
  --systemd   a single "ExecStart=<binary> <args...>" line for the unit file

Exit codes: 0 ok; 2 config invalid or refused (forbidden flag).
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

import yaml

DEFAULT_BINARY = "/usr/local/bin/readsb-retina"

# sec: structured config fields are interpolated into the argv verbatim, and
# systemd word-splits ExecStart — so every substituted string must be a single
# clean token or a value like `bind_address: "127.0.0.1 --lat=51.5"` would
# smuggle a forbidden flag past the extra_args scan.
HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")  # hostnames, IPv4, IPv6 literals

# DESIGN.md §3: these flags MUST NOT be set on the aggregate hub.
# --lat/--lon arm readsb's 450 nmi max-range filter and silently drop far
# SBS positions; --json-separate-(alt-)ground breaks the alt_baro:"ground"
# string the v2 envelope consumers expect; --net-api-port accepts exactly
# one port, already emitted from api_port.
FORBIDDEN_FLAGS = {
    "--lat": 'would arm the 450 nmi range filter and drop far SBS positions (DESIGN.md §3)',
    "--lon": 'would arm the 450 nmi range filter and drop far SBS positions (DESIGN.md §3)',
    "--json-separate-ground": 'would break the alt_baro:"ground" v2 envelope (DESIGN.md §3)',
    "--json-separate-alt-ground": 'would break the alt_baro:"ground" v2 envelope (DESIGN.md §3)',
    "--net-api-port": "readsb accepts a single api port; set api_port instead (DESIGN.md §3)",
    "--max-range": "set max_range instead of duplicating the flag",
    "--write-json": "set write_json_dir instead of duplicating the flag",
    "--net-connector": "set relay.host/relay.port instead of duplicating the flag",
    "--json-reliable": "set json_reliable instead of duplicating the flag",
    # ADR-0008: the Beast contribution listener and --net-ingest are emitted
    # as a PAIR from beast_feed_port. A Beast listener without --net-ingest
    # would let any feeder set this hub's receiver location (radarcape
    # position frame → arms the range filter) and would skip readsb's
    # per-feeder rate/garbage limits and PROXY-header parsing.
    "--net-bi-port": "set beast_feed_port instead (emits --net-bi-port AND --net-ingest together, ADR-0008)",
    "--net-ingest": "emitted together with --net-bi-port from beast_feed_port; never set it separately (ADR-0008)",
    "--devel": "set ingest_limit_messages_per_second / ingest_limit_positions_per_second instead; other devel modes are unsupported on the hub",
}

# --net-ingest applies its per-client message/position rate limits to EVERY
# input client, the ADSBHub SBS relay connector included. readsb's defaults
# (5420 msg/s, 300 positions/s — sized for one physical receiver) flag the
# aggregate SBS stream as garbage within seconds and every ADSBHub position
# stops being served (observed live 2026-09-06: 0 positions after restart).
# They are therefore raised to bounds an aggregate never reaches while a
# single feeder still cannot exceed them (nginx's 1 MB/s per connection cap
# is ~45k Beast messages/s).
INGEST_LIMIT_DEFAULTS = {
    "ingest_limit_messages_per_second": 60000,
    "ingest_limit_positions_per_second": 20000,
}

TOP_KEYS_REQUIRED = {
    "config_version",
    "relay",
    "api_port",
    "bind_address",
    "write_json_dir",
    "write_json_every",
    "max_range",
    "connector_delay",
    "api_shutdown_delay",
}
TOP_KEYS_OPTIONAL = {
    "sbs_debug_port", "extra_args", "beast_feed_port", "json_reliable",
    *INGEST_LIMIT_DEFAULTS,
}
RELAY_KEYS = {"host", "port"}


def fail(msg: str) -> NoReturn:
    print(f"gen-readsb-args: error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _unclean(text: str) -> bool:
    """Whitespace or control characters: would word-split or corrupt the argv."""
    return any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def require_host(key: str, value: Any) -> str:
    if not isinstance(value, str) or not HOST_RE.fullmatch(value):
        fail(f"{key}: must be a single host/address token matching [A-Za-z0-9_.:-]+ "
             f"(no whitespace, commas, or option syntax), got {value!r}")
    return value


def require_abs_path(key: str, value: Any) -> str:
    if (not isinstance(value, str) or not value.startswith("/")
            or _unclean(value)):
        fail(f"{key}: must be an absolute path without whitespace or control "
             f"characters, got {value!r}")
    return value


def require_positive_number(key: str, value: Any) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0):
        fail(f"{key} must be a positive finite number, got {value!r}")
    return value


def fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def require_port(cfg: dict[str, Any], key: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        fail(f"{key}: expected a TCP port (1-65535), got {value!r}")
    return value


def load_config(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        fail(f"cannot read {path}: {exc}")
    except yaml.YAMLError as exc:
        fail(f"cannot parse {path}: {exc}")
    if not isinstance(raw, dict):
        fail(f"{path}: top level must be a mapping")
    return raw


def validate(cfg: dict[str, Any], path: Path) -> dict[str, Any]:
    # spec §18.1: explicit config_version, unknown keys fatal
    unknown = set(cfg) - TOP_KEYS_REQUIRED - TOP_KEYS_OPTIONAL
    if unknown:
        fail(f"{path}: unknown keys (fatal per spec §18.1): {', '.join(sorted(unknown))}")
    missing = TOP_KEYS_REQUIRED - set(cfg)
    if missing:
        fail(f"{path}: missing required keys: {', '.join(sorted(missing))}")
    if cfg["config_version"] != 1:
        fail(f"{path}: config_version must be 1, got {cfg['config_version']!r}")

    relay = cfg["relay"]
    if not isinstance(relay, dict):
        fail(f"{path}: relay must be a mapping with host/port")
    unknown = set(relay) - RELAY_KEYS
    if unknown:
        fail(f"{path}: unknown relay keys (fatal per spec §18.1): {', '.join(sorted(unknown))}")
    if RELAY_KEYS - set(relay):
        fail(f"{path}: relay requires host and port")
    require_host(f"{path}: relay.host", relay["host"])
    require_port(cfg, "relay.port", relay["port"])
    require_port(cfg, "api_port", cfg["api_port"])
    if "sbs_debug_port" in cfg and cfg["sbs_debug_port"] is not None:
        require_port(cfg, "sbs_debug_port", cfg["sbs_debug_port"])
    if cfg.get("beast_feed_port") is not None:
        require_port(cfg, "beast_feed_port", cfg["beast_feed_port"])
    # Every listener readsb opens must be a distinct port or it fails to bind.
    listeners = {
        key: cfg[key]
        for key in ("api_port", "sbs_debug_port", "beast_feed_port")
        if cfg.get(key) is not None
    }
    listeners["relay.port"] = relay["port"]
    seen: dict[int, str] = {}
    for key, port in listeners.items():
        if port in seen:
            fail(f"{path}: {key} and {seen[port]} both use port {port}")
        seen[port] = key
    for key in INGEST_LIMIT_DEFAULTS:
        if key in cfg and cfg[key] is not None:
            if cfg.get("beast_feed_port") is None:
                fail(f"{path}: {key} only applies with beast_feed_port set (it tunes --net-ingest)")
            v = cfg[key]
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                fail(f"{path}: {key} must be a positive integer, got {v!r}")
    if cfg.get("json_reliable") is not None:
        jr = cfg["json_reliable"]
        # readsb clamps to -1..4 (readsb.c:1893-1897); below 1 would serve
        # single-message positions from anonymous feeders (ADR-0008).
        if not isinstance(jr, int) or isinstance(jr, bool) or not 1 <= jr <= 4:
            fail(f"{path}: json_reliable must be an integer 1-4, got {jr!r}")

    for key in ("connector_delay", "api_shutdown_delay", "max_range", "write_json_every"):
        require_positive_number(f"{path}: {key}", cfg[key])
    # readsb floors the json interval at 100 ms (readsb.c:1878-1880)
    if cfg["write_json_every"] < 0.1:
        fail(f"{path}: write_json_every below readsb's 0.1 s floor")

    require_abs_path(f"{path}: write_json_dir", cfg["write_json_dir"])
    require_host(f"{path}: bind_address", cfg["bind_address"])
    if cfg["bind_address"] != "127.0.0.1":
        # DESIGN.md §1/§2: all readsb listeners stay loopback in Phase A
        print(
            f"gen-readsb-args: warning: bind_address {cfg['bind_address']!r} is not "
            "127.0.0.1 — every readsb listener will bind it (AI-06/spec §19: "
            "loopback/private only)",
            file=sys.stderr,
        )

    extra = cfg.get("extra_args") or []
    if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
        fail(f"{path}: extra_args must be a list of strings")
    for arg in extra:
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            fail(f"{path}: refusing forbidden flag {flag!r} in extra_args: {FORBIDDEN_FLAGS[flag]}")
        if not flag.startswith("--"):
            fail(f"{path}: extra_args entries must be --long-options, got {arg!r}")
    return cfg


def build_args(cfg: dict[str, Any]) -> list[str]:
    # Argument vector per DESIGN.md §3 — order preserved.
    relay = cfg["relay"]
    json_dir = cfg["write_json_dir"].rstrip("/")
    args = [
        "--net",
        "--quiet",
        f"--net-bind-address={cfg['bind_address']}",
        f"--net-connector={relay['host']},{fmt(relay['port'])},sbs_in",
        f"--net-connector-delay={fmt(cfg['connector_delay'])}",
        f"--max-range={fmt(cfg['max_range'])}",
        f"--write-json={json_dir}",
        f"--write-json-every={fmt(cfg['write_json_every'])}",
        # pinned explicitly (1 when unset — the pre-ADR-0008 vector);
        # shipped config sets 2, see DESIGN.md §3
        f"--json-reliable={fmt(cfg.get('json_reliable') or 1)}",
        f"--net-api-port={fmt(cfg['api_port'])}",
        f"--api-shutdown-delay={fmt(cfg['api_shutdown_delay'])}",
    ]
    if cfg.get("sbs_debug_port") is not None:
        args.append(f"--net-sbs-in-port={fmt(cfg['sbs_debug_port'])}")
    if cfg.get("beast_feed_port") is not None:
        # ADR-0008: always as a pair (see FORBIDDEN_FLAGS).
        args.append(f"--net-bi-port={fmt(cfg['beast_feed_port'])}")
        args.append("--net-ingest")
        # readsb splits --devel on commas: <name>,<value> (readsb.c OptDevel)
        msgs = cfg.get("ingest_limit_messages_per_second") or INGEST_LIMIT_DEFAULTS["ingest_limit_messages_per_second"]
        pos = cfg.get("ingest_limit_positions_per_second") or INGEST_LIMIT_DEFAULTS["ingest_limit_positions_per_second"]
        args.append(f"--devel=ingestLimitRate,{fmt(msgs)}")
        args.append(f"--devel=ingestLimitPositionRate,{fmt(pos)}")
    args.append(f"--write-prom={json_dir}/stats.prom")
    args.extend(cfg.get("extra_args") or [])
    scan_tokens(args)
    return args


# The flags build_args itself emits; exempt from the final forbidden-flag scan
# (some, e.g. --net-connector, are in FORBIDDEN_FLAGS only to stop extra_args
# from duplicating them).
GENERATED_FLAGS = frozenset({
    "--net", "--quiet", "--net-bind-address", "--net-connector",
    "--net-connector-delay", "--max-range", "--write-json",
    "--write-json-every", "--json-reliable", "--net-api-port",
    "--api-shutdown-delay", "--net-sbs-in-port", "--net-bi-port",
    "--net-ingest", "--devel", "--write-prom",
})


def scan_tokens(args: list[str]) -> None:
    """Defense in depth over the FINAL argv (per-field validation is the first
    gate): no token may word-split under systemd, and no forbidden flag may
    appear anywhere the generator did not itself emit it."""
    for token in args:
        if _unclean(token):
            fail(f"generated token contains whitespace/control characters "
                 f"(would word-split in the systemd unit): {token!r}")
        flag = token.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS and flag not in GENERATED_FLAGS:
            fail(f"refusing forbidden flag {flag!r} in generated argv: {FORBIDDEN_FLAGS[flag]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate readsb-hub arguments from config/readsb.yaml (spec §18.3)."
    )
    parser.add_argument("--config", required=True, type=Path, help="path to readsb.yaml")
    parser.add_argument(
        "--systemd",
        action="store_true",
        help="emit a single ExecStart= line for deploy/systemd/readsb-hub.service",
    )
    parser.add_argument(
        "--binary",
        default=DEFAULT_BINARY,
        help=f"readsb binary path for --systemd mode (default {DEFAULT_BINARY})",
    )
    opts = parser.parse_args()

    cfg = validate(load_config(opts.config), opts.config)
    args = build_args(cfg)
    if opts.systemd:
        print(f"ExecStart={opts.binary} {' '.join(args)}")
    else:
        print("\n".join(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
