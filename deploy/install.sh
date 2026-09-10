#!/usr/bin/env bash
# RETINA ADS-B Phase A — idempotent single-host install (DESIGN.md §8, ADR-0003).
#
# Installs:  /opt/retina-adsb        code + venv (root-owned, world-readable)
#            /etc/retina-adsb        configs (root-owned, 0640, group = service user)
#            /usr/local/bin/readsb-retina   pinned readsb build (ADR-0002)
#            /etc/systemd/system/{adsbhub-adapter,readsb-hub,retina-serving-api}.service
#
# Does NOT: start/enable any unit, touch any firewall, or enable a Q5 nginx
# boundary. The stack ships default-closed (AC-S-01); see the printed
# post-install steps and deploy/README.md.
#
# Usage: deploy/install.sh [--install-deps] [--skip-port-check]
#   --install-deps    let build-readsb.sh apt-get install missing build packages
#   --skip-port-check skip the spec §19 port-collision precondition (not advised)
#
# Safe to rerun: users are created once, existing /etc configs are never
# clobbered (a changed example lands next to them as *.dist), code/venv/units
# are refreshed in place.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPT_DIR="/opt/retina-adsb"
ETC_DIR="/etc/retina-adsb"
VENV="$OPT_DIR/.venv"

INSTALL_DEPS_FLAG=()
SKIP_PORT_CHECK=0
for arg in "$@"; do
    case "$arg" in
        --install-deps)   INSTALL_DEPS_FLAG=(--install-deps) ;;
        --skip-port-check) SKIP_PORT_CHECK=1 ;;
        -h|--help)        grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install: unknown argument: $arg" >&2; exit 1 ;;
    esac
done

die() { echo "install: ERROR: $*" >&2; exit 1; }
log() { echo "install: $*"; }

[ "$(id -u)" -eq 0 ] || die "must run as root"
command -v rsync >/dev/null || die "rsync is required (apt-get install -y rsync)"
command -v python3 >/dev/null || die "python3 is required"

# --- 1. Port-collision precondition (spec §19: 'No deployment automation may
#        assume these ports are free') -- tolerate our own already-running units.
check_port() {
    local port="$1" unit="$2" what="$3"
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        return 0
    fi
    if [ -n "$(ss -Hltn "sport = :$port" 2>/dev/null)" ]; then
        die "port $port ($what) is already in use by a foreign process and \
$unit is not the one running. Resolve the collision first (spec §19 install \
precondition), or rerun with --skip-port-check if you know better."
    fi
}
if [ "$SKIP_PORT_CHECK" -eq 0 ]; then
    check_port 31003 adsbhub-adapter.service   "adapter SBS relay"
    check_port 9101  adsbhub-adapter.service   "adapter metrics/health"
    check_port 30152 readsb-hub.service        "readsb re-api"
    check_port 31004 readsb-hub.service        "readsb Beast contribution listener (loopback, ADR-0008)"
    # 32006 (readsb debug SBS inject) not checked: sbs_debug_port ships
    # commented out (validation-bypass port, test/debug overlays only).
    # 7444 (public feed front) belongs to nginx and is opened only by
    # deploy/enable-feed-port.sh, never here.
    check_port 8080  retina-serving-api.service "serving-api"
    log "port-collision check passed (31003 9101 30152 31004 8080)"
else
    log "port-collision check SKIPPED by request"
fi

# --- 2. System users (DESIGN.md §8: three units, three users) ---------------
for user in retina-adapter readsb retina-api; do
    if ! getent passwd "$user" >/dev/null; then
        useradd --system --user-group --no-create-home \
                --home-dir /nonexistent --shell /usr/sbin/nologin "$user"
        log "created system user $user"
    fi
done

# --- 3. Code -> /opt/retina-adsb (root-owned; services run from it read-only)
install -d -m 0755 "$OPT_DIR"
rsync -a --delete "$REPO_ROOT/services/" "$OPT_DIR/services/"
install -m 0644 "$REPO_ROOT/pyproject.toml" "$OPT_DIR/pyproject.toml"
install -m 0644 "$REPO_ROOT/LICENSE" "$OPT_DIR/LICENSE"
log "code synced to $OPT_DIR"

# --- 4. venv + project install ----------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || die "venv creation failed (apt-get install -y python3-venv?)"
    log "created venv at $VENV"
fi
# First call resolves dependencies (needs the index once); the second forces
# the freshly-synced project code in even when the version number is unchanged.
"$VENV/bin/pip" install --quiet "$OPT_DIR"
"$VENV/bin/pip" install --quiet --no-deps --force-reinstall "$OPT_DIR"
log "project installed into $VENV"

