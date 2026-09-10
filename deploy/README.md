# RETINA ADS-B Phase A — single-host deployment

Deployment artifacts for the Phase A data plane (DESIGN.md §8, ADR-0002/0003):

| File | Purpose |
|---|---|
| `install.sh` | idempotent single-host install (users, code, venv, configs, readsb build, units) |
| `build-readsb.sh` | reproducible pinned readsb build → `/usr/local/bin/readsb-retina` |
| `gen-readsb-args.py` | `config/readsb.yaml` → verified readsb argument vector (spec §18.3) |
| `verify-units.sh` | grep-asserts the mandated hardening keys in the unit files |
| `systemd/*.service` | hardened units: `adsbhub-adapter`, `readsb-hub`, `retina-serving-api` |
| `nginx/public.conf` | the **chosen** Q5 boundary (ADR-0005): public TLS front, adsb.lol parity — enabled only by `enable-public-front.sh`, never by `install.sh` |
| `enable-public-front.sh` | idempotent: authoritative DNS + routing probe, Let's Encrypt certificate, installs `public.conf`, `--proxied` Cloudflare real-IP trust + ufw 80/443 from Cloudflare ranges only, verifies the front |
| `nginx/cloudflare-ips.txt` | vendored Cloudflare IP ranges — offline fallback for `--proxied`; the script refreshes from cloudflare.com on every run |
| `nginx/mtls.conf`, `nginx/private-net.conf` | the two alternative (authenticated) boundaries — shipped, not enabled |
| `nginx/feed.conf` | **contribution port** (ADR-0008): nginx `stream{}` front on TCP 7444 → readsb's loopback Beast listener; plain Beast or TLS (no client cert) auto-detected; PROXY v1 so readsb sees the real feeder — enabled only by `enable-feed-port.sh` |
| `enable-feed-port.sh` | idempotent: installs the stream module, checks readsb carries `--net-bi-port`/`--net-ingest`, optional certificate for a DNS-only feed hostname, installs `feed.conf`, `ufw allow 7444/tcp`, verifies plain + TLS end to end (readsb must log the real address); `--disable` closes it again |

## Prerequisites (plan.md A0 — do these first)

1. **ADSBHub IP whitelist.** ADSBHub opens `data.adsbhub.org:5002` **only for
   IP addresses configured on your ADSBHub profile page**. Whitelist this
   host's stable egress IP before first start; a missing or stale whitelist
   is indistinguishable from provider-down (connection refused/timeout).
   If the host is ever rebuilt or moved, update the whitelist *before*
   cutover — prefer a reserved/floating IP so it never changes (this IP is
   the "stable egress IP" of spec §6.1 S1).
2. **Designated contribution station.** The data entitlement rests on the
   receiver at **47.46.206.146** actively feeding ADSBHub from its own
   decoder (never hub/central data) — spec AI-04/§4.1.
3. Ubuntu 24.04 host with `python3` (3.12), `python3-venv`, `rsync`, `git`,
   and outbound access to PyPI for the one-time venv build. readsb build
   deps (`build-essential pkg-config libncurses-dev zlib1g-dev libzstd-dev`)
   are checked by `build-readsb.sh`; pass `--install-deps` to let it apt-get
   them.

## Install

```sh
sudo deploy/install.sh              # add --install-deps to fetch build packages
```

What it does (rerunnable; nothing is started, no firewall is touched):

1. Port-collision precondition (spec §19) for 31003/9101/30152/31004/8080.
2. Creates system users `retina-adapter`, `readsb`, `retina-api`.
3. Syncs code to `/opt/retina-adsb` (root-owned), builds the venv at
   `/opt/retina-adsb/.venv`, pip-installs the project into it.
4. Installs configs to `/etc/retina-adsb/*.yaml` (root-owned, mode 0640,
   group-readable by the owning service user). Existing edited configs are
   never clobbered — a changed example lands as `*.dist`.
5. Builds and pin-verifies readsb (commit `d9a4c62`, ADR-0002) and installs
   it as `/usr/local/bin/readsb-retina` (not `readsb`, to avoid colliding
   with a distro readsb).
