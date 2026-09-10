#!/usr/bin/env bash
# RETINA ADS-B — enable the account-less Beast contribution port, TCP 7444
# (ADR-0008: adsb.lol-style open ingest; TLS optional, no client certificate).
#
#   sudo deploy/enable-feed-port.sh [--tls-host <hostname> [--email <acme-email>]]
#   sudo deploy/enable-feed-port.sh --disable
#
# --tls-host  also accept TLS on 7444, presenting the Let's Encrypt
#             certificate for <hostname> (obtained here if missing). Feeders
#             that want privacy in transit wrap their Beast connector in
#             stunnel/socat; plain Beast keeps working on the same port.
#             <hostname> must resolve DIRECTLY to this host (DNS-only record):
#             Cloudflare's proxy does not carry raw TCP, so the feed port is
#             reached at the origin address. Reusing the public front's
#             hostname works only for its certificate (feeders then connect
#             to this host's IP with SNI/verification set to that name).
# --disable   remove the front and close 7444 again; readsb keeps its
#             loopback listener (serve-only until re-enabled).
#
# Idempotent. Each run:
#   1. Installs nginx's stream module (libnginx-mod-stream) and adds
#      `stream { include /etc/nginx/retina-stream.d/*.conf; }` to nginx.conf.
#   2. Checks readsb-hub really carries the loopback Beast listener + --net-ingest
#      pair generated from beast_feed_port in /etc/retina-adsb/readsb.yaml
#      and that it is listening (the front is useless — and refused — otherwise).
#   3. --tls-host: ensures a certificate (authoritative-DNS + routing probe,
#      certbot webroot HTTP-01; opens 80/tcp from anywhere for validation and
#      renewals when the host firewall is on).
#   4. Renders deploy/nginx/feed.conf → /etc/nginx/retina-stream.d/retina-adsb-feed.conf
#      (TLS block stripped in plain-only mode), nginx -t, reload.
#   5. Host firewall: `ufw allow 7444/tcp` (rules are re-applied by
#      enable-public-front.sh's ufw reset via /etc/retina-adsb/feed-port.env).
#   6. Verifies end to end through the public address: a plain Beast
#      heartbeat (and a TLS one when enabled) must reach readsb, which logs
#      the feeder's REAL address from the PROXY header on disconnect.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/deploy/nginx/feed.conf"
STREAM_DIR="/etc/nginx/retina-stream.d"
DST="$STREAM_DIR/retina-adsb-feed.conf"
NGINX_CONF="/etc/nginx/nginx.conf"
READSB_YAML="/etc/retina-adsb/readsb.yaml"
UNIT="/etc/systemd/system/readsb-hub.service"
FEED_ENV="/etc/retina-adsb/feed-port.env"
ACME_ROOT="/var/www/retina-acme"
ACME_80_CONF="/etc/nginx/conf.d/retina-acme-80.conf"
PUBLIC_CONF="/etc/nginx/conf.d/retina-adsb-public.conf"
VENV="/opt/retina-adsb/.venv"
FEED_PORT=7444
TLS_PORT=7445

die() { echo "enable-feed-port: ERROR: $*" >&2; exit 1; }
log() { echo "enable-feed-port: $*"; }

TLS_HOST=""
EMAIL_ARGS=(--register-unsafely-without-email)
DISABLE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --tls-host) TLS_HOST="${2:-}"; [ -n "$TLS_HOST" ] || die "--tls-host needs a hostname"; shift 2 ;;
        --email) EMAIL_ARGS=(--email "$2" --no-eff-email); shift 2 ;;
        --disable) DISABLE=1; shift ;;
        -h|--help) grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[ "$(id -u)" -eq 0 ] || die "must run as root"

ufw_active() { command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; }

# --- --disable --------------------------------------------------------------
if [ "$DISABLE" -eq 1 ]; then
    rm -f "$DST" "$FEED_ENV"
    if command -v nginx >/dev/null && nginx -t >/dev/null 2>&1; then systemctl reload nginx; fi
    if command -v ufw >/dev/null; then ufw delete allow "$FEED_PORT/tcp" >/dev/null 2>&1 || true; fi
    log "feed port $FEED_PORT disabled (front removed, firewall rule deleted); readsb's loopback Beast listener is untouched"
    exit 0
fi

