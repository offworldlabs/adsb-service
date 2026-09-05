# ADR-0007: ICAO zero-padding in the ADSBHub adapter

## Status

Accepted.

## Context

ADSBHub emits the SBS ICAO field (field index 4) **without leading-zero
padding**: addresses below 0x100000 arrive with fewer than 6 hex chars, e.g.
`D0EEC` for Mexican 0D0EEC (Volaris, callsign VOI). Measured on the live feed
2026-09-05: in a 52,026-line raw capture, 1,287 lines and 157 of 6,292
distinct aircraft (≈2.5%) carried a 5-char ICAO; no 4-char ones appeared in
that sample, but addresses below 0x10000 (e.g. Zimbabwe 004000–0043FF) would
arrive as 4 chars. Over the same window the adapter counted
`records_total{result="invalid_structure"}` = 3,861 of 143,600 (2.7%), all
attributable to this.

Both ends of the Phase A data path rejected those lines:

- the adapter validator (`services/adsbhub_adapter/sbs.py`) required exactly
  6 hex chars, and
- pinned readsb (`vendor/readsb/net_io.c:3112`) requires
  `strlen(t[5]) >= 6 && strlen(t[5]) <= 7`.

So relaying them verbatim would not have helped either: the whole
000000–0FFFFF block (Mexico, Cuba, much of Africa and the Caribbean) was
invisible to the pipeline, silently, counted only as generic structural
rejects.

The conflict is with the Phase A relay contract: **verbatim relay, no field
rewriting** (DESIGN.md §4; conformance-ledger rows `units-no-silent-conversion`,
`range-checks`; ADR-0006 §§6–7, which deferred the §12.2 longitude and track
normalizations to the Phase B canonical layer precisely because a validating
relay may not rewrite fields).

## Decision

**The adapter left-zero-pads the ICAO field to 6 hex chars before relay, and
this is the sole, explicitly enumerated exception to the verbatim-relay rule.**

The exact rule:

- The ICAO field is accepted as an optional leading `~` followed by **1–6 hex
  chars** (either case). Zero hex chars, more than 6, or any non-hex character
  remain `INVALID_STRUCTURE` exactly as before. A 7-hex-char body stays
  rejected even though readsb's `strlen` window (6–7) admits it: readsb reads
  exactly 6 hex digits after an optional `~` (net_io.c:3113-3125) and silently
  ignores the 7th, i.e. it would key the aircraft on an address that is not
  the one on the wire. The 7-char allowance exists for the `~` prefix form,
  which we still accept.
- When the hex body is shorter than 6, the relayed line carries it left-padded
  with ASCII `0` to 6 chars, `~` preserved in front, the existing hex chars'
  **case unchanged**. Nothing else in the line changes: the relayed bytes are
  byte-identical to the input except for the inserted zeros, and the original
  `\r\n` / `\n` terminator is forwarded exactly as received.
- **Length re-check:** padding happens before readsb's 20–199 byte acceptance
  window (net_io.c:3046-3049) is re-evaluated. A 199-byte line with a 5-char
  ICAO becomes 200 bytes once padded, which readsb would drop, so it is
  rejected here as `INVALID_STRUCTURE` and counted — never relayed as bytes
  the consumer discards.
- **Idempotence:** the relayed bytes are a fixed point. Re-validating
  `relay_line` yields the same result, `relay=True`, the same bytes, the same
  generated timestamp and delay, and `normalized=False`. One pass, then
  nothing changes — so the validator and readsb can never judge the relayed
  line differently (the same parity argument as ADR-0006 §8 and ADR-0004).
- `ValidationResult` gained `relay_line: bytes | None` (the exact bytes to
  forward, without terminator; `None` whenever `relay` is False) and
  `normalized: bool`. The relay forwards `relay_line`, not the raw input.

**Metric.** `retina_adsb_provider_records_normalized_total{provider,
normalization}` (counter), incremented with `normalization="icao_zero_pad"`.
The label is the fixed rule name — never an aircraft key or any other
high-cardinality value (spec §21 metrics cardinality; §1642: keys never on the
default log path, so padded addresses are not logged either). The
`icao_zero_pad` series is pre-initialized to 0 at startup, like the
`records_total` result labels, so it exists immediately after a restart.
Padded lines still count under `records_total` with their ordinary result
(`ok`, or `unparseable_ts` etc.). The counter counts every record the padding
rule *applied to*, including the few that a later range or timestamp check
then rejects — so its ratio to `records_total` measures how much of the feed
uses the unpadded spelling, which is the signal the runbook watches, rather
than how much of it survived the rest of validation.

**Why this is legitimate where the ADR-0006 rewrites were not.** Padding is
lossless: `int(body, 16)` is unchanged, so no information is added, removed or
reinterpreted — the same address is spelled in the canonical 6-digit width the
consumer already uses. readsb itself re-keys every aircraft as `%06x`
(`json_out.c:626/845`), so the padded field yields *exactly* the `aircraft_key`
spec §9.1 demands; the unpadded spelling yields nothing at all, because the
line never reaches the parser. By contrast the §12.2 longitude (+180 → −180)
and track (mod 360) normalizations change a *value's representation as data*
and are legitimately canonical-layer duties.

## Consequences

- The verbatim-relay rule now has exactly one enumerated exception, named in
  DESIGN.md §4, in the conformance ledger rows it touches, and in code
  (`sbs.py`, `NORMALIZATION_LABELS`). Any future exception needs its own ADR
  and its own `normalization` label; the counter makes them countable.
- The 000000–0FFFFF address block becomes visible; `invalid_structure` drops
  by roughly the 2.7% measured above, and a sudden change in the ratio
  `records_normalized_total` / `records_total` is now a provider-format-change
  signal (see `runbooks/parse-error-surge.md`).
- Phase B inherits nothing new: the canonical layer already keys on `%06x`, so
  a padded field is what it would have produced anyway. This ADR does not
  license any other rewrite in Phase A.
- **Upgrade gate:** the readsb pin-bump checklist must re-check that readsb
  still requires 6–7 hex chars in `decodeSbsLine` (net_io.c:3112). If a future
  readsb accepts short ICAOs itself, this padding becomes redundant rather
  than wrong — but the length re-check and the `%06x` keying argument must be
  re-verified before removing it (`runbooks/readsb-upgrade-rollback.md`,
  `docs/readsb-pin.md`).
- Evidence is kept as a fixture: `test/fixtures/sbs/golden_icao_unpadded.txt`
  holds three real 2026-09-05 ADSBHub lines for 0D0EEC, verbatim.

## Amend

- Spec §12.4 (the ICAO field is accepted at 1–6 hex chars and left-padded to 6
  before relay; the padding is the enumerated exception to verbatim relay, with
  the readsb 6–7 char rule as its rationale).
- Core `sbs-parsing` note (same exception, plus the length re-check against
  readsb's 199-byte cap).
