# RETINA ADS-B Phase A — single-host deployment

Deployment artifacts for the Phase A data plane (DESIGN.md §8, ADR-0002/0003):

| File | Purpose |
|---|---|
| `install.sh` | idempotent single-host install (users, code, venv, configs, readsb build, units) |
| `build-readsb.sh` | reproducible pinned readsb build → `/usr/local/bin/readsb-retina` |
| `gen-readsb-args.py` | `config/readsb.yaml` → verified readsb argument vector (spec §18.3) |
| `verify-units.sh` | grep-asserts the mandated hardening keys in the unit files |
| `systemd/*.service` | hardened units: `adsbhub-adapter`, `readsb-hub`, `retina-serving-api` |
| `nginx/mtls.conf`, `nginx/private-net.conf` | the two Q5 boundary options — **neither enabled by default** |

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

1. Port-collision precondition (spec §19) for 31003/9101/30152/8080.
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

## Q5 boundary options (external exposure — default CLOSED)

The serving-api binds loopback only. To let RETINA nodes/operators reach
`GET /v2/...` from off-host, an operator must enable exactly one of the two
documented nginx fronts (spec AC-S-01 forbids unauthenticated public
exposure; the choice between them is open question Q5, pending
stakeholders):

- **`nginx/mtls.conf` (recommended):** TLS on 443 with mandatory client
  certificates from the RETINA CA; per-client identity, works over any
  network. Requires the CA/enrollment story (ADR #4).
- **`nginx/private-net.conf`:** bind a private interface (VPC/VPN/WireGuard)
  plus an `allow/deny` CIDR allowlist; no client identity — acceptable only
  where private-network membership is itself controlled.

Both files are fully commented and inert until copied into the local nginx
config and the operator opens the corresponding firewall port. Rate
limiting is enforced in-app either way (2 req/s, burst 10 per client,
sized for ~1 Hz node polling).

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