[ -f "$SRC" ] || die "missing $SRC"
[ -f "$READSB_YAML" ] || die "$READSB_YAML missing — run deploy/install.sh first"
[ -x "$VENV/bin/python" ] || die "$VENV missing — run deploy/install.sh first"
for tool in curl dig ss; do command -v "$tool" >/dev/null || die "$tool is required"; done

# --- 1. Packages + stream include ------------------------------------------
pkgs=()
command -v nginx >/dev/null || pkgs+=(nginx)
[ -f /usr/lib/nginx/modules/ngx_stream_module.so ] || pkgs+=(libnginx-mod-stream)
[ -z "$TLS_HOST" ] || command -v certbot >/dev/null || pkgs+=(certbot)
if [ "${#pkgs[@]}" -gt 0 ]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${pkgs[@]}"
    log "installed: ${pkgs[*]}"
fi
rm -f /etc/nginx/sites-enabled/default
install -d -m 0755 "$STREAM_DIR"
if ! grep -q "retina-stream.d" "$NGINX_CONF"; then
    printf '\n# RETINA ADS-B Beast contribution front (deploy/enable-feed-port.sh, ADR-0008)\nstream {\n    include %s/*.conf;\n}\n' "$STREAM_DIR" >> "$NGINX_CONF"
    log "added stream include to $NGINX_CONF"
fi

# --- 2. readsb side must exist first ---------------------------------------
BEAST_PORT="$("$VENV/bin/python" - "$READSB_YAML" <<'EOF'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
p = cfg.get("beast_feed_port")
print(p if isinstance(p, int) and not isinstance(p, bool) else "")
EOF
)"
[ -n "$BEAST_PORT" ] || die "beast_feed_port is not set in $READSB_YAML. Set it (31004 per DESIGN.md §2), regenerate the unit line with deploy/gen-readsb-args.py --systemd, rerun deploy/install.sh and restart readsb-hub."
grep -qE -- "--net-bi-port=$BEAST_PORT --net-ingest( |$)" "$UNIT" \
    || die "$UNIT does not carry '--net-bi-port=$BEAST_PORT --net-ingest' — rerun deploy/install.sh (ADR-0008 pair)"
systemctl is-active --quiet readsb-hub || die "readsb-hub is not running"
ss -Hltn "sport = :$BEAST_PORT" | grep -q "127.0.0.1:$BEAST_PORT" \
    || die "readsb is not listening on 127.0.0.1:$BEAST_PORT — restart readsb-hub after installing the updated unit"
log "readsb-hub listens on 127.0.0.1:$BEAST_PORT with --net-ingest"

mapfile -t MY_IPS < <(hostname -I | tr ' ' '\n' | grep -E '^[0-9.]+$')
[ "${#MY_IPS[@]}" -gt 0 ] || die "cannot determine this host's IPv4 address"
PUBLIC_IP="${MY_IPS[0]}"

# --- 3. Certificate for --tls-host -----------------------------------------
HTTP01_ANY=0
if [ -n "$TLS_HOST" ]; then
    CERT_DIR="/etc/letsencrypt/live/$TLS_HOST"
    if [ -s "$CERT_DIR/fullchain.pem" ]; then
        log "using existing certificate $CERT_DIR"
    else
        # The name must route straight here (DNS-only): ask the authoritative
        # servers, the local resolver may cache a negative answer.
        zone="${TLS_HOST#*.}"; NS=()
        while [ -n "$zone" ] && [ "$zone" != "$TLS_HOST" ]; do
            mapfile -t NS < <(dig +short NS "$zone" 2>/dev/null | grep -v '^$')
            [ "${#NS[@]}" -gt 0 ] && break
            zone="${zone#*.}"
        done
        [ "${#NS[@]}" -gt 0 ] || die "cannot find the authoritative nameservers for $TLS_HOST"
        mapfile -t DNS_IPS < <(dig +short A "$TLS_HOST" "@${NS[0]}" 2>/dev/null | grep -E '^[0-9.]+$' | sort -u)
        [ "${#DNS_IPS[@]}" -gt 0 ] || die "$TLS_HOST has no A record at ${NS[0]}. Create a DNS-only (grey cloud) A record → $PUBLIC_IP and re-run."
        match=0
        for d in "${DNS_IPS[@]}"; do for m in "${MY_IPS[@]}"; do [ "$d" = "$m" ] && match=1; done; done
        [ "$match" -eq 1 ] || die "$TLS_HOST resolves to ${DNS_IPS[*]}, not this host (${MY_IPS[*]}). The feed host must be DNS-only — Cloudflare's proxy cannot carry TCP $FEED_PORT."
        log "DNS ok: $TLS_HOST -> ${DNS_IPS[*]} (direct)"

        install -d -m 0755 "$ACME_ROOT/.well-known/acme-challenge"
        if [ ! -f "$PUBLIC_CONF" ] && [ ! -f "$ACME_80_CONF" ]; then
            cat > "$ACME_80_CONF" <<EOF
