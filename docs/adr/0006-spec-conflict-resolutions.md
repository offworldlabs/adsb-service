# ADR-0006: Recorded spec conflict/ambiguity resolutions

## Status

Accepted.

## Context

The core's governance rule (core preamble, README): on conflict the core
governs, and "the conflict is a defect to fix in both documents in one
change." The reader extraction surfaced five places where the spec conflicts
with itself or leaves a load-bearing value unspecified (items 1–5); the
adversarial review pass added two more (items 6–7), and a post-implementation
readsb-parity review added one (item 8). Each resolution below
is implemented per DESIGN.md and recorded here; the "Amend" line names the
sections to fix — in both `RETINA_ADSB_NORMATIVE_CORE.md` and
`RETINA_ADSB_IMPLEMENTATION_SPEC.md`, in one change, version lockstep.

## Decisions

### 1. Reconnect initial delay: §18.2 `1s` vs §24 `250 ms`

- **Conflict:** spec §24's default backoff progression starts at 250 ms
  ("250 ms, 500 ms, 1 s, 2 s, 4 s … cap 60 s"); the §18.2 server config
  example sets `adsbhub.reconnect.initial: 1s`. Both cap at 60 s.
- **Choice:** the initial delay is configurable (`reconnect.initial`),
  **default 1 s**; doubling, cap 60 s, full jitter, reset after 60 s stable —
  all per §24. Rationale: §18.2 is the ADSBHub-specific value and a 1 s floor
  is kinder to the single whitelisted upstream; the §24 progression remains
  the shape, entered at the configured initial.
- **Amend:** spec §24 (state the progression starts at the configured
  per-provider initial, ADSBHub default 1 s) and §18.2 (mark `initial` as the
  normative default, not just an example); core §1 AI-08 note referencing the
  configurable initial.

### 2. `idle_timeout: 30s` semantics (unspecified in §18.2)

- **Conflict/gap:** §18.2 lists `idle_timeout: 30s` for the upstream
  connection with no defined action on expiry.
- **Choice:** 30 s without bytes from ADSBHub → treat the session dead:
  close, close the relay, reconnect through backoff ("kill + reconnect").
  TCP keepalive is additionally enabled. Aligned with §14.4 row 1
  (unhealthy at 30 s without a valid record) and the §22.5 ADSBHubDown alert
  so the connection state machine and health thresholds agree.
