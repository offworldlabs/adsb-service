# Spec amendment (DRAFT): interim adsb.lol v2 serving contract

> **Status: DRAFT.** Not yet part of the binding document set. Requires
> spec-owner signoff and integration into BOTH
> `RETINA_ADSB_NORMATIVE_CORE.md` and `RETINA_ADSB_IMPLEMENTATION_SPEC.md`
> in one change with a lockstep version bump (README governance rule; core
> preamble conflict rule). Until merged, ADR-0004 and the conformance ledger
> record the divergence as deliberate.

## Purpose

Core §4 makes the §11 Protocol Buffer definition the only contract and has no
interim-serving provision. Phase A (plan §0) serves nodes the adsb.lol v2
REST format they already consume. Per the core's own conflict rule, that
divergence is a defect resolved by this amendment: it documents the interim
contract — fields, `seen_pos` semantics including upstream age, staleness
guarantees, sunset — as plan §0 commits to.

## 1. Endpoint

```
GET /v2/lat/{lat}/lon/{lon}/dist/{radius}
GET /v2/point/{lat}/{lon}/{radius}          (alias, same semantics)
```

Parameters:

| Param | Type / range | Notes |
|---|---|---|
| `lat` | float, −90..90 | WGS-84 decimal degrees |
| `lon` | float, −180..180 | WGS-84 decimal degrees |
| `radius` | float, ≥ 0, **nautical miles** | clamped to `min(radius, 250)` nmi (adsb.lol behavior). NOTE: unit diverges from the canonical API's meters (core §2) — this is the adsb.lol convention the node proxy already assumes. |

Out-of-range `lat`/`lon` → 400. Upstream (readsb) unavailable → 502 JSON
`{"msg": "upstream unavailable"}`; timeout → 504. Excess rate → 429 +
`Retry-After`.

## 2. Envelope

The response body is readsb's `&jv2` envelope, passed through verbatim
(byte-compatible with adsb.lol by construction):

```json
{"ac": [...], "msg": "No error", "now": <int ms>, "total": <N>,
 "ctime": <int ms>, "ptime": <ms>}
```

- `ac` — array of aircraft objects (below).
- `now` (= `ctime`) — **integer milliseconds** since epoch: the re-api cache
  build time, refreshed every `write-json-every` (1 s default).
- `msg` — literal `"No error"` on 200. Error paths are HTTP-status JSON from
  the serving tier (above), never a mutated envelope.
- `ground` movers render as the **string** `"ground"` in `alt_baro`
  (`--json-separate-ground` is forbidden on the hub for exactly this reason).

## 3. Fields served for SBS-derived aircraft

Recorded from the live envelope fixture of the pinned build
(`docs/requirements/code-readsb.md` / `code-adsblol-api.md`;
fixture: `test/fixtures/api/`). Per aircraft:

| Field | Type | Notes |
|---|---|---|
| `hex` | string | 6 lowercase hex, `~` prefix for non-ICAO |
| `type` | string | always `"other"` for SBS input (ADDR_OTHER) |
| `flight` | string | callsign, right-padded, only when upstream sent one |
| `alt_baro` | int feet **or** string `"ground"` | omitted when unreliable |
| `gs` | float kt | when sent |
| `track` | float deg | when sent |
| `baro_rate` | int ft/min | when sent |
| `squawk` | string | 4 octal digits, when sent |
| `lat`, `lon` | float deg | 6 dp |
| `nic` | int | **always 0** — SBS carries no integrity data; consumers MUST NOT read this as a measured NIC |
| `rc` | int | always 0 (RC_UNKNOWN) |
| `seen_pos` | float s | see §4 — the load-bearing freshness field |
| `seen` | float s | age of ANY message, 0.1 s resolution — MUST NOT substitute for `seen_pos` (spec §33 note 2) |
| `alert`, `spi` | flags | when sent |
| `mlat`, `tisb` | arrays | always `[]` for SBS input |
| `messages` | int | message count |
| `rssi` | float | **placeholder −49.5** — SBS carries no signal level; not a measurement |
| `dst` | float nmi | distance from query point (circle queries) |
| `dir` | float deg | true bearing from query point (circle queries) |

**Absent fields — consumers MUST tolerate their absence:**

- `alt_geom` — SBS carries no geometric altitude; it is never present for
  ADSBHub-derived aircraft and MUST NOT be synthesized from `alt_baro`
  (§33 note 9). Known consumer impact: blah2-api falls back to `alt_baro`
  (fine); the dormant adsb2dd path hard-drops aircraft lacking
  `alt_geom`/`flight` — a blocker only if revived against this feed.