# GENERATED by deploy/enable-feed-port.sh — ACME HTTP-01 responder only.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    location ^~ /.well-known/acme-challenge/ { root $ACME_ROOT; }
    location / { return 404; }
}
EOF
            nginx -t >/dev/null 2>&1 || die "nginx -t failed on $ACME_80_CONF"
        fi
        systemctl enable --now nginx >/dev/null 2>&1 || true
        systemctl reload nginx
        if ufw_active; then
            # Let's Encrypt validates from many addresses: 80 must be open to
            # all, and stay open for renewals (port 80 serves ACME + 301 only).
            ufw allow 80/tcp >/dev/null
            HTTP01_ANY=1
            log "ufw: 80/tcp open from anywhere (HTTP-01 validation + renewals for $TLS_HOST)"
        fi
        probe="retina-probe-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        echo "$probe" > "$ACME_ROOT/.well-known/acme-challenge/$probe"
        got="$(curl -sS -m 15 "http://$TLS_HOST/.well-known/acme-challenge/$probe" || true)"
        rm -f "$ACME_ROOT/.well-known/acme-challenge/$probe"
        [ "$got" = "$probe" ] || die "http://$TLS_HOST/.well-known/acme-challenge/ does not reach this host (got: '${got:0:80}')"
        certbot certonly --webroot -w "$ACME_ROOT" -d "$TLS_HOST" \
            --non-interactive --agree-tos "${EMAIL_ARGS[@]}" \
            || die "certbot failed for $TLS_HOST"
        [ -s "$CERT_DIR/fullchain.pem" ] || die "certificate missing at $CERT_DIR after certbot"
    fi
    install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
    if [ ! -f /etc/letsencrypt/renewal-hooks/deploy/retina-nginx-reload.sh ]; then
        printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/retina-nginx-reload.sh
        chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/retina-nginx-reload.sh
    fi
elif [ -f "$FEED_ENV" ]; then
    # keep the earlier run's firewall decision visible to enable-public-front.sh
    grep -q '^FEED_HTTP01_ANY=1' "$FEED_ENV" && HTTP01_ANY=1
fi

# --- 4. Render + install the stream front -----------------------------------
if [ -n "$TLS_HOST" ]; then
    sed -e "s/__BEAST_PORT__/$BEAST_PORT/g" -e "s/__TLS_PORT__/$TLS_PORT/g" \
        -e "s/__TLS_UPSTREAM__/127.0.0.1:$TLS_PORT/g" -e "s/__FEED_TLS_HOST__/$TLS_HOST/g" "$SRC" > "$DST"
else
    # plain-only: a ClientHello is sent to a closed loopback port (immediate
    # reset) instead of into readsb as garbage; the TLS server block is dropped.
    sed -e "s/__BEAST_PORT__/$BEAST_PORT/g" -e "s|__TLS_UPSTREAM__|127.0.0.1:1|g" "$SRC" \
        | awk '/^# --tls-begin/{skip=1} /^# --tls-end/{skip=0; next} !skip{print}' > "$DST"
fi
nginx -t || die "nginx -t failed on $DST"
systemctl enable --now nginx >/dev/null 2>&1 || true
systemctl reload nginx
log "feed front installed: $DST ($([ -n "$TLS_HOST" ] && echo "plain + TLS as $TLS_HOST" || echo "plain Beast only"))"

# --- 5. Firewall + state file -----------------------------------------------
if command -v ufw >/dev/null; then
    ufw allow "$FEED_PORT/tcp" >/dev/null
    ufw_active && log "ufw: $FEED_PORT/tcp open from anywhere" \
        || log "ufw rule for $FEED_PORT/tcp added (ufw is not active — nothing is filtered on this host)"
