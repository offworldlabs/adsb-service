# ADR-0004: Interim adsb.lol v2 serving and the honest-age mechanism

## Status

Accepted (plan §0 decision; spec §31 ADR #10 — provider timestamp trust
policy / ADSBHub latency treatment).

## Context

The core makes the §11 gRPC proto the only contract (contract-authority,
core §4) and contains no interim-serving provision. But the retina-node
investigation (plan §0) found the node fleet already adsb.lol-native: every
consumer polls readsb/tar1090-shaped data at ~1 Hz, and the tar1090-node
proxy calls exactly `GET /v2/lat/{lat}/lon/{lon}/dist/{radius}`. The plan
therefore serves the adsb.lol v2 format as a documented temporary contract,
resolved through the core's own conflict rule (defect fixed in both docs in
one change — the spec amendment).

The binding constraint (plan §0 req 1): `seen_pos` is the node's **only**
freshness signal — blah2-api reconstructs observation time as wall clock −
`seen_pos` and hard-refuses |dt| > 5 s, with no staleness cutoff on the live
association path. Served `seen_pos` MUST include ADSBHub upstream/ingest
latency ("the §10.5 penalty in interim clothing") or aged aggregate data
silently degrades truth association.

## Decision

### 1. Interim serving surface

`GET /v2/lat/{lat}/lon/{lon}/dist/{radius}` (+ alias
`/v2/point/{lat}/{lon}/{radius}`) served by `services/serving_api/`, mapping
to readsb's re-api `?circle={lat},{lon},{radius}&jv2` with the response body
passed through **verbatim** — the envelope is built by readsb and is
byte-compatible with adsb.lol by construction. Contract details:
`docs/spec-amendment-interim-serving.md` (DRAFT pending spec-owner signoff).

**Sunset condition:** Phase B native contract adoption — the
`retina.adsb.v1` proto is committed to the repo (transferring contract
authority per core §4) and nodes migrate to it. The interim surface then
takes no new consumers and is retired. Until then it stays minimal (two
routes + health/metrics) to limit format lock-in (plan §4 risk).

### 2. Honest-age mechanism

readsb itself supplies upstream-inclusive ages: it parses the SBS "generated"
timestamp (fields 7/8, interpreted UTC, ms precision) into
`mm->sysTimestamp`, and **all** aircraft clocks (`a->seen`, `a->seen_pos`,
validity `updated`) derive from it, while JSON/api writers subtract from
wall clock at write time. Served `seen_pos` therefore = wall-clock-at-write −
upstream-generated-time, i.e. it honestly includes ADSBHub upstream + ingest
latency. **Verified live** (`docs/requirements/code-readsb.md`,
net_io.c:3135-3153, track.c:130,198,2124): MSG,3 lines timestamped 3.0 s in
the past yielded `seen_pos: 3.414` in both aircraft.json and the re-api.

**Clamp hazards** (net_io.c:3154-3161): readsb silently rewrites the
timestamp to arrival wall clock when the parsed value is > arrival + 1 s
(fast upstream clock — latency hidden) or < arrival − 20 min (very stale —
would look **fresh**); unparseable dates also become arrival time. Non-UTC
upstream timestamps would fall outside the window and degrade every line to
arrival stamping.

**Adapter guard** (the mitigation, DESIGN.md §4): the adapter parses fields
7/8 as UTC and rejects — counts, does not relay — lines whose generated
timestamp is more than `future_tolerance` (1 s, the §12.3 analogue) ahead of
adapter wall clock or older than `max_record_age` (60 s). This prevents the
">20 min stale clamps to fresh" failure outright. Unparseable timestamps are
relayed (readsb stamps arrival = conservative) but counted separately.

**Continuous validation:** per-line delay (adapter wall clock − generated
timestamp) feeds `retina_adsb_provider_record_delay_seconds` (histogram,
buckets 0.1–60 s). This is simultaneously the §33 note 5 validation of
provider timestamps (running always, not once), the Q7 latency evidence
(measured ADSBHub age vs the node 5 s budget, gating cutover), and the
AC-O-03 before-ingest staleness attribution.

## Alternatives considered

- **Fixed §10.5-style penalty injection** (post-process the envelope, add a
  configured `provider_age_penalty` to each `seen_pos`): rejected. It would
  forfeit the verbatim-envelope passthrough — the serving tier would have to
  parse, mutate, and re-serialize every response, creating a new
  compatibility surface where byte-compatibility currently holds by
  construction — and a fixed constant misstates the true per-line latency
  that the timestamp mechanism already carries exactly.
- **Receipt-based conservative time per spec §33 note 5** (strip/ignore
  provider timestamps; let readsb stamp arrival): rejected. It violates plan
  §0 req 1 — served `seen_pos` would exclude upstream latency entirely, and
  aged aggregate data would pass the node's 5 s refusal as fresh. §33 note
  5's intent ("until validated") is met instead by the adapter guard plus the
  always-on delay histogram; the raw parsed values remain diagnostics
  (metrics), never trusted blindly (guard bounds), per §6.1 S1.

## Consequences

- **Honesty degrades gracefully, and visibly.** When ADSBHub clocks
  misbehave — future-fast beyond 1 s (readsb clamp / adapter reject) or
  unparseable — timing degrades to receipt-based arrival stamping, the
  conservative direction bounded by the 1 s clamp window. Every such mode is
  visible: the delay histogram shifts/spikes, and
  `records_total{result=stale|future|unparseable_ts}` counts the rejects.
  Operators see the degradation in the freshness dashboard instead of nodes
  silently associating stale data.
- Served data still bypasses the canonical safeguards (§12.3 plausibility,
  §10.5 penalties, §14.2 precision flag, §14 TTLs) — acknowledged interim
  gap, recorded in the ledger and the amendment.
- The amendment must carry the `seen_pos` semantics, clamp caveats, and
  sunset clause verbatim; node cutover remains gated on Q7 + soak.
- Aircraft with positions rejected by the guard age out of readsb (~60 s)
  rather than being served with hidden staleness.