- `nac_p`, `sil`, `sil_type`, `category`, `emergency`, `geom_rate`,
  `track_rate`, `roll`, etc. — never populated from SBS. No integrity or
  precision metadata is fabricated (core §5 sbs-parsing consequence: nothing
  served implies RF-exact timing or integrity SBS cannot carry).

## 4. `seen_pos` semantics (including upstream age)

`seen_pos` = seconds (1 ms resolution) between the last reliable position and
the envelope's `now`. Client reconstruction
`observation_epoch_ms = now − seen_pos × 1000` is exact.

**`seen_pos` includes ADSBHub upstream + ingest latency.** readsb clocks all
aircraft state from the SBS "generated" timestamp (fields 7/8, UTC, ms) and
subtracts from wall clock at cache-build time — verified live: 3.0 s of
injected upstream lag served as `seen_pos` 3.414 (ADR-0004,
code-readsb.md). This satisfies plan §0 req 1: the node-side reconstruction
(wall clock − `seen_pos`, hard |dt| > 5 s refusal) sees true observation age.

**Clamp caveats** (the honesty boundary):

- readsb rewrites the timestamp to arrival wall clock when it is > 1 s ahead
  of arrival or > 20 min behind, or unparseable. In those modes `seen_pos`
  degrades to receipt-based age (upstream latency invisible), bounded on the
  fast side by the 1 s clamp window.
- The adapter guard keeps the degraded modes rare and bounded: lines with a
  generated timestamp > 1 s in the future or > 60 s stale are rejected before
  readsb (counted per reason); unparseable timestamps are relayed and readsb
  stamps arrival (conservative).
- Degradation is **observable**, not silent: the
  `retina_adsb_provider_record_delay_seconds` histogram and the
  `records_total{result=stale|future|unparseable_ts}` counters expose
  upstream clock misbehavior continuously (Q7 evidence).

`seen` (any-message age) MUST NOT be used for position freshness.

## 5. Staleness guarantees

- Envelope `now` is the api cache build time; the cache refreshes every
  `write-json-every` (**1 s** default, 100 ms floor) — served data adds at
  most one interval of staleness on top of `seen_pos`.
- Aircraft are evicted from the api/JSON ~**60 s** after their last message;
  positions older than 60 s are no longer matched by circle queries (they
  fall back to `lastPosition` in aircraft.json only). No aircraft is served
  with a position age the `seen_pos` field does not disclose.
- The adapter's **60 s stale guard** bounds input age: records older than
  60 s at ingest never enter the hub, so replayed backlogs cannot appear
  fresh (spec §33 note 11).
- On upstream (ADSBHub) failure the adapter closes the relay; hub aircraft
  age out and expire — no frozen-but-fresh-looking state.

## 6. Rate expectations

Sized for ~**1 Hz polling per node** (plan §0 req 5): per-client-IP token
bucket, default rate 2/s, burst 10; 429 + `Retry-After` on excess; bounded
client table. Nodes SHOULD poll at 1 Hz and MUST honor `Retry-After`.

## 7. Auth boundary

**Placeholder — pending Q5** (ADR-0005, decision before first node cutover).
Default-closed loopback until an operator enables one of the two shipped
fronts (nginx mTLS — recommended — or private network + firewall).
Unauthenticated public exposure is forbidden in every reading (AC-S-01).
This amendment will be updated with the chosen boundary when Q5 resolves.

## 8. SUNSET

This contract is **temporary**. It is retired when Phase B lands the native
contract: `proto/retina/adsb/v1/adsb.proto` committed to the repository
(transferring contract authority per core §4) and nodes migrated to the
retina.adsb.v1 surface. From that point the interim endpoint accepts no new
consumers and is removed after the last node migrates. The surface stays
minimal (the two routes above + `/health` + `/metrics`) until then; any
change to it requires a revision of this amendment (plan §4 lock-in risk).

## Integration checklist (for the spec owner)

- [ ] Merge into `RETINA_ADSB_IMPLEMENTATION_SPEC.md` (new §, cross-refs
      from §11/§19/§33) and `RETINA_ADSB_NORMATIVE_CORE.md` (interim-serving
      provision + AI-06 exposure note) in ONE change.
- [ ] Lockstep version bump in both docs.
- [ ] Fold in the ADR-0006 resolutions touching the same sections.
- [ ] Resolve the Q5 placeholder (§7) once decided.
