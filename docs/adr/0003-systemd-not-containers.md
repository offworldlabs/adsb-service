# ADR-0003: systemd hardened services, not containers

## Status

Accepted (spec §31 ADR #3; plan §0 single-host decision).

## Context

Phase A runs on one host with no Docker/Kubernetes. adsblol/infra — the
topology source — is a Kubernetes layout. Spec §20.1 isolation requirements
are written container-first but explicitly allow "containers or **hardened
system services**"; several acceptance/CI items are container-worded and need
a documented translation or acceptance review reads as failing
(`docs/requirements/spec-serving-ops.md` risks): §26.8 container scan, CI
gate 6 "container image immutable, signed, tied to a source commit",
AC-S-05 "images have SBOMs".

## Decision

Three systemd units, three system users, per DESIGN.md §8.

Unit hardening inventory (every unit):

| Setting | Value |
|---|---|
| User | `retina-adapter` / `readsb` / `retina-api` (separate system users) |
| NoNewPrivileges | yes |
| ProtectSystem | strict |
| ProtectHome | yes |
| PrivateTmp | yes |
| RestrictAddressFamilies | AF_INET AF_INET6 AF_UNIX |
| MemoryMax | adapter 256M, readsb 1G, api 256M |
| Restart | always, rate-limited |
| Network binds | loopback only — no unit binds a non-loopback address |

readsb unit additionally: `RuntimeDirectory=readsb` → `/run/readsb` tmpfs
(the k8s emptyDir equivalent; JSON/state files never touch disk, ages out on
restart — matches spec §24 disk-full row and §17's memory-only intent).

CI "container image" gate reinterpretation (§26.8 / CI gate 6 / AC-S-05):

- image immutability + signed + source-commit tie → **pinned commits**
  (readsb `d9a4c62`, adsblol/api `3c969c8`, adsblol/infra `b4712ea`) built by
  checked-in scripts (`deploy/build-readsb.sh`) into the §20.5 versioned
  layout (`/usr/lib/retina-adsb/<version>/` + `current` symlink), rollback
  keeps previous build (§20.6);
- image SBOM/vulnerability scan → `docs/licenses.md` SBOM record + host
  package updates via the OS mechanism;
- least privilege / no node private keys → holds directly via the unit
  inventory above (no node keys exist server-side in Phase A).

What is NOT reproduced from k8s, and why acceptable:

- **Per-pod network namespaces.** All components share the host loopback;
  any local user can connect to 127.0.0.1 ports (adapter relay 31003, re-api
  30152, serving 8080, metrics 9101). Accepted: single-tenant host, no
  untrusted local users, zero Internet-exposed ports by default (AC-S-01
  default-closed), host firewall default-deny inbound. Spec §20.1's own
  fallback applies: "private addresses + firewall policy rather than
  publishing ports". Revisit if the host ever becomes multi-tenant.
- **NetworkPolicy objects** → host firewall; external port-scan check in the
  A3 runbook verifies the effective posture (AC-S-01).
- **Liveness/readiness probes** → `Restart=always` + `/healthz`
  (adapter), `/health` (serving), re-api `?status` (readsb), consumed by
  alerting rather than a restart controller; systemd restarts on exit, alerts
  page on unhealthy-but-alive.
- **Horizontal scaling / replicas** → N/A single host; the logical
  ingest → hub → serving boundaries stay separable per §20.1/§20.2 (separate
  processes, users, and config), so re-splitting onto multiple hosts later is
  a deployment change, not a redesign.
- **jemalloc LD_PRELOAD** from the adsb.lol readsb image: not reproduced;
  glibc malloc + `MemoryMax=1G` bounds are sufficient at Phase A volumes.

## Consequences

- Acceptance review reads §20.1 / AC-S-05 / CI gate 6 through this
  translation (ledger rows AC-S-05, deploy-isolation).
- Loopback isolation between local users is weaker than netns — recorded
  residual risk.
- `install.sh` creates users/units/venv, runs `--check-config`, and opens no
  firewall ports; the nginx fronts (ADR-0005) are operator-enabled only.