# --- 5. Configs -> /etc/retina-adsb (root:serviceuser 0640; never clobber) --
install -d -m 0755 "$ETC_DIR"
install_config() {
    local src="$1" dst="$2" group="$3"
    if [ ! -f "$dst" ]; then
        install -o root -g "$group" -m 0640 "$src" "$dst"
        log "installed $dst"
    elif ! cmp -s "$src" "$dst"; then
        install -o root -g "$group" -m 0640 "$src" "$dst.dist"
        log "NOTE: $dst exists and differs from the repo example; new example left at $dst.dist"
    else
        chown "root:$group" "$dst"; chmod 0640 "$dst"
    fi
}
install_config "$REPO_ROOT/config/adapter.yaml" "$ETC_DIR/adapter.yaml" retina-adapter
install_config "$REPO_ROOT/config/serving.yaml" "$ETC_DIR/serving.yaml" retina-api
# readsb.yaml is read only by gen-readsb-args.py (run by root), never by a service.
install_config "$REPO_ROOT/config/readsb.yaml"  "$ETC_DIR/readsb.yaml"  root

# Stamp the installed commit for retina_adsb_build_info{commit} (spec §22.1).
# World-readable: contains only the public commit id.
RETINA_COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf 'RETINA_ADSB_COMMIT=%s\n' "$RETINA_COMMIT" > "$ETC_DIR/build.env"
chmod 0644 "$ETC_DIR/build.env"
log "stamped $ETC_DIR/build.env (commit $RETINA_COMMIT)"

# --- 6. Pinned readsb build (ADR-0002) --------------------------------------
bash "$REPO_ROOT/deploy/build-readsb.sh" "${INSTALL_DEPS_FLAG[@]}"

# --- 7. Gates: every config must validate before units are (re)installed ----
log "gate: adsbhub-adapter --check-config"
"$VENV/bin/adsbhub-adapter" --check-config --config "$ETC_DIR/adapter.yaml" \
    || die "adapter.yaml failed --check-config"
log "gate: retina-serving-api --check-config"
"$VENV/bin/retina-serving-api" --check-config --config "$ETC_DIR/serving.yaml" \
    || die "serving.yaml failed --check-config"

log "gate: gen-readsb-args.py vs frozen unit ExecStart (spec §18.3)"
gen_line="$("$VENV/bin/python" "$REPO_ROOT/deploy/gen-readsb-args.py" \
    --config "$ETC_DIR/readsb.yaml" --systemd)" \
    || die "readsb.yaml refused by gen-readsb-args.py"
unit_line="$(grep '^ExecStart=' "$REPO_ROOT/deploy/systemd/readsb-hub.service")"
if [ "$gen_line" != "$unit_line" ]; then
    die "readsb-hub.service ExecStart does not match the args generated from \
$ETC_DIR/readsb.yaml. The generated args are the contract (spec §18.3): \
regenerate the unit line with \
'deploy/gen-readsb-args.py --config $ETC_DIR/readsb.yaml --systemd', paste it \
into deploy/systemd/readsb-hub.service, and rerun install.
  generated: $gen_line
  unit:      $unit_line"
fi

# --- 8. systemd units (installed, daemon-reloaded, NOT started/enabled) -----
for unit in adsbhub-adapter.service readsb-hub.service retina-serving-api.service; do
    install -m 0644 "$REPO_ROOT/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
log "units installed and daemon-reloaded (not enabled, not started)"

# --- 9. Post-install instructions -------------------------------------------
cat <<'EOF'

============================================================================
RETINA ADS-B Phase A installed. NOTHING has been started, and no firewall
rule was touched (the stack is loopback-only / default-closed, AC-S-01).

BEFORE FIRST START — ADSBHub prerequisites (plan.md A0):
  * Whitelist THIS host's stable egress IP on the ADSBHub profile page.
    ADSBHub opens data.adsbhub.org:5002 only for profile-configured IPs;
    a missing/stale whitelist looks exactly like provider-down. If this
    host is ever rebuilt or moved, update the whitelist BEFORE cutover
    (prefer a reserved/floating IP so it never changes).
  * The designated ADSBHub contribution station is the receiver at
    47.46.206.146 — it must be actively feeding ADSBHub from its own
    decoder (never hub/central data) for the data entitlement to exist.

Start (data-path order; enable for boot as you see fit):
  systemctl enable --now adsbhub-adapter readsb-hub retina-serving-api

Verify:
  curl -s http://127.0.0.1:9101/healthz          # adapter (503 until ADSBHub delivers)
  curl -s 'http://127.0.0.1:30152/re-api/?status' # readsb re-api
  curl -s http://127.0.0.1:8080/health            # serving-api
  curl -s 'http://127.0.0.1:8080/v2/lat/37.6/lon/-122.4/dist/50'

External exposure stays CLOSED until you enable it by hand (deploy/README.md):
  deploy/enable-public-front.sh <host> --proxied   # nodes: GET /v2 over HTTPS (ADR-0005)
  deploy/enable-feed-port.sh [--tls-host <host>]   # feeders: Beast in on TCP 7444, no account (ADR-0008)
============================================================================
EOF
