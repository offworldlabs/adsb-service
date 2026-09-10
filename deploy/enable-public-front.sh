#!/usr/bin/env bash
# RETINA ADS-B Phase A — enable the PUBLIC TLS front (Q5 option 3, ADR-0005).
#
#   sudo deploy/enable-public-front.sh <hostname> [--proxied] [--email <acme-email>]
#
# --proxied  the DNS record is Cloudflare-proxied (orange cloud) — the
#            deployed mode for adsb.retina.fm. Cloudflare terminates the
#            public TLS session and absorbs DDoS; this origin then trusts
#            CF-Connecting-IP from Cloudflare's published ranges only, and
#            the firewall admits 80/443 from those ranges only. Without the
#            flag the record must point straight at this host (DNS-only).
#
# Idempotent. Each run:
#   1. Ensures nginx + certbot are installed; removes nginx's default site.
#   2. Adds 127.0.0.1 to rate_limit.trusted_proxies in
#      /etc/retina-adsb/serving.yaml (if absent) so the app's per-client
#      limiter keys on the real client, and restarts retina-serving-api.
#   3. --proxied: refreshes Cloudflare's IP ranges (fallback: the vendored
#      deploy/nginx/cloudflare-ips.txt) and writes
#      /etc/nginx/conf.d/retina-cloudflare-realip.conf. Otherwise removes it.
#   4. Installs deploy/nginx/public.conf (hostname substituted) as
#      /etc/nginx/conf.d/retina-adsb-public.conf — first with only the
#      port-80 ACME block if no certificate exists yet.
#   5. Proves the name routes HERE: direct mode — the authoritative A record
#      must be one of this host's addresses; proxied mode — the authoritative
#      A records must all lie in Cloudflare's ranges. Both modes then fetch a
#      one-time probe file from the ACME webroot through the public name.
#      (Authoritative servers are asked directly: the system resolver may
#      still hold a cached "no such name" — Cloudflare's negative TTL is
#      30 min — and Let's Encrypt's resolvers do not share that cache.)
#   6. Obtains (or renews) the Let's Encrypt certificate via webroot HTTP-01
#      (Cloudflare passes /.well-known/acme-challenge/ through unredirected).
#   7. Enables the full front, nginx -t, reload.
#   8. Host firewall (ufw): default deny incoming; 22 from anywhere; 80/443
#      from anywhere (direct) or from Cloudflare's ranges only (proxied).
#   9. Verifies through the public name: /health 200 (and a cf-ray header
#      when proxied), /metrics 403, /livez 404, plain HTTP → 301, /v2 200.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/deploy/nginx/public.conf"
CF_VENDORED="$REPO_ROOT/deploy/nginx/cloudflare-ips.txt"
DST="/etc/nginx/conf.d/retina-adsb-public.conf"
REALIP_CONF="/etc/nginx/conf.d/retina-cloudflare-realip.conf"
SERVING_YAML="/etc/retina-adsb/serving.yaml"
ACME_ROOT="/var/www/retina-acme"
VENV="/opt/retina-adsb/.venv"

die() { echo "enable-public-front: ERROR: $*" >&2; exit 1; }
log() { echo "enable-public-front: $*"; }

HOST="${1:-}"; [ -n "$HOST" ] || die "usage: $0 <hostname> [--proxied] [--email <acme-email>]"
shift
PROXIED=0
EMAIL_ARGS=(--register-unsafely-without-email)
while [ $# -gt 0 ]; do
    case "$1" in
        --proxied) PROXIED=1; shift ;;
        --email) EMAIL_ARGS=(--email "$2" --no-eff-email); shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[ "$(id -u)" -eq 0 ] || die "must run as root"
[ -f "$SRC" ] || die "missing $SRC"
[ -f "$SERVING_YAML" ] || die "$SERVING_YAML missing — run deploy/install.sh first"
for tool in curl dig python3; do command -v "$tool" >/dev/null || die "$tool is required"; done

# 1. Packages, default site off.
command -v nginx >/dev/null && command -v certbot >/dev/null \
    || DEBIAN_FRONTEND=noninteractive apt-get install -y -q nginx certbot
rm -f /etc/nginx/sites-enabled/default
install -d -m 0755 "$ACME_ROOT/.well-known/acme-challenge"

# 2. App-side: trust this proxy for X-Forwarded-For keying.
if ! grep -qE '^\s*trusted_proxies:' "$SERVING_YAML"; then
    grep -qE '^rate_limit:' "$SERVING_YAML" || die "$SERVING_YAML has no rate_limit section"
    sed -i '/^rate_limit:/a\  # Q5 option 3 (ADR-0005): the same-host nginx public front; the limiter\n  # keys on the address it appends to X-Forwarded-For instead of 127.0.0.1.\n  trusted_proxies: ["127.0.0.1"]' "$SERVING_YAML"
    # install.sh's ownership contract (root:<service user>, 0640) — an editor
    # that rewrites the file can drop the group and lock the service out.
    chown root:retina-api "$SERVING_YAML"; chmod 0640 "$SERVING_YAML"
    "$VENV/bin/retina-serving-api" --check-config --config "$SERVING_YAML" >/dev/null \
        || die "$SERVING_YAML failed --check-config after adding trusted_proxies"
    systemctl restart retina-serving-api
    log "trusted_proxies added to $SERVING_YAML; retina-serving-api restarted"