- **Amend:** spec §18.2 (define the key's action) with a cross-reference to
  §14.4 row 1; core §15 incorporation note.

### 3. Parse-error-surge alert threshold (unspecified)

- **Conflict/gap:** §22.5 says only "over threshold", §24 "trip degraded
  threshold"; no number anywhere.
- **Choice:** alert when `invalid/total ratio > 5%` over 5 minutes **and**
  total records > 100 in the window (minimum-volume guard so a quiet feed
  cannot page on 1-of-3 lines). Implemented in
  `observability/prometheus/` rules from
  `retina_adsb_provider_records_total{result}`.
- **Amend:** spec §22.5 alert table (add ParseErrorSurge with these
  defaults, configurable); core §15 alerts row.

### 4. §12.4 "explicitly tested BaseStation variants" (unenumerated)

- **Conflict/gap:** §12.4 permits parsing only explicitly tested variants but
  never lists them; §26.2 requires a fixture per supported subtype.
- **Choice:** the supported set is **MSG transmission types 1–8** — what
  ADSBHub emits, and what pinned readsb parses (uniformly: the type field is
  never branched on, net_io.c:3106-3109). One golden fixture per subtype in
  `test/fixtures/sbs/`. Non-`MSG` lines (SEL/ID/AIR/STA/CLK) and any other
  type value are counted and dropped, never relayed.
- **Amend:** spec §12.4 (enumerate MSG 1–8 as the v1 tested set) and §26.2
  (fixture-per-subtype reference); core sbs-parsing requirement note.

### 5. Provider-timestamp sanity bounds (unenumerated in §12.4)

- **Conflict/gap:** §12.4 requires provider timestamps "never trusted without
  sanity checks" but doesn't define the bounds; nearest analogues are §12.3
  `future_tolerance_ms` 1,000 and `position_hard_reject_ms` 30,000 — but
  §12.3 is a state-layer function deferred to Phase B, and readsb's own clamp
  window is +1 s / −20 min.
- **Choice:** adapter guard bounds: **future > 1 s** → reject (matches §12.3
  `future_tolerance_ms` and readsb's clamp, so nothing we relay gets silently
  re-stamped for being fast); **stale > 60 s** → reject (matches readsb's
  60 s aircraft-expiry horizon and the eviction guarantee in the amendment;
  it exists to defeat the ">20 min stale clamps to fresh-looking arrival
  time" hazard, ADR-0004). Unparseable → relay + count (readsb stamps
  arrival, conservative). Note: lines aged 30–60 s pass the guard although
  §12.3 would hard-reject them at 30 s — acceptable because §12.3 enforcement
  is deferred with the state layer and such lines serve with honest
  `seen_pos` ≥ 30 s, far beyond the node's 5 s refusal.
- **Amend:** spec §12.4 (enumerate the sanity bounds and their rationale,
  configurable as `future_tolerance` / `max_record_age`); core sbs-parsing
  and sbs-timestamp-caution notes.

### 6. §12.2 longitude "+180 → −180" normalization vs the verbatim relay

- **Conflict:** §12.2's longitude clause says +180 normalizes to −180. The
  Phase A adapter is by design a verbatim relay (units-no-silent-conversion:
  it validates and forwards SBS lines, never rewrites fields), and readsb
  serves longitude exactly as received — so a line carrying +180.0 passes
  the ±180-inclusive range check and is served as +180. No Phase A component
  is allowed to perform the rewrite.
- **Choice:** Phase A **knowingly serves +180 as-is**; the normalization
  clause is a canonical-layer duty and lands with the Phase B canonical
  contract, where field rewriting is legitimate. Consumers see, at worst,
  the single degenerate antimeridian value — both spellings denote the same
  meridian.
- **Amend:** spec §12.2 (state that the +180→−180 rewrite belongs to the
  canonical layer, not to validating relays); core range-checks note.

### 7. Track domain widened; altitude bounds made configurable

- **Conflict/gap:** §12.2 gives no explicit domain for track; the first
  implementation rejected anything outside [0, 360). Real provider encodings
  exceed that (negative and >360 values appear), and pinned readsb itself
  accepts **any finite** heading value — so the narrow adapter check dropped
  lines readsb would have handled.
- **Choice:** the adapter accepts any **finite** track with **|t| ≤ 3600**
  (garbage magnitudes still rejected); normalization into [0, 360) is
  deferred to the Phase B canonical layer (relaying is lossless since readsb
  accepts any finite heading). Altitude bounds are configurable —
  `adsbhub.altitude_min_ft` (default **−2000**) / `adsbhub.altitude_max_ft`
  (default **100000**) — keeping the §12.2 defaults while letting a
  deployment tighten or widen per provider.
- **Amend:** spec §12.2 (enumerate the track acceptance domain and the
  normalization deferral; mark the altitude bounds configurable with these
  defaults); core range-checks note.

### 8. §18.2 `timestamp_timezone` key removed (readsb hardcodes UTC)

- **Conflict:** spec §18.2/§12.4 define a configurable `timestamp_timezone`
  (default UTC) for parsing SBS generated timestamps. Pinned readsb hardcodes
  UTC — it converts the parsed `struct tm` with `timegm`
  (vendor/readsb/net_io.c:3153) — and the Phase A adapter relays lines
  verbatim, so under any non-UTC setting the validator would read the same
  bytes differently than readsb, the canonical consumer: the ADR-0004
  stale/future guard would pass or reject lines against a timestamp readsb
  never computes, defeating the parity the guard exists to provide.
- **Choice:** the knob is **removed entirely**. The validator hardcodes UTC
  (`timegm`-equivalent arithmetic, matching net_io.c:3135-3161 exactly,
  including the literal-milliseconds `strtol` fraction handling), and the
  strict loader rejects `timestamp_timezone` as an unknown key so a config
  carrying it fails loudly instead of being silently ignored.
- **Amend:** spec §18.2 (drop the key from the adsbhub block) and §12.4
  (state that provider timestamps are parsed as UTC, fixed by readsb parity,
  not configured); core exact-constants list (`timestamp_timezone UTC`
  entry).

## Consequences

- All recorded resolutions are implemented in DESIGN.md (§4, §9) and
  testable; the conformance ledger cites this ADR for the affected rows.
- Until the amendment lands, this ADR is the record that the divergences are
  deliberate, per the README governance rule — not drift.