6. Gates: `adsbhub-adapter --check-config`, `retina-serving-api
   --check-config`, and `gen-readsb-args.py` output verified against the
   frozen `ExecStart` in `systemd/readsb-hub.service` (spec §18.3: the
   generated args are the contract — regenerate, don't edit).
7. Installs the three units and runs `systemctl daemon-reload`. **Not
   enabled, not started.**

Then start in data-path order:

```sh
sudo systemctl enable --now adsbhub-adapter readsb-hub retina-serving-api
```

## Port map (DESIGN.md §2 — all loopback out of the box)

| What | Value | Why |
|---|---|---|
| ADSBHub upstream | `data.adsbhub.org:5002` (outbound; config, not compiled) | spec §6.1 S1 |
| Adapter relay listen | `127.0.0.1:31003` | spec §18.3 |
| Adapter metrics/health HTTP | `127.0.0.1:9101` (`/metrics`, `/healthz`) | interim surface (ledger) |
| readsb re-api | `127.0.0.1:30152` (path `/re-api/` cosmetic) | adsb.lol convention |
| readsb JSON dir | `/run/readsb` (`RuntimeDirectory=readsb`) | readsb default; tar1090 reads it |
| serving-api | `127.0.0.1:8080` (`/v2/...`, `/health`, `/metrics`) | new |
| readsb debug SBS inject | `127.0.0.1:32006` — **disabled by default** (`sbs_debug_port` commented out in `config/readsb.yaml`; bypasses adapter validation) | test/debug overlays only |
| readsb Beast contribution listener | `127.0.0.1:31004` (`--net-bi-port` + `--net-ingest`, key `beast_feed_port`) | spec §19 port; ADR-0008 |
| Public feed port | `0.0.0.0:7444` nginx stream → 31004 — **closed until `enable-feed-port.sh`** | ADR-0008 |

Exactly **zero public ports** exist after install (AC-S-01 by
default-closed). `install.sh` never opens firewall ports; the external
port-scan check belongs to the A3 soak (see below).

## Changing the readsb invocation

`systemd/readsb-hub.service` carries a frozen argument line generated from
`config/readsb.yaml`. Never hand-edit it: edit the YAML, run

```sh
deploy/gen-readsb-args.py --config config/readsb.yaml --systemd
```

and paste the emitted `ExecStart=` line into the unit. The generator refuses
(exit 2) configs that would set `--lat/--lon` (arms the 450 nmi range filter
→ silently drops far SBS positions) or `--json-separate-(alt-)ground`
(breaks the `alt_baro:"ground"` v2 envelope). `install.sh` re-verifies the
unit line on every run.

## Q5 boundary — public TLS front (external exposure; CLOSED until enabled)

The serving-api binds loopback only and `install.sh` opens nothing. Q5 was
decided 2026-09-05 (ADR-0005): the interim `GET /v2/...` surface is served
**publicly over HTTPS with no client authentication, exactly like
api.adsb.lol**, so nodes cut over by changing their base URL. Enable it with:

```sh
sudo deploy/enable-public-front.sh adsb.retina.fm --proxied --email ops@example.com
```

Prerequisite: an **A record `adsb.retina.fm → this host's public IP` on
the Cloudflare zone that the domain's nameservers actually publish**,
**proxied (orange cloud)** — Cloudflare fronts the origin for DDoS
absorption (ADR-0005). The script asks the authoritative nameservers
directly (the local resolver may cache "no such name" for 30 min), checks
the record points at Cloudflare, and proves routing by fetching a one-time
probe through the public name. It then installs nginx + certbot, refreshes
Cloudflare's IP ranges (fallback `nginx/cloudflare-ips.txt`) into a
`realip` trust file so every limiter sees the real node address from
`CF-Connecting-IP`, obtains and auto-renews the Let's Encrypt certificate,
installs `nginx/public.conf` (only `/v2/` and `/health` proxied; `/metrics`
loopback-only; everything else 404; nginx per-client `limit_req` in front
of the in-app 2 req/s burst 10 limiter, keyed on the real client because
`127.0.0.1` is in `rate_limit.trusted_proxies`), enables ufw with 22 open
and **80/443 admitted only from Cloudflare's ranges**, and verifies the
front end to end (including a `cf-ray` header). Re-run it after any change
to `public.conf` and periodically to refresh the Cloudflare ranges. Without
`--proxied` the same script supports a DNS-only record pointing straight at
the host. Nodes then use
`https://adsb.retina.fm/v2/lat/{lat}/lon/{lon}/dist/{nmi}`.

Cloudflare dashboard settings for the hostname (not automatable from here):
SSL/TLS **Full (strict)**; Bot Fight Mode / managed challenges **off** for
`adsb.retina.fm` (nodes are non-browser clients); a Cache Rule bypassing
cache for `adsb.retina.fm/*`; optionally an edge Rate Limiting rule on
`/v2/*` (~5 requests per 10 s per IP) so floods die at the edge.

The two authenticated alternatives stay shipped and are a front-only swap:

- **`nginx/mtls.conf`:** TLS on 443 with mandatory client certificates from
  the RETINA CA; per-client identity. Requires the CA/enrollment story
  (ADR #4) — the Phase B direction.
- **`nginx/private-net.conf`:** bind a private interface (VPC/VPN/WireGuard)
  plus an `allow/deny` CIDR allowlist; no client identity.

## Contribution port — account-less Beast ingest on TCP 7444 (ADR-0008)

Decided 2026-09-06: receivers feed the hub the way they feed adsb.lol — a
Beast connector, no account, no client certificate — at the spec's port
number. TLS is offered on the same port for feeders that want privacy in
transit (server certificate only). Enable it with:

```sh
sudo deploy/enable-feed-port.sh --tls-host adsb.retina.fm      # as deployed (reuses that certificate)
sudo deploy/enable-feed-port.sh                                # plain Beast only
sudo deploy/enable-feed-port.sh --tls-host feed.retina.fm --email ops@example.com   # own DNS-only feed name
sudo deploy/enable-feed-port.sh --disable
```

What it does: installs `libnginx-mod-stream` and a `stream { include
/etc/nginx/retina-stream.d/*.conf; }` block; refuses unless
`/etc/retina-adsb/readsb.yaml` sets `beast_feed_port` and the installed unit
carries the generated `--net-bi-port=31004 --net-ingest` pair (with the
raised `--devel=ingestLimit*` bounds) and readsb is listening; obtains a
certificate for `--tls-host` if none exists (the name must be **DNS-only**
and point straight here — Cloudflare's proxy does not carry raw TCP — and
Let's Encrypt validates from many addresses, so the script then opens
`80/tcp` from anywhere and keeps it open for renewals); renders
`nginx/feed.conf` (plain + TLS auto-detected by `ssl_preread`; PROXY v1 to
readsb; `limit_conn` 4 per address, 1 MB/s per connection, 90 s idle, 65 s
preread); `ufw allow 7444/tcp`; then proves the path by connecting through
the public address (plain and TLS) and requiring readsb's journal to show
the host's own address from the PROXY header. `enable-public-front.sh`
re-applies the 7444 (and, if needed, 80) rules after its ufw reset, reading
`/etc/retina-adsb/feed-port.env`.

Feeders (send **only your own receiver's data** — never re-fed central data):

```sh
readsb ... --net-connector=138.197.82.83,7444,beast_out        # plain (beast_reduce_out also fine)
```

TLS: an stunnel client with `connect = <origin>:7444`, `verifyChain = yes`,
`checkHost`/`sni` = the certificate name, then `--net-connector=127.0.0.1,7444,beast_out`.
As deployed the certificate is `adsb.retina.fm`, which resolves to
Cloudflare, so TLS feeders connect to the origin IP with `checkHost`/`sni`
set to that name — or add a DNS-only `feed.retina.fm` record and re-run
with `--tls-host feed.retina.fm`.

Who is feeding: `/run/readsb/clients.json` (every 10 s: source address,
receiver id, rates) and `journalctl -u readsb-hub` (`disc:` lines, garbage
closes). Bounds without identity: nginx per-address/per-connection limits
above; readsb per-feeder message/position limits, garbage disconnect after
512 undecodable bytes, `--json-reliable=2` (two consistent positions before
an aircraft is served). 7444 is **outside the Cloudflare shield** (feeders
reach the origin directly); a volumetric attack on it hits the droplet.

## Operator visualization (optional)

tar1090 (operator map UI over readsb's JSON output) is **NOT installed or
configured by `install.sh`** and is not shipped anywhere in this repo —
spec §6.1 S3 makes it a MAY, and Phase A defers it to the operator. To add
it, use wiedehopf's standard installer
(<https://github.com/wiedehopf/tar1090>):

```sh
sudo bash -c "$(curl -sSL https://github.com/wiedehopf/tar1090/raw/master/install.sh)" /run/readsb
```

pointing its json dir at `/run/readsb` (where `readsb-hub` writes
`aircraft.json`). Constraints if you do:

- It must stay **behind the Q5 boundary**: never publicly reachable, and if
  exposed off-host at all, only through the same authenticated nginx front
  as `/v2` (a new explicit `location`, same allow/deny or mTLS discipline).
  AC-S-01's zero-public-ports property must survive the install.
- It is an operator convenience only — **never a node/solver contract**.
  Nodes consume `GET /v2/...`; nothing may start depending on tar1090's
  paths or JSON shapes.

## A3 — 72-hour soak

Phase A exits through the 72-hour shadow soak (spec §26.7/§28: no unbounded
memory/FD growth, parse errors understood, freshness dashboards
operational, coverage compared against an independent view). The soak
checklist and the operational procedures live in `runbooks/` (see also
`observability/` for the Prometheus scrape config and alert rules, and
`dashboards/` for the pipeline + freshness Grafana boards). The external
port-scan verification (only approved endpoints visible) is part of that
checklist, not of `install.sh`.

## Upgrade / rollback notes

- readsb: pinned by ADR-0002. Any bump = new pin + golden-fixture replay
  gate (spec §12.5/§26.6) — see the readsb upgrade/rollback runbook.
- Python services: rerun `install.sh` from the updated checkout (it
  force-reinstalls the project into the venv), then restart units.
- Configs under `/etc/retina-adsb` are yours after first install; diff
  against the `*.dist` files the installer leaves when examples change.
