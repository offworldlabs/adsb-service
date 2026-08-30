# Vendored code provenance

> §4.1 SBOM/license gate record lives in `docs/licenses.md`; this file is the
> provenance + usage inventory for everything under `vendor/`.
>
> Mechanism: each tree is a **git submodule** pinned at the commit below
> (`.gitmodules`, shallow). After cloning this repo:
> `git submodule update --init --depth 1`.

| Path | Upstream | Pinned commit | License | Used for |
|---|---|---|---|---|
| `vendor/api/` | [adsblol/api](https://github.com/adsblol/api) | `3c969c84f6f659e1c83715a73cb6c2b6eb1d1d89` (2026-06-03, shallow) | **BSD-3-Clause** (`LICENSE`, © 2023 Katia Esposito). NOTE: `pyproject.toml` carries a `License :: OSI Approved :: MIT License` classifier that contradicts the LICENSE file — the LICENSE file governs; contradiction recorded for the SBOM and worth reporting upstream. | Reference/derivation source for `services/serving_api/`: the `reapi.py` client pattern, v2 route → re-api `circle` mapping, envelope models, aioresponses test approach. **Not executed** — the vendored app hard-requires Redis and phones adsb.lol infrastructure (see `docs/requirements/code-adsblol-api.md`). |
| `vendor/infra/` | [adsblol/infra](https://github.com/adsblol/infra) | `b4712ea731855fc0ba71755eddff623521795e42` (2026-06-03) | **BSD-3-Clause** (`LICENSE`, © 2023 Katia Esposito) | Configuration/topology reference only: readsb hub/reapi invocation patterns, port conventions, service layering translated to single-host systemd (DESIGN.md §1–§3, ADR-0003). No code executed. |
| `vendor/readsb/` | [wiedehopf/readsb](https://github.com/wiedehopf/readsb) | `d9a4c62655490e70d07704e207738bb9c6cffde1` (v3.16.16, 2026-08-17) | **GPL-3.0** (`COPYING`) | Built (`deploy/build-readsb.sh`, ADR-0002) and run as `readsb-hub` — a **separate OS process** communicating over sockets and files only. No linking against MIT-licensed RETINA code; the repo's MIT license is unaffected (boundary analysis in `docs/licenses.md`). Pinned as a submodule to the public upstream; if a built binary is ever distributed, corresponding source for the exact pinned commit must accompany the offer (keep a source archive with any release artifact). |

## Update procedure

Vendored trees are pinned submodules, not tracking clones. To update one:

1. Fetch and check out the target upstream commit inside the submodule,
   then stage the new gitlink (`git add vendor/<name>`); record
   old → new commit here.
2. Review the diff for license changes and behavior changes on the surfaces
   we depend on (for api/infra: the files listed in
   `docs/requirements/code-adsblol-*.md`; for readsb: SBS parser, timestamp
   handling, re-api, json_out).
3. Re-run the license check and update `docs/licenses.md`.
4. **readsb only:** the spec §12.5 upgrade gate applies — golden fixtures
   must pass and a ≥30-minute shadow replay must show no incompatible
   mapping, parse-error increase, or freshness regression
   (`runbooks/readsb-upgrade-rollback.md`); update `docs/readsb-pin.md` and
   ADR-0002 (or supersede it) with the new pin.
5. api/infra are reference material: an update only matters if
   `services/serving_api/` re-derives from the new code — record that
   decision in the relevant ADR if so.