else
    log "WARNING: ufw not installed — open TCP $FEED_PORT in your firewall yourself"
fi
{
    echo "# GENERATED by deploy/enable-feed-port.sh on $(date -u +%FT%TZ); read by enable-public-front.sh"
    echo "FEED_TLS_HOST=$TLS_HOST"
    echo "FEED_HTTP01_ANY=$HTTP01_ANY"
} > "$FEED_ENV"
chmod 0644 "$FEED_ENV"

# --- 6. Verify through the public address -----------------------------------
# A Beast heartbeat frame (0x1A '1' + 9 zero bytes, vendor/readsb/net_io.c)
# is not a message: it plants nothing in the served picture, but the
# connection is real, so readsb (--net-ingest) logs "disc: ... TCP4 <client>"
# with the address from the PROXY header when we hang up.
since="$(date -u '+%Y-%m-%d %H:%M:%S')"
"$VENV/bin/python" - "$PUBLIC_IP" "$FEED_PORT" "$TLS_HOST" <<'EOF' || die "feed probe failed"
import socket, ssl, sys, time
ip, port, tls_host = sys.argv[1], int(sys.argv[2]), sys.argv[3]
HB = b"\x1a1" + b"\x00" * 9
def probe(wrap):
    s = socket.create_connection((ip, port), timeout=10)
    if wrap:
        s = ssl.create_default_context().wrap_socket(s, server_hostname=tls_host)
    s.sendall(HB); time.sleep(1.5); s.sendall(HB)
    s.settimeout(0.5)
    try:
        data = s.recv(64)          # readsb sends nothing on beast_in; b'' = closed on us
        if data == b"":
            raise SystemExit(f"{'TLS' if wrap else 'plain'}: connection closed by the server")
    except (TimeoutError, socket.timeout):
        pass
    s.close()
    print(f"enable-feed-port: {'TLS' if wrap else 'plain'} Beast connection to {ip}:{port} accepted")
probe(False)
if tls_host:
    probe(True)
EOF
sleep 1
want=1; [ -n "$TLS_HOST" ] && want=2
seen="$(journalctl -u readsb-hub --since "$since" --no-pager -o cat 2>/dev/null | grep -c "disc:.*TCP[46] $PUBLIC_IP " || true)"
[ "${seen:-0}" -ge "$want" ] || die "readsb logged $seen feeder disconnect(s) carrying the real address $PUBLIC_IP, expected $want — the PROXY header path is broken (journalctl -u readsb-hub)"
log "verified: readsb saw $seen connection(s) from $PUBLIC_IP through the PROXY header"

cat <<EOF

Beast contribution port live on $PUBLIC_IP:$FEED_PORT (no account, no client certificate).
Feeders send ONLY their own receiver's data (never re-fed hub/central data — AI-04):

  plain:  readsb/dump1090-fa ... --net-connector=$PUBLIC_IP,$FEED_PORT,beast_out
          (or beast_reduce_out / beast_reduce_plus_out for lower bandwidth)
EOF
if [ -n "$TLS_HOST" ]; then
    cat <<EOF
  TLS:    stunnel client on the receiver, then --net-connector=127.0.0.1,$FEED_PORT,beast_out
            [retina-feed]
            client = yes
            accept = 127.0.0.1:$FEED_PORT
            connect = $TLS_HOST:$FEED_PORT
            verifyChain = yes
            CAfile = /etc/ssl/certs/ca-certificates.crt
            checkHost = $TLS_HOST
            sni = $TLS_HOST
EOF
    if ! dig +short A "$TLS_HOST" 2>/dev/null | grep -qxF "$PUBLIC_IP"; then
        echo "  NOTE: $TLS_HOST does not resolve to $PUBLIC_IP (proxied or unset) — TLS feeders must set"
        echo "        connect = $PUBLIC_IP:$FEED_PORT and keep checkHost/sni = $TLS_HOST, or add a DNS-only"
        echo "        A record (e.g. feed.<zone> → $PUBLIC_IP) and re-run with --tls-host feed.<zone>."
    fi
fi
echo "Who is feeding: /run/readsb/clients.json (10 s cadence) and 'journalctl -u readsb-hub' (disc: lines)."
