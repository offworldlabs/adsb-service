"""The generated readsb args are the contract (spec §18.3, DESIGN.md §3).

Pure-file test — no sockets, no readsb binary. Runs deploy/gen-readsb-args.py
against the shipped config/readsb.yaml in both output modes and asserts:

  (a) the argument vector is exactly the DESIGN.md §3 set (with the one
      recorded delta: `--net-sbs-in-port` is ABSENT because sbs_debug_port
      ships commented out — the debug injection listener bypasses adapter
      validation and the ADR-0004 stale guard, so it is disabled by default;
      delta recorded in docs/conformance-ledger.md "New surfaces");
  (b) the --systemd output equals the frozen ExecStart= line in
      deploy/systemd/readsb-hub.service verbatim.

install.sh performs the same ExecStart comparison at install time against
/etc/retina-adsb/readsb.yaml; this test is the repo-side gate so a drifted
unit line or generator change fails in CI, not at install.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "deploy" / "gen-readsb-args.py"
CONFIG = REPO_ROOT / "config" / "readsb.yaml"
UNIT = REPO_ROOT / "deploy" / "systemd" / "readsb-hub.service"

# DESIGN.md §3 argument set, order preserved. §3 additionally shows
# --net-sbs-in-port=32006; the shipped default disables it (see module
# docstring) — enabling sbs_debug_port restores it, covered below.
EXPECTED_ARGS = [
    "--net",
    "--quiet",
    "--net-bind-address=127.0.0.1",
    "--net-connector=127.0.0.1,31003,sbs_in",
    "--net-connector-delay=5",
    "--max-range=99999",
    "--write-json=/run/readsb",
    "--write-json-every=1",
    "--json-reliable=1",
    "--net-api-port=30152",
    "--api-shutdown-delay=2",
    "--write-prom=/run/readsb/stats.prom",
]

BINARY = "/usr/local/bin/readsb-retina"


def run_generator(*extra: str, config: Path = CONFIG) -> str:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--config", str(config), *extra],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"gen-readsb-args.py exited {result.returncode}: {result.stderr}"
    )
    return result.stdout


def test_default_mode_emits_design_section3_vector() -> None:
    args = run_generator().splitlines()
    assert args == EXPECTED_ARGS


def test_systemd_mode_matches_frozen_unit_execstart() -> None:
    gen_line = run_generator("--systemd").strip()
    unit_lines = [
        line
        for line in UNIT.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    ]
    assert len(unit_lines) == 1, f"expected exactly one ExecStart=, got {unit_lines}"
    assert gen_line == unit_lines[0], (
        "deploy/systemd/readsb-hub.service ExecStart drifted from the "
        "generator output — regenerate it with "
        "`deploy/gen-readsb-args.py --config config/readsb.yaml --systemd` "
        "(spec §18.3: the generated args are the contract)"
    )


def test_systemd_mode_is_binary_plus_default_vector() -> None:
    # The two output modes must describe the same invocation.
    gen_line = run_generator("--systemd").strip()
    assert gen_line == f"ExecStart={BINARY} {' '.join(EXPECTED_ARGS)}"


def test_debug_port_stays_disabled_in_shipped_config() -> None:
    # sec review: the frozen production vector must not carry the
    # validation-bypass injection listener.
    assert not any(a.startswith("--net-sbs-in-port") for a in run_generator().splitlines())


def run_generator_expect_refusal(config: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--config", str(config)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f"expected exit 2 (refused), got {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "gen-readsb-args: error:" in result.stderr
    return result.stderr


def overlay_config(tmp_path: Path, **overrides: object) -> Path:
    """The shipped config with top-level (or relay.*) keys overridden."""
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        section, _, sub = key.partition("__")
        if sub:
            cfg[section][sub] = value
        else:
            cfg[section] = value
    path = tmp_path / "readsb.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


class TestFieldInjectionRefused:
    """sec (M2): structured config fields are interpolated into the argv and
    systemd word-splits ExecStart — every substituted string must be a single
    clean token, or a crafted value smuggles flags (e.g. --lat, which arms
    the 450 nmi range filter) past the extra_args forbidden-flag scan."""

    @pytest.mark.parametrize("value", [
        "127.0.0.1 --lat=51.5",           # word-split smuggle
        "127.0.0.1\t--lon=0",
        "127.0.0.1 --json-separate-ground",
        " 127.0.0.1",                      # whitespace alone must be refused
        "",
    ])
    def test_bind_address_injection_refused(self, tmp_path: Path, value: str) -> None:
        err = run_generator_expect_refusal(overlay_config(tmp_path, bind_address=value))
        assert "bind_address" in err

    @pytest.mark.parametrize("value", [
        "127.0.0.1 --lat=51.5",
        # a comma would splice extra --net-connector parameters into the token
        "127.0.0.1,30005,beast_in",
        "evil host",
        "",
    ])
    def test_relay_host_injection_refused(self, tmp_path: Path, value: str) -> None:
        err = run_generator_expect_refusal(overlay_config(tmp_path, relay__host=value))
        assert "relay.host" in err

    @pytest.mark.parametrize("value", [
        "/run/readsb --lat=51.5",
        "/run/read sb",
        "/run/readsb\n--lon=0",
        "run/readsb",                      # relative path
        "",
    ])
    def test_write_json_dir_injection_refused(self, tmp_path: Path, value: str) -> None:
        err = run_generator_expect_refusal(overlay_config(tmp_path, write_json_dir=value))
        assert "write_json_dir" in err

    @pytest.mark.parametrize("key,value", [
        ("max_range", float("inf")),
        ("connector_delay", float("nan")),
    ])
    def test_nonfinite_numbers_refused(self, tmp_path: Path, key: str, value: float) -> None:
        err = run_generator_expect_refusal(overlay_config(tmp_path, **{key: value}))
        assert key in err

    def test_clean_config_roundtrip_is_byte_identical(self, tmp_path: Path) -> None:
        # the added validation must not perturb the frozen §3 vector
        overlay = overlay_config(tmp_path)  # parse + re-dump, no changes
        assert run_generator(config=overlay) == run_generator()
        assert run_generator().splitlines() == EXPECTED_ARGS


def test_debug_port_overlay_restores_sbs_in_port(tmp_path: Path) -> None:
    # A test/debug overlay that sets sbs_debug_port gets the §3 listener
    # back, placed before --write-prom as DESIGN.md §3 shows it.
    overlay = tmp_path / "readsb.yaml"
    overlay.write_text(
        CONFIG.read_text(encoding="utf-8") + "\nsbs_debug_port: 32006\n",
        encoding="utf-8",
    )
    args = run_generator(config=overlay).splitlines()
    expected = EXPECTED_ARGS[:-1] + ["--net-sbs-in-port=32006", EXPECTED_ARGS[-1]]
    assert args == expected
