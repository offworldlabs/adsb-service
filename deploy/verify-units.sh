#!/usr/bin/env bash
# RETINA ADS-B Phase A — grep-assert the mandated systemd hardening/policy
# keys in deploy/systemd/*.service (DESIGN.md §8). Stand-in for
# systemd-analyze verify where that tool is unavailable (build containers).

set -u

UNIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/systemd" && pwd)"
FAIL=0

assert() {
    # assert <unit-file> <exact line that must be present>
    local unit="$1" line="$2"
    if grep -qxF -- "$line" "$UNIT_DIR/$unit"; then
        echo "ok   $unit: $line"
    else
        echo "FAIL $unit: missing line: $line" >&2
        FAIL=1
    fi
}

assert_absent() {
    local unit="$1" pattern="$2" why="$3"
    if grep -qE -- "$pattern" "$UNIT_DIR/$unit"; then
        echo "FAIL $unit: forbidden pattern present ($why): $pattern" >&2
        FAIL=1
    else
        echo "ok   $unit: absent: $pattern"
    fi
}

for unit in adsbhub-adapter.service readsb-hub.service retina-serving-api.service; do
    [ -f "$UNIT_DIR/$unit" ] || { echo "FAIL missing unit file: $unit" >&2; FAIL=1; continue; }
    # DESIGN.md §8 mandated hardening set
    assert "$unit" "NoNewPrivileges=true"
    assert "$unit" "ProtectSystem=strict"
    assert "$unit" "ProtectHome=true"
    assert "$unit" "PrivateTmp=true"
    assert "$unit" "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX"
    assert "$unit" "SystemCallFilter=@system-service"
    assert "$unit" "SystemCallErrorNumber=EPERM"
    assert "$unit" "PrivateDevices=true"
    assert "$unit" "ProtectProc=invisible"
    assert "$unit" "ProcSubset=pid"
    assert "$unit" "ProtectClock=true"
    assert "$unit" "ProtectHostname=true"
    assert "$unit" "ProtectKernelLogs=true"
    assert "$unit" "Restart=always"
    assert "$unit" "RestartSec=5"
    assert "$unit" "StartLimitIntervalSec=300"
    assert "$unit" "StartLimitBurst=20"
    # No unit may bind or reference a non-loopback listen address
    assert_absent "$unit" '0\.0\.0\.0' "loopback-only deployment (DESIGN.md §1)"
done

assert "adsbhub-adapter.service" "User=retina-adapter"
assert "adsbhub-adapter.service" "MemoryMax=256M"
assert "adsbhub-adapter.service" "ExecStart=/opt/retina-adsb/.venv/bin/adsbhub-adapter --config /etc/retina-adsb/adapter.yaml"

assert "readsb-hub.service" "User=readsb"
assert "readsb-hub.service" "MemoryMax=1G"
assert "readsb-hub.service" "RuntimeDirectory=readsb"
assert "readsb-hub.service" "RuntimeDirectoryMode=0755"
assert "readsb-hub.service" "After=adsbhub-adapter.service"
assert "readsb-hub.service" "Wants=adsbhub-adapter.service"
if grep -q '^ExecStart=/usr/local/bin/readsb-retina ' "$UNIT_DIR/readsb-hub.service"; then
    echo "ok   readsb-hub.service: ExecStart uses /usr/local/bin/readsb-retina"
else
    echo "FAIL readsb-hub.service: ExecStart must use /usr/local/bin/readsb-retina" >&2
    FAIL=1
fi
# DESIGN.md §3 MUST NOTs on the frozen argument line
assert_absent "readsb-hub.service" '\-\-lat[ =]|\-\-lon[ =]' "arms the 450 nmi range filter"
assert_absent "readsb-hub.service" '\-\-json-separate' 'breaks alt_baro:"ground"'
# sbs_debug_port ships disabled: the injection listener bypasses adapter
# validation + the stale guard (test/debug overlays only, sec review)
assert_absent "readsb-hub.service" '\-\-net-sbs-in-port' "validation-bypass debug port must stay disabled by default"

assert "retina-serving-api.service" "User=retina-api"
assert "retina-serving-api.service" "MemoryMax=256M"
assert "retina-serving-api.service" "After=readsb-hub.service"
assert "retina-serving-api.service" "ExecStart=/opt/retina-adsb/.venv/bin/retina-serving-api --config /etc/retina-adsb/serving.yaml"

if [ "$FAIL" -ne 0 ]; then
    echo "verify-units: FAILED" >&2
    exit 1
fi
echo "verify-units: all assertions passed"