fi

# 3. Cloudflare ranges + realip trust (proxied mode only).
CF_RANGES=()
if [ "$PROXIED" -eq 1 ]; then
    tmp="$(mktemp)"
    if curl -sSf -m 20 https://www.cloudflare.com/ips-v4 > "$tmp" && echo >> "$tmp" \
       && curl -sSf -m 20 https://www.cloudflare.com/ips-v6 >> "$tmp"; then
        log "Cloudflare IP ranges refreshed from cloudflare.com"
    else
        cp "$CF_VENDORED" "$tmp"
        log "WARNING: could not fetch Cloudflare ranges; using vendored $CF_VENDORED"
    fi
    mapfile -t CF_RANGES < <(grep -vE '^\s*(#|$)' "$tmp" | tr -d '\r')
    rm -f "$tmp"
    [ "${#CF_RANGES[@]}" -ge 10 ] || die "Cloudflare range list looks wrong (${#CF_RANGES[@]} entries)"
    for r in "${CF_RANGES[@]}"; do
        python3 -c "import ipaddress,sys; ipaddress.ip_network(sys.argv[1])" "$r" \
            || die "not a CIDR in Cloudflare range list: $r"
    done
    {
        echo "# GENERATED by deploy/enable-public-front.sh --proxied on $(date -u +%FT%TZ) — do not edit."
        echo "# Trust CF-Connecting-IP only when the connection comes from Cloudflare;"
        echo "# \$remote_addr then becomes the real client for limit_req/limit_conn/XFF."
        for r in "${CF_RANGES[@]}"; do echo "set_real_ip_from $r;"; done
        echo "real_ip_header CF-Connecting-IP;"
    } > "$REALIP_CONF"
    log "wrote $REALIP_CONF (${#CF_RANGES[@]} ranges)"
else
    rm -f "$REALIP_CONF"
fi

# 4. Install the front, 443 block omitted until a certificate exists.
render() { sed "s/__SERVER_NAME__/$HOST/g" "$SRC"; }
CERT_DIR="/etc/letsencrypt/live/$HOST"
if [ ! -s "$CERT_DIR/fullchain.pem" ]; then
    render | awk '/^server \{/{n++} n==2{exit} {print}' > "$DST"   # port-80 block only
else
    render > "$DST"
fi
nginx -t >/dev/null 2>&1 || die "nginx -t failed on $DST"
systemctl enable --now nginx >/dev/null 2>&1 || true
systemctl reload nginx

# 5. Prove the name routes here.
mapfile -t MY_IPS < <(hostname -I | tr ' ' '\n' | grep -v '^$')
zone="${HOST#*.}"; NS=()
while [ -n "$zone" ] && [ "$zone" != "$HOST" ]; do
    mapfile -t NS < <(dig +short NS "$zone" 2>/dev/null | grep -v '^$')
    [ "${#NS[@]}" -gt 0 ] && break
    zone="${zone#*.}"
done
[ "${#NS[@]}" -gt 0 ] || die "cannot find the authoritative nameservers for $HOST"
mapfile -t DNS_IPS < <(dig +short A "$HOST" "@${NS[0]}" 2>/dev/null | grep -E '^[0-9.]+$' | sort -u)
[ "${#DNS_IPS[@]}" -gt 0 ] || die "$HOST has no A record at its authoritative server ${NS[0]} (zone $zone). Create it on the Cloudflare zone those nameservers actually publish, then re-run."
if [ "$PROXIED" -eq 1 ]; then
    python3 - "${DNS_IPS[@]}" -- "${CF_RANGES[@]}" <<'EOF' || die "$HOST resolves to ${DNS_IPS[*]}, not Cloudflare addresses — turn the proxy ON for the record (orange cloud), or drop --proxied."
import ipaddress, sys
args = sys.argv[1:]; sep = args.index("--")
ips, nets = args[:sep], [ipaddress.ip_network(n) for n in args[sep+1:]]
sys.exit(0 if all(any(ipaddress.ip_address(i) in n for n in nets) for i in ips) else 1)
EOF
    log "DNS ok: $HOST -> ${DNS_IPS[*]} (Cloudflare edge, proxied)"
else
    match=0
    for d in "${DNS_IPS[@]}"; do for m in "${MY_IPS[@]}"; do [ "$d" = "$m" ] && match=1; done; done
    [ "$match" -eq 1 ] || die "$HOST resolves to ${DNS_IPS[*]} but this host's addresses are ${MY_IPS[*]}. Fix the record (DNS-only) or use --proxied."
    log "DNS ok: $HOST -> ${DNS_IPS[*]} (direct)"
