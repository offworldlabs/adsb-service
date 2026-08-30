# License review & SBOM record (spec §4.1 gate)

> Core compliance-gate requirement: "SBOM and license/redistribution review
> before production rollout; ADSBHub terms rechecked before launch and at
> least annually; the designated ADSBHub contribution station documented."
> This file is that record. Reviewed 2026-08-29 against the working tree.

## Our code

`retina-adsb` 0.1.0 — **MIT** (`/LICENSE`, © 2026 Offworld Labs). Packages
`adsbhub_adapter`, `serving_api` under `services/`.

## Vendored code (provenance detail: `vendor/VENDOR.md`)

| Component | Commit | License | Compatibility |
|---|---|---|---|
| adsblol/api | `3c969c8` | BSD-3-Clause (LICENSE file). **Contradiction:** its pyproject classifier claims MIT — the LICENSE file governs; recorded here, report upstream. | Compatible with MIT redistribution (attribution + no-endorsement clauses); derived code in `services/serving_api/` retains BSD-3 attribution obligations. |
| adsblol/infra | `b4712ea` | BSD-3-Clause | Compatible; used as configuration reference only. |
| wiedehopf/readsb | `d9a4c62` (v3.16.16) | GPL-3.0 | See boundary analysis below. |

## GPL-3 boundary analysis (readsb)

- readsb runs as a **separate OS process** (`readsb-hub`, own systemd unit,
  own user). All interaction is at arm's length: SBS over a loopback TCP
  socket, HTTP against its re-api, and files it writes to `/run/readsb`.
- No RETINA code links against readsb (no shared objects, no headers, no
  FFI); no readsb code is copied into MIT-licensed sources. Communication
  over sockets/files between separate programs does not create a combined/
  derivative work, so the GPL does not extend to the MIT repo.
- Obligations we do carry: if we distribute the readsb **binary**, we must
  provide corresponding source under GPL-3 — satisfied by vendoring the
  complete pinned source with `COPYING` intact (`vendor/readsb/`). The
  Phase A single-host deployment does not distribute binaries externally,
  but the source stays vendored regardless.
- Conclusion: **process boundary holds; no derivative work; repo remains
  MIT.**

## Python runtime dependencies (verified via `.venv/bin/pip show`, 2026-08-29)

Declared deps (pyproject):

| Package | Version | License (as installed) |
|---|---|---|
| fastapi | 0.141.1 | MIT |
| uvicorn | 0.52.4 | BSD-3-Clause |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT |
| prometheus_client | 0.26.0 | Apache-2.0 AND BSD-2-Clause |
| PyYAML | 6.0.3 | MIT |

(aiohttp and prometheus_client carry compound expressions in their metadata —
slightly broader than the single-license shorthand often quoted; both are
permissive-only in all parts.)

Transitive runtime set (full venv snapshot; dev-only tools marked, they do
not ship): aiohappyeyeballs 2.7.1 PSF-2.0; aiosignal 1.4.0 Apache-2.0;
annotated-doc MIT; annotated-types MIT; anyio MIT; attrs MIT; certifi
MPL-2.0 (unmodified use — fine); click BSD-3; frozenlist Apache-2.0; h11
MIT; httptools MIT; idna BSD-3; multidict Apache-2.0; propcache Apache-2.0;
pydantic + pydantic_core MIT; python-dotenv BSD-3; starlette BSD-3;
typing-inspection MIT; typing_extensions PSF-2.0; uvloop MIT; watchfiles
MIT; websockets BSD-3; yarl Apache-2.0. Dev-only: pytest MIT,
pytest-asyncio Apache-2.0, hypothesis MPL-2.0, httpx BSD-3, httpcore BSD-3,
aioresponses MIT, orjson MPL-2.0/Apache/MIT, Pygments BSD-2, sortedcontainers
Apache-2.0, iniconfig MIT, pluggy MIT, packaging Apache/BSD.

**Verdict: no copyleft reaches shipped code.** Strongest licenses present are
MPL-2.0 (file-level, unmodified: certifi at runtime, hypothesis/orjson
dev-only) and GPL-3 behind the readsb process boundary. Regenerate this
snapshot (`pip show` / freeze) as part of any dependency change and before
production rollout.

## ADSBHub terms & entitlement

- Feed: `data.adsbhub.org:5002`; the TCP port opens **only for IP addresses
  configured on the ADSBHub profile page** (confirmed 2026-08-26 against
  adsbhub.org/howtogetdata.php); data may be processed/published/used
  commercially; accuracy disclaimed — treated as untrusted sensor data
  (core compliance-gate).
- **Terms recheck: annual** (plan §4 / core compliance-gate). Last check
  2026-08-26; next due **before 2027-08-26** and before launch if earlier.
- **Designated contribution station (AI-04 record):** the receiver at static
  IP **47.46.206.146**, feeding ADSBHub directly from its own decoder —
  never hub-aggregate or central-derived data. A0 must confirm it is
  actively feeding before ingest goes live.
- **Data-access IP:** the droplet's static IP is whitelisted **separately**
  on the profile page (the data-access IP need not be the feeding station's
  IP). It is the "stable egress IP" of spec §6.1 S1 — use a reserved/
  floating IP, or update the whitelist before any rebuild/move (wrong egress
  IP presents as connection-refused, indistinguishable from provider-down —
  runbook entry in `runbooks/adsbhub-disconnected.md`).

## Open items on this gate

- §21.5 data-governance policy (retention/access/disclosure for ADS-B data,
  including the diagnostic/fixture SBS captures) is a MUST with no owning
  task yet — flagged in the conformance ledger; must exist before
  production rollout.
- §4.1 SHOULD: coverage comparison against at least one alternative
  aggregator recorded before launch — feeds the A3 independent-view check.