fi
# Routing probe through the public name (via Cloudflare when proxied).
probe="retina-probe-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
echo "$probe" > "$ACME_ROOT/.well-known/acme-challenge/$probe"
got="$(curl -sS -m 15 "http://$HOST/.well-known/acme-challenge/$probe" || true)"
rm -f "$ACME_ROOT/.well-known/acme-challenge/$probe"
[ "$got" = "$probe" ] || die "http://$HOST/.well-known/acme-challenge/ does not reach this host (got: '${got:0:80}'). Proxied: check the record is orange-cloud on the zone the nameservers publish and that port 80 is open here."
log "routing ok: $HOST reaches this host's ACME webroot"

# 6. Certificate.
if [ ! -s "$CERT_DIR/fullchain.pem" ]; then
    certbot certonly --webroot -w "$ACME_ROOT" -d "$HOST" \
        --non-interactive --agree-tos "${EMAIL_ARGS[@]}" \
        || die "certbot failed (is port 80 reachable from the Internet / through Cloudflare?)"
fi
[ -s "$CERT_DIR/fullchain.pem" ] || die "certificate missing at $CERT_DIR after certbot"
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/retina-nginx-reload.sh
chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/retina-nginx-reload.sh

# 7. Full front.
render > "$DST"
nginx -t || die "nginx -t failed on the full config"
systemctl reload nginx
log "public front live: $DST"

# 8. Host firewall: default-deny inbound; 22 anywhere; 80/443 per mode
#    (AC-S-01 scan discipline; the whole stack is otherwise loopback-only).
if command -v ufw >/dev/null; then
    ufw --force reset >/dev/null
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw allow 22/tcp >/dev/null
    if [ "$PROXIED" -eq 1 ]; then
        for r in "${CF_RANGES[@]}"; do
            ufw allow from "$r" to any port 80 proto tcp >/dev/null
            ufw allow from "$r" to any port 443 proto tcp >/dev/null
        done
        log "ufw enabled: deny incoming except 22 (any) and 80/443 from Cloudflare's ${#CF_RANGES[@]} ranges"
    else
        ufw allow 80/tcp >/dev/null
        ufw allow 443/tcp >/dev/null
        log "ufw enabled: deny incoming except 22/80/443"
    fi
    # Beast contribution port (ADR-0008, deploy/enable-feed-port.sh): the
    # reset above wiped its rules; put them back when the front is installed.
    if [ -f /etc/nginx/retina-stream.d/retina-adsb-feed.conf ]; then
        ufw allow 7444/tcp >/dev/null
        log "ufw: 7444/tcp kept open (Beast contribution port)"
        if [ "$PROXIED" -eq 1 ] && grep -qs '^FEED_HTTP01_ANY=1' /etc/retina-adsb/feed-port.env; then
            ufw allow 80/tcp >/dev/null
            log "ufw: 80/tcp open from anywhere (HTTP-01 renewals for the feed TLS host)"
        fi
    fi
    ufw --force enable >/dev/null
else
    log "WARNING: ufw not installed — verify the firewall by hand (spec AC-S-01)"
fi

# 9. Verify through the public name.
code() { curl -sS -o /dev/null -m 15 -w '%{http_code}' "$@" || echo "000"; }
h=$(code "https://$HOST/health");     [ "$h" = "200" ] || die "https://$HOST/health returned $h"
if [ "$PROXIED" -eq 1 ]; then
    curl -sSI -m 15 "https://$HOST/health" | grep -qi '^cf-ray:' || die "https://$HOST/health did not come back through Cloudflare (no cf-ray header)"
fi
m=$(code "https://$HOST/metrics");    [ "$m" = "403" ] || die "/metrics reachable externally ($m) — AC-S-01 violated"
u=$(code "https://$HOST/livez");      [ "$u" = "404" ] || die "/livez reachable externally ($u)"
r=$(code "http://$HOST/health");      [ "$r" = "301" ] || die "plain HTTP not redirected ($r)"
v=$(code "https://$HOST/v2/lat/0/lon/0/dist/1"); [ "$v" = "200" ] || die "/v2 query returned $v"
log "verified: /health 200$([ "$PROXIED" -eq 1 ] && echo ' via Cloudflare'), /metrics 403, /livez 404, http→https 301, /v2 200"
if [ "$PROXIED" -eq 1 ]; then
    cat <<EOF
Cloudflare zone settings to confirm for $HOST (dashboard, not automatable here):
  * SSL/TLS mode: Full (strict)  — the origin presents a valid Let's Encrypt cert.
  * Bot Fight Mode / managed challenges: OFF for this hostname (nodes are
    non-browser clients; a challenge page would replace the JSON).
  * Cache: a Cache Rule "Bypass cache" for $HOST/* (responses are live data).
  * Optional but recommended: an edge Rate Limiting rule on $HOST/v2/* around
    5 requests per 10 s per IP, so floods are dropped at the edge, not here.
EOF
fi
echo "Nodes: set their adsb.lol base URL to https://$HOST (endpoint GET /v2/lat/{lat}/lon/{lon}/dist/{radius_nmi})."
