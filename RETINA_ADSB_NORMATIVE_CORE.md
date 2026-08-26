# RETINA ADS-B v1 — Normative Core

**Companion to:** `RETINA_ADSB_IMPLEMENTATION_SPEC.md` (“the full specification”)
**Document version:** 1.2 (the two documents version in lockstep)
**Status:** Binding requirement set for v1 implementation and acceptance
**Date:** 2026-08-26

This document is the review and acceptance surface for the RETINA ADS-B data plane: it contains the binding requirements an implementer or reviewer works against daily. The full specification carries the architecture, rationale, data flows, configuration examples, and operational guidance behind them. On conflict, this document governs, and the conflict is a defect to fix in both documents in one change. `§n` references point into the full specification; where such a reference supplies parameters or procedures (Section 15 lists them), those are incorporated by reference and equally binding.

**MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** express requirement strength. “Local” means data received directly by the node’s attached ADS-B receiver. “Central” means data delivered by the RETINA server, regardless of whether it originated at ADSBHub or another RETINA receiver.

---

## 1. Architecture invariants

| ID | Invariant |
|---|---|
| AI-01 | The solver obtains all ADS-B data exclusively through the node-local `retina-adsb-agent` API. It MUST NOT connect to ADSBHub, central or local `readsb`, tar1090 files, or receiver TCP ports (§2). |
| AI-02 | The local path (receiver → sidecar → agent → solver) has no cloud dependency. The agent MUST start, serve its API, and report `DEGRADED_NO_SOURCES` even with zero sources. Disabling contribution or central consumption MUST never disable local service (§20.4, §32). |
| AI-03 | Local and central candidates are kept separate per aircraft and field group. The central feed MUST NOT enter the decoder instance that supplies the local candidate (§7.4). |
| AI-04 | Node contribution carries only local receiver data; central or redistributed data MUST NEVER be re-fed to the server. The ADSBHub contribution entitlement MUST be satisfied by a designated receiver feeding ADSBHub directly — never with hub-aggregate or central-derived data — and MUST be documented (§4.1, §7.4). |
| AI-05 | Default node integration is the isolated sidecar (Mode A): `--net-only`, read-only Beast client of the existing decoder, never opens the SDR, and leaves existing `readsb`/tar1090 service, config, and data paths unchanged (§6.2, §7.1). Optional Mode B changes to the existing decoder MUST preserve all existing command-line arguments and service overrides and validate port availability first (§7.2). |
| AI-06 | Public exposure is exactly TCP 443 (distribution gRPC, mTLS) and TCP 7444 (contribution feed, mTLS). No raw `readsb`, metrics, admin, or node-local port is Internet-exposed (§19). |
| AI-07 | The feed gateway maps each client certificate to exactly one `node_id` and expected receiver UUID, rejects mismatches, and MUST NOT act as a general TCP tunnel (§6.1). |
| AI-08 | Every queue, cache, history buffer, and reconnect loop is explicitly bounded. Retries use exponential backoff with jitter, capped at 30 s (node distribution) and 60 s (ADSBHub); 60 s of stable connection resets backoff (§24). |
| AI-09 | No provider-specific value, field name, or failure mode crosses the canonical contract (§3.3). |
| AI-10 | `readsb-hub` performs Mode-S decoding, CPR resolution, and message deduplication; RETINA MUST NOT reimplement those algorithms in v1 (§13.1). |

---

## 2. Identity, units, and field groups

`aircraft_key`: exactly six lowercase hexadecimal characters for ICAO addresses (`a1b2c3`); non-ICAO addresses keep the leading `~` plus six lowercase hex (`~abc123`). Invalid, empty, broadcast, or malformed keys are rejected. The key is a correlation identifier, not proof of identity (§9.1).

| Value | Unit/representation |
|---|---|
| Latitude/longitude | WGS-84 decimal degrees, `double` |
| Altitude | Feet, signed integer; geometric and barometric kept separate |
| Ground/air speed | Knots |
| Track/heading | Degrees in `[0, 360)` |
| Vertical rate | Feet per minute |
| Containment radius | Meters |
| Distance query radius | Meters in canonical API; config may accept km/nmi suffixes |
| Absolute time | Signed Unix epoch nanoseconds (`int64`) |
| Durations/ages | Milliseconds on wire where noted; seconds in Prometheus metric names |

Values MUST NOT silently convert altitude reference, heading reference, or speed type.

Fusion operates on atomic field groups, never arbitrary scalars; every populated group carries its own `ObservationMeta`:

| Group | Atomic members |
|---|---|
| `POSITION` | latitude, longitude, position integrity fields |
| `BARO_ALTITUDE` | barometric altitude or on-ground state |
| `GEOM_ALTITUDE` | geometric altitude |
| `GROUND_VECTOR` | ground speed, true track |
| `VERTICAL_RATE_BARO` / `VERTICAL_RATE_GEOM` | vertical rate by reference |
| `IDENTITY` | callsign |
| `SQUAWK_EMERGENCY` | squawk, emergency, SPI/alert |
| `NAVIGATION`, `AIR_DATA` | optional v1 pass-through |

Latitude and longitude MUST come from one `POSITION` candidate. Barometric and geometric altitude MUST never overwrite one another. Ground speed and track SHOULD come from one `GROUND_VECTOR` sample; a partial group MUST carry that fact. `on_ground` is tri-state: absent means unknown, `false` is an observed airborne indication; it is never altitude zero. An output state MAY combine groups from different sources and times.

---

## 3. Time model

Wire-mandatory per group: `age_ms_at_emit`, `time_basis`, `time_uncertainty_ms`, and (central records) `server_emitted_at_unix_ns`. Other absolute timestamps are optional and SHOULD be omitted from streamed upserts when reconstructible; an absent `observed_at_unix_ns` is defined as `server_emitted_at_unix_ns − age_ms_at_emit`, and consumers MUST reconstruct it that way (§10.1).

Time bases: `RECEIVER_OBSERVATION`, `READSB_SEEN_AGE`, `PROVIDER_TIMESTAMP`, `INGEST_RECEIPT`, `VALUE_CHANGE_ESTIMATE` (and `UNSPECIFIED`) (§10.2).

Monotonic clocks: all timeout, age-since-receipt, retry, and expiry logic uses a monotonic clock. Clock corrections MUST NOT make a cached field younger:

```text
effective_age(now) = age_at_local_receipt + monotonic_elapsed_since_receipt
```

Every host runs monitored time sync (e.g., chrony); alert at >100 ms offset for 5 min (warning) and >500 ms for 2 min (critical) (§10.3).

`readsb` mapping: `observed_at = json.now − json.seen_pos`. Reject negative `seen_pos` (clamp tiny float artifacts to zero) and values above the configured hard maximum. `seen` (any message) MUST NOT substitute for `seen_pos` (position). The pinned build’s timestamp resolution is measured and recorded as `source_timestamp_quantization_ms` (commonly 100 ms); `time_uncertainty_ms` for `READSB_SEEN_AGE` groups MUST be at least this value, and every constant bounded below by it (`exact_sample_window_ms`, `same_source_reorder_tolerance_ms`) MUST be validated against the measured value in CI (§10.4). Ancillary fields carried in a position object without their own message age use the position observation time with `TIME_BASIS_VALUE_CHANGE_ESTIMATE` or `TIME_BASIS_READSB_SEEN_AGE` and an uncertainty flag; consumers MUST NOT treat this as RF-exact timing.

Central network-delay penalty (§10.5): nodes probe RTT at least every 30 s and compute

```text
age_at_local_receipt = server_age_ms_at_emit + clamp(last_rtt_ms / 2, 0, max_network_penalty_ms)
```

with `unknown_network_penalty_ms` (default 250 ms) when no valid RTT exists. Groups whose timing derives from `TIME_BASIS_INGEST_RECEIPT`, or that cannot be attributed to a receiver with known timing, receive `provider_age_penalty_ms` (default 1,000 ms, included in `age_ms_at_emit`) and `ingest_uncertainty_floor_ms` (default 1,000 ms, applied to `time_uncertainty_ms`), carry `upstream_latency_known=false` and `provider_latency_unknown`, MUST NOT be classed `FRESH`, and are never precision-eligible. The server classifies timing trust per group from the best available provenance (`readsb` `type`, receiver id); when attribution is unavailable, the conservative treatment applies to all central groups (§13.1). Central provenance MUST truthfully report `SOURCE_CLASS_RETINA_CENTRAL` and MUST NOT invent an ADSBHub or node receiver ID; more specific source fields are populated only where `readsb` provides reliable provenance.

---

## 4. Contract and transports

The contract is the Protocol Buffer definition in full-specification §11, incorporated here verbatim and binding, until `proto/retina/adsb/v1/adsb.proto` exists in the repository; thereafter the repository file is authoritative and both documents reference it. Package `retina.adsb.v1`; services `DistributionService` (`Stream`, `Probe`) and `LocalAircraftService` (`GetSnapshot`, `Watch`, `GetAtTime`, `GetRegionAtTime`).

Schema evolution (§11.1): field numbers MUST NOT change or be reused; deletions are `reserved`; new enum values append; receivers tolerate unknown enum values and ignore unknown fields; the server supports current and previous minor versions during rolling upgrades; CI runs Protobuf lint, breaking-change detection, generated-code freshness, and cross-version serialization tests.

Contribution transport (§11.2): TCP 7444, mutual TLS (TLS 1.3 preferred; 1.2 minimum only where approved), ALPN `retina-adsb-feed/1` where supported; payload is an unmodified continuous BeastReducePlus stream, no custom framing/compression/multiplexing; one connection = one authenticated node/receiver pair; a valid receiver UUID marker is required within the first 64 KiB and 5 seconds and is then enforced for the session; certificate/UUID mismatch or undecodable prefix closes the connection without forwarding. The node forwarder buffers at most 1 MiB or 2 seconds (whichever is smaller) and MUST discard expired backlog rather than replay it as live. The pinned server `readsb` build MUST enable the upstream receiver-UUID feature, CI MUST verify BeastReducePlus UUID preservation with a real encoded fixture, and upstream build flags MUST be revalidated on upgrade (§4).

Distribution transport (§11.3): gRPC over HTTP/2 on TCP 443 with mutual TLS; production nodes MUST validate the server name and trust chain; certificate identity authorized before serving; TLS termination MUST preserve authenticated identity by a trusted mechanism (untrusted forwarded-identity headers prohibited); current and previous certificate chains accepted during rotation; compression off for live updates, measured for snapshot chunks; maximum encoded message 4 MiB, chunk target ≤512 KiB.

---

## 5. Validation and normalization

At every untrusted-input boundary (§12.1): reject NaN/infinity; enforce maximum message/line/frame sizes before allocation; bound collection counts and string lengths; right-trim callsigns preserving internal characters; lowercase aircraft keys and receiver UUIDs; validate UTF-8 and control characters; count rejects by reason without logging full payloads; never terminate a stream because one record is malformed.

| Field | Rule |
|---|---|
| latitude | `[-90, 90]`; reject outside |
| longitude | `[-180, 180]`; normalize +180 to −180 consistently |
| baro/geom altitude | `[-2,000, 100,000] ft` (configurable); reject outside |
| ground speed | `[0, 1,500] kt`; reject outside |
| track/heading | normalize finite values modulo 360; reject extreme parser overflow |
| vertical rate | `[-20,000, 20,000] ft/min`; reject outside |
| callsign | ≤16 UTF-8 bytes after trim; v1 SHOULD restrict to printable subset |
| squawk | exactly four octal digits |
| NIC/NAC/SIL | values within the canonical enum/range; unknown is absent, never zero-by-default |

Plausibility (§12.3) — hard-reject a new position if: invalid coordinates; age > `position_hard_reject_ms` (30,000); timestamp > `future_tolerance_ms` (1,000) ahead; implied speed > 1,500 kt airborne / 250 kt on-ground after a 250 m spatial tolerance, computed over `dt_effective = max(observed_dt, jump_check_dt_floor_ms (250), u_a + u_b)` where `u_a`, `u_b` are the samples’ time uncertainties; same-source sequence regression beyond the reorder window; configured geographic exclusion. One failure goes to a bounded diagnostic ring; two or more mutually consistent samples MAY re-establish the track.

SBS (§12.4): only explicitly tested BaseStation variants are parsed; unknown types are counted and ignored. CSV parsing MUST correctly handle empty fields and MUST NOT use naive string splitting if quoting is possible in the selected format. ADSBHub host, port, and parsing behavior MUST be configuration, not compiled constants. SBS carries no NIC/NACp/SIL and cannot distinguish direct ADS-B from MLAT/TIS-B; ADSBHub-derived groups carry `provider_surveillance_type_unknown`, never populate integrity fields, and never satisfy precision eligibility.

`readsb` mapping (§12.5): golden fixtures for every used field of the pinned build — `hex` (incl. `~`), `now`/`seen`/`seen_pos`, `lat`/`lon`, numeric altitude and the string `ground`, `alt_geom`, `gs`, `track`, `baro_rate`, `geom_rate`, `flight`, `squawk`, `emergency`, `category`, `nic`/`nac_p`/`sil`/`sil_type`/`rc`, `type`/`mlat`/`tisb` hints, and omitted/unknown fields; an upgrade is blocked if fixtures or a ≥30-minute shadow replay show incompatible mapping, parse-error increase, or freshness regression.

---

## 6. Source selection and fusion

Per `(aircraft_key, field_group)` the agent keeps at most one current LOCAL candidate, one current CENTRAL candidate, the selected candidate, and bounded position history. Candidates are never overwritten by an older same-source observation (§13.2).

Eligibility (§13.3): passed validation and plausibility; all required atomic members present; effective age ≤ the group’s selection TTL; no hard quality flag; observation time not unacceptably in the future.

```text
select(local, central, current, now):
  L = eligible(local, now)
  C = eligible(central, now)

  if !L and !C: return NONE
  if  L and !C: return local  (SELECTION_REASON_ONLY_VALID_CANDIDATE)
  if !L and  C: return central (SELECTION_REASON_ONLY_VALID_CANDIDATE)

  local_age   = effective_age(local, now)
  central_age = effective_age(central, now)
  delta       = local_age - central_age

  tie_window = local_preference_window_ms
  if current is eligible and current.source == CENTRAL:
      tie_window = local_preference_window_ms - switch_advantage_ms

  if abs(delta) <= tie_window:
      if local fails preferred integrity floor and central passes:
          return central (SELECTION_REASON_BETTER_INTEGRITY)
      return local (SELECTION_REASON_LOCAL_TIE_BREAK)

  younger = candidate with smaller age

  if current is eligible
     and younger.source != current.source
     and age(current) - age(younger) < switch_advantage_ms:
      return current (SELECTION_REASON_CURRENT_SOURCE_HYSTERESIS)

  return younger (SELECTION_REASON_MATERIALLY_FRESHER)
```

Defaults for `POSITION`: `local_preference_window_ms = 250`; `switch_advantage_ms = 100` (config validation MUST require `switch_advantage_ms < local_preference_window_ms`); `preferred_integrity_floor_nac_p = 5` when both sides report NACp — a tie-break floor only, distinct from precision eligibility (Section 7). Local therefore wins within 250 ms (150 ms while central is currently selected); hysteresis never retains an expired candidate; selection is recomputed on every candidate update and expiry timer (§13.4).

Group behavior (§13.5): position selection is independent of callsign, altitude, and velocity; a callsign change is accepted only after two matching observations or one high-confidence direct ADS-B update; emergency/status changes bypass update suppression and publish immediately.

Out-of-order and deduplication (§13.6): live state ignores a position older than the current same-source position by more than `same_source_reorder_tolerance_ms` (default 100 ms; ≥ measured quantization); a late position within `history_late_accept_ms` (5,000 ms) may enter time-sorted history; exact duplicates do not increment revision; a newer receipt of an identical value does not imply a newer observation; transport duplicates are removed by `(server_epoch, server_event_id)`; subscription-sequence gaps always cause resubscription.

---

## 7. Freshness, TTLs, and precision eligibility

| Effective position age | Class | Default solver treatment |
|---:|---|---|
| `0–1,000 ms` | `FRESH` | Eligible for precision association |
| `>1,000–2,000 ms` | `USABLE` | Eligible; report age and uncertainty |
| `>2,000–5,000 ms` | `STALE` | Not precision-eligible; cautious extrapolation on request |
| `>5,000 ms` | `EXPIRED` | Omit live position; bounded history/identity only |

| Field group | Fresh | Selection TTL | Cache retention |
|---|---:|---:|---:|
| Position | 1 s | 5 s | 120 s history |
| Baro/geom altitude | 2 s | 5 s | 30 s |
| Ground vector | 2 s | 5 s | 30 s |
| Vertical rate | 2 s | 5 s | 30 s |
| Squawk/emergency | 5 s | 15 s | 60 s |
| Callsign/category | 15 s | 60 s | 120 s |
| Integrity fields | atomic with position | with position | with position |
| Aircraft container without position | n/a | n/a | remove after 60 s without any group |

Thresholds are configurable centrally by signed node configuration with safe local defaults. An expired field is removed from live output and produces a state revision — never emitted as a magic zero/null (§14.1, §14.3).

`usable_for_precision_association` MUST be computed by the process emitting the `AircraftState` (the agent’s computation is authoritative for the solver) and is true only when all hold for the selected position group (§14.2):

1. freshness class is `FRESH`;
2. validation and plausibility passed and no `track_discontinuity` is active;
3. `time_basis` is `RECEIVER_OBSERVATION` or `READSB_SEEN_AGE`, and `time_uncertainty_ms` ≤ `precision_max_time_uncertainty_ms` (default 250);
4. neither `provider_latency_unknown` nor `provider_surveillance_type_unknown` is set;
5. when NACp is reported, NACp ≥ `precision_min_nac_p` (default 8, ≈93 m containment); when absent, requires `precision_allow_unknown_integrity` (default false);
6. `source_class` is not `SOURCE_CLASS_MLAT` or `SOURCE_CLASS_TISB`.

The solver MUST use `usable_for_precision_association` and position age rather than assume any returned coordinate is current (§14.1).

v1 `quality_flags`: `provider_latency_unknown`, `provider_surveillance_type_unknown`, `integrity_below_precision_floor`, `time_uncertainty_high`, `track_discontinuity`. Consumers MUST tolerate unknown flags; adding a flag is a documentation update, not a schema change.

---

## 8. Streaming and snapshot semantics

Subscription (§15.1): the first client message is `Subscribe`; the authenticated certificate identity MUST match `Subscribe.node_id` (`PERMISSION_DENIED` otherwise). `requested_features` is reserved: v1 defines none and servers ignore unknown entries. Region radius is derived per deployment from the solver detection envelope plus margin (cost scales with radius²); shipped default 150 km, server policy min/max recommended 25–800 km, clamp or reject above authorization.

Snapshot barrier (§15.2): record `barrier_event_id`; enumerate regional state at the barrier; buffer subsequent matching events in a bounded per-subscription queue; send begin/chunks/end with contiguous `subscription_sequence`; drain buffered events in order. Chunks: ≤500 aircraft or 512 KiB. The client commits its shadow table only after: matching snapshot id on all chunks; contiguous sequence; declared count match; SHA-256 match over the canonical manifest; and `SnapshotEnd` within `snapshot_timeout_ms` (10,000). Manifest: sort states by `aircraft_key` bytewise; append per state UTF-8 `aircraft_key`, one NUL, the unsigned decimal `revision`, one LF; hash with SHA-256.

Deltas (§15.3): an `AircraftUpsert` is the complete current state (idempotent), never a sparse patch. `AircraftDelete` is sent on group expiry, region exit past hysteresis, authorization/filter change, or correction; unknown-key deletes are idempotent success. Region hysteresis (§15.4): remain until outside `radius + boundary_hysteresis_m` (10,000 m); delete after two consecutive outside observations or `boundary_exit_hold_ms` (2,000 ms), whichever confirms first.

Ordering (§15.5): `subscription_sequence` starts at 1 and increments on every server message including snapshot frames and heartbeats; any gap, duplicate-with-different-payload, or regression invalidates the stream → close and resnapshot. `server_epoch` is a random 128-bit id per state-service start; event ids are monotonic only within an epoch; v1 clients SHOULD resnapshot after every reconnect.

Backpressure (§15.6): per-subscription queues bounded at 10,000 messages / 16 MiB; on overflow increment the slow-consumer counter, close with `RESOURCE_EXHAUSTED` and a retry hint, never block the global state loop, require a new snapshot. The server MUST coalesce queued upserts per aircraft before declaring overflow (deletes, per-aircraft ordering, and snapshot correctness preserved), so occupancy is bounded by regional aircraft count plus deletes. `acknowledged_subscription_sequence` is diagnostic-only in v1: sent at most once per heartbeat interval, never required, never used for flow control before a protocol minor defines it.

Decimation (§15.8): the server MUST support per-subscription minimum emit interval per aircraft, optionally tiered by distance from region center (default: full rate within 100 km, 1,000 ms minimum interval beyond). Coalescing is to the newest complete state; deletes, emergency/status changes, and snapshot frames are never decimated. Decimation parameters are part of node authorization and visible in agent diagnostics.

Heartbeat (§15.7): application heartbeat every 5 s when idle; HTTP/2 keepalive coordinated across client/server/LB; no aggressive sub-minute pings without active calls.

---

## 9. Local agent API

Transport (§16.1): preferred `unix:///run/retina/adsb-agent.sock`, owner/group/mode `retina-adsb:retina-solver 0660`; loopback TCP fallback binds only `127.0.0.1`/`::1` behind an OS firewall.

`GetSnapshot` (§16.2): complete point-in-time selected table; excludes expired positions by default; per-group provenance and age included; cap 5,000 aircraft (`RESOURCE_EXHAUSTED` beyond); recommended deadline 500 ms.

`Watch` (§16.3): caller snapshots, records `local_revision`, then opens `Watch(after_local_revision)`. The agent replays matching newer events from a ring covering ≥ `watch_replay_window_ms` (30,000 ms), compacted — superseded upserts for an aircraft MAY be dropped when the newest retained upsert and all deletes are preserved — within a 10,000-event bound after compaction. Older-than-ring requests return `OUT_OF_RANGE` → new snapshot. Revision gaps are legal (region filtering, compaction); regression or two different events with one revision is a protocol error. Complete state on every material change; deletions are explicit `LocalAircraftEvent.delete`, never an empty state; slow local consumers are disconnected and resnapshot.

`GetAtTime` (§16.4), for aircraft `A` at time `T`:

1. `EXACT_SAMPLE` when a sample lies within ±`exact_sample_window_ms` (default 100 ms; ≥ measured quantization);
2. else interpolate only between single-source-class bracketing positions, each valid, gap ≤2.5 s, motion plausible, `T` between them; cross-source brackets only when no single-source bracket exists, adding a warning and inflating `estimated_horizontal_uncertainty_m` by combined time uncertainty × ground speed;
3. else extrapolate only when requested, last valid position ≤2.0 s before `T`, a valid ground vector within 2.0 s of it, and no detected discontinuity;
4. else `NEAREST_SAMPLE` only when `allow_nearest_sample` is set, or `UNAVAILABLE`.

Interpolation operates in ECEF or on a geodesic (never linearly across the antimeridian); extrapolation uses a constant-velocity great-circle model; vertical estimation never converts barometric to geometric. Responses identify method, temporal distance, estimated uncertainty, source(s), and warnings. `GetRegionAtTime` applies the same rules to every aircraft whose estimated position at `T` lies in the region, omitting `UNAVAILABLE`, bounded by the `GetSnapshot` cap — one RPC per radar timestamp instead of one per aircraft.

Suppression (§16.5): an event may be suppressed only if all selected values, sources, and age-class boundaries are unchanged; position updates are never suppressed merely for small coordinate change.

---

## 10. History and resource bounds

Per aircraft (§17): 120 s / ≤512 position samples; time-sorted late insertion ≤5 s; duplicate collapse by source, observation time, and coordinate; cross-source collapse: a sample matching another source’s sample within `cross_source_duplicate_m` (10 m) and within their combined time uncertainty SHOULD be stored as a duplicate reference keeping the lower-uncertainty timestamp, not a distinct kinematic sample. Memory-only; no restart survival required. Optional diagnostic capture MUST be time-bounded, encrypted where required, and disabled by default.

Node-wide: ≤5,000 aircraft with state; ≤500,000 history samples; replay ring 30 s compacted / ≤10,000 events; agent memory ≤256 MiB. Under pressure evict expired/non-regional aircraft first, then oldest history; never intentionally kill the local API process.

---

## 11. Configuration, security, and compliance

Configuration (§18.1): YAML with `config_version`; unknown keys fatal; secrets only by file/secret-manager reference; `--check-config` runs without opening sockets; reloadable values documented; processes expose a redacted config hash and build version; fleet configuration is signed and staged by cohort. Feature flags MUST independently disable: ADSBHub ingest; contribution per node/cohort; central consumption per cohort; interpolation/extrapolation; optional ancillary groups; any new protocol-minor feature (§32).

Authentication (§21.1–21.2): unique per-node client certificates (never shared by fleet image), SAN carries `node_id`; feed authorization maps `node_id` → allowed receiver UUID(s), connection and rate policy; distribution authorization maps `node_id` → radius, features, environment. Node private keys MUST be generated in, and never leave, the node’s ATECC608B secure element where present (TLS client auth against the element; enrollment SHOULD reuse the existing RETINA chain-of-custody flow); file-based node keys (mode 0600, outside images) only on hardware without a secure element. Certificate lifetime 30–90 days; renewal starts before 50% consumed; current+next CA accepted during rotation; revocation propagates ≤15 minutes; a node with an expired/revoked certificate loses contribution and central access but keeps local service.

Input/resource protection (§21.3): size limits enforced before parsing; per-node token buckets on bytes and frames; global connection/memory caps; parser fuzzing is a release gate; logs escape untrusted values and never contain keys, tokens, or full frames; aircraft key, callsign, certificate subject, and unrestricted node id MUST NOT be metric labels (§22).

Compliance (§4.1): SBOM and license/redistribution review before production rollout; ADSBHub terms rechecked before launch and at least annually; the designated ADSBHub contribution station documented (AI-04). RETINA MUST define retention, access, and disclosure policy for ADS-B data (§21.5).

---

## 12. Performance objectives

Reference load: 5,000 concurrently tracked central aircraft, 200 node subscriptions, 100 contributing feeds, 5 position updates/s/aircraft burst tolerance, ≤400 aircraft within a default 150 km node region, instance CPU <70%. The 2,000-aircraft regional figures are capability bounds, not typical load. Bandwidth is a first-class budget: a full upsert encodes to ≈0.4–0.7 KB, and an undecimated 2,000-aircraft region at 2–4 updates/s/aircraft is ≈13–45 Mbps sustained — region radius, decimation, and meta slimming are the controls, and any change that increases encoded upsert size requires a bandwidth review (§25.2).

| SLO | Target |
|---|---|
| Local JSON receipt → agent selection p95 | ≤50 ms |
| Agent selection → solver watcher p95 | ≤20 ms |
| Server accepted update → subscribed node p95 | ≤250 ms; p99 ≤500 ms |
| Agent snapshot API p95 | ≤100 ms for 2,000 aircraft |
| Central reconnect → committed regional snapshot p95 | ≤5 s for 2,000 aircraft |
| Node local API availability | ≥99.99% monthly, excluding host downtime |
| Central distribution availability | ≥99.9% monthly initially |
| Lost accepted updates due to internal unbounded blocking | 0; overload must be explicit drops/disconnects |
| Agent steady-state memory | ≤256 MiB at reference node load |
| Agent steady-state CPU | ≤10% of one modern ARM core at 500 local/2,000 regional aircraft |
| Per-node sustained distribution throughput p95 | ≤2 Mbps at reference regional load (≈650 GB/month/node) |
| Fleet server egress | Projected from regions and decimation policy; approved before Phase 4 |

Capacity (§25.3): release tests pass at 2× expected volume for 60 minutes (no memory growth, crash, invalid sequence, or p95 > 2× SLO); a 5× 60-second burst recovers within 30 seconds without replaying stale queues as fresh. Any `readsb`, parser, or fusion change requires a ≥30-minute replay comparison; rollout is blocked on unexplained loss, stale-age regression, or implausible positions (§26.6).

---

## 13. Acceptance criteria

Criteria are staged into two gates; evidence requirements are identical, only timing differs:

- **Gate 1 — canary production (Phase 3):** required before any production solver consumes the agent API.
- **Gate 2 — fleet rollout (Phase 4):** additionally required before expansion beyond the canary cohort.

### 13.1 Functional

| ID | Criterion | Gate |
|---|---|---|
| AC-F-01 | Solver obtains all ADS-B data exclusively through the node-local API. | 1 |
| AC-F-02 | A fresh local position is selected over central within the effective tie window (250 ms; 150 ms while central is currently selected, §13.4). | 1 |
| AC-F-03 | A valid central position is selected when local is absent, invalid, or expired. | 1 |
| AC-F-04 | A central position materially newer than local wins according to configured policy. | 1 |
| AC-F-05 | Position, altitude types, ground vector, identity, and status retain independent metadata/TTL. | 1 |
| AC-F-06 | Latitude and longitude never come from different observations. | 1 |
| AC-F-07 | Startup/reconnect commits only a complete verified snapshot, followed by ordered deltas. | 1 |
| AC-F-08 | Sequence gaps cause resnapshot without corrupting committed state. | 1 |
| AC-F-09 | Aircraft expiry/region exit reaches nodes as an explicit tombstone. | 1 |
| AC-F-10 | `GetAtTime` and `GetRegionAtTime` return correct exact/interpolated/extrapolated/unavailable method and uncertainty. | 1 |
| AC-F-11 | Node contribution contains only local receiver data and cannot form a feedback loop (including the §4.1 ADSBHub contribution path). | 1 |
| AC-F-12 | `usable_for_precision_association` is computed per §14.2; ADSBHub-derived positions never set it. | 1 |

### 13.2 Resilience and non-disruption

| ID | Criterion | Gate |
|---|---|---|
| AC-R-01 | Stopping all central components for 30 minutes does not interrupt local receiver → agent → solver updates. | 1 |
| AC-R-02 | Stopping local decoder input causes central fallback without restarting agent or solver. | 1 |
| AC-R-03 | Existing tar1090 remains usable and its original service/config/data paths remain unchanged in default integration mode. | 1 |
| AC-R-04 | Interrupted snapshots never replace the last committed central table. | 1 |
| AC-R-05a | 72-hour server soak and 48-hour node soak show no unbounded memory, queue, disk, or descriptor growth. | 1 |
| AC-R-05b | Full §26.7 soaks (7-day node with fault cycling; 24 h server at 2×) pass. | 2 |
| AC-R-06 | A slow or malformed node cannot stall other subscriptions or ingest. | 1 |
| AC-R-07 | Rollback to the prior schema-compatible agent/server succeeds without editing solver code. | 1 |

### 13.3 Performance

| ID | Criterion | Gate |
|---|---|---|
| AC-P-01 | Local decode-to-selection p95 ≤50 ms at reference node load. | 1 |
| AC-P-02 | Agent-selection-to-solver p95 ≤20 ms. | 1 |
| AC-P-03 | Server-accepted-to-node p95 ≤250 ms and p99 ≤500 ms at reference load. | 1 |
| AC-P-04 | Regional snapshot of 2,000 aircraft commits within 5 s p95 after reconnect. | 1 |
| AC-P-05 | 2× capacity test passes for 60 minutes and 5× burst recovers within 30 seconds. | 2 |
| AC-P-06 | Position ages are nondecreasing between observations and cross class/expiry thresholds on time ±50 ms in deterministic tests (synthetic timestamps; real-source tolerance additionally includes the measured §10.4 quantization). | 1 |
| AC-P-07 | Per-node distribution throughput at reference regional load is within the §25.2 budget. | 1 |

### 13.4 Security

| ID | Criterion | Gate |
|---|---|---|
| AC-S-01 | Public scan exposes only approved TLS endpoints; no raw `readsb`, metrics, admin, or node-local ports. | 1 |
| AC-S-02 | Invalid/expired/wrong-node client certificates are rejected. | 1 |
| AC-S-03 | Receiver UUID mismatch is rejected and audited. | 1 |
| AC-S-04 | Fuzz suites produce no crash, hang, unbounded allocation, or secret disclosure. | 1 |
| AC-S-05 | Images have SBOMs, pass vulnerability policy, run with least privilege, and contain no node private keys. | 1 |
| AC-S-06 | Credential rotation and revocation are demonstrated without loss of local service — before Gate 2 and in any case before the first certificate renewal window. | 2 |

### 13.5 Observability and operations

| ID | Criterion | Gate |
|---|---|---|
| AC-O-01a | Pipeline, freshness, and node source-selection dashboards (§22.4 items 1–3) are operational. | 1 |
| AC-O-01b | Remaining §22.4 dashboards are operational. | 2 |
| AC-O-02 | Each failure in §24 produces the documented status/metric/alert without an alert storm. | 1 |
| AC-O-03 | Operators can identify whether staleness originates before server ingest, in server processing, network delivery, or node selection when timing information permits. | 1 |
| AC-O-04 | Build, schema, and redacted config hashes are visible for server and every node. | 1 |
| AC-O-05a | Runbooks exist for provider outage, node local receiver loss, certificate failure, and rollback. | 1 |
| AC-O-05b | The full §32 runbook set exists. | 2 |

---

## 14. Required invariants and test vectors

Property invariants (§26.1), enforced by property-based tests:

- the selected candidate is eligible;
- selected position coordinates come from one candidate;
- effective age never decreases without a newer observation;
- an expired local candidate cannot block an eligible central candidate;
- central failure cannot delete an eligible local candidate;
- a sequence gap cannot produce a partial snapshot commit.

Selection truth table (tie window 250 ms; integrity acceptable unless noted; `current = none` unless stated — while central is currently selected the local tie-break threshold is 150 ms):

| Local age | Central age | Local valid | Central valid | Expected selection |
|---:|---:|---:|---:|---|
| 80 ms | 700 ms | yes | yes | Local: materially fresher |
| 300 ms | 100 ms | yes | yes | Local: inside 250 ms tie window |
| 500 ms | 100 ms | yes | yes | Central: materially fresher |
| 900 ms | 100 ms | poor integrity | good | Central: integrity/freshness |
| 4,900 ms | 300 ms | yes | yes | Central |
| 5,001 ms | 400 ms | expired | yes | Central: only eligible |
| absent | 400 ms | no | yes | Central |
| 100 ms | absent | yes | no | Local |
| 5,100 ms | 5,200 ms | expired | expired | No live position |
| 200 ms | 0 ms | yes | yes, currently selected | Central: tie window tightened to 150 ms |

Minimum fusion test vectors:

1. Equal timestamps, local and central identical → local.
2. Equal timestamps, coordinates differ within tolerance → local plus discrepancy metric.
3. Equal timestamps, coordinates imply impossible separation → reject suspect candidate per continuity rules.
4. Central 249 ms newer → local.
5. Central 251 ms newer but current local and switch advantage 100 ms → central.
6. Central 300 ms newer, central fails range validation → local.
7. Local expires between input messages → timer-triggered central selection.
8. Central deletes aircraft while local remains fresh → local remains.
9. Local disappears while central copy exists → central selected without empty intermediate state where ordering permits.
10. Both expire → position removed and deletion/update emitted.
11. Late local sample enters history but does not replace live newer position.
12. Server epoch changes with lower event ID → resnapshot, not regression error.
13. Central currently selected, local age deficit oscillating 140–260 ms → local reselected only at ≤150 ms; no flapping at the 250 ms boundary.
14. Same broadcast via local and central at identical coordinates, observation-time estimates 300 ms apart → one kinematic sample in history; single-source interpolation bracket; no near-zero-velocity artifact.
15. ADSBHub-derived position, 400 ms transport age → selectable as fallback after the §10.5 penalty, but `usable_for_precision_association=false` with `provider_latency_unknown`.

---

## 15. Incorporated by reference

The following full-specification sections supply binding parameters and procedures not restated here:

- §11 — the Protocol Buffer contract text (until the repository proto file exists; see Section 4);
- §14.4 — stream-health degraded/unhealthy thresholds (an active empty feed is distinguished from a dead connection by bytes/heartbeats/decoder statistics, never aircraft count alone);
- §20 — deployment isolation, installer preflight/verify/activate/rollback sequence, Mender-pipeline delivery where operated;
- §22 — required metric, log-field, dashboard, and alert sets;
- §23 — liveness/readiness semantics (empty airspace is never a liveness failure);
- §24 — per-failure required behavior and operator signals;
- §26 — test program: unit/property coverage, golden fixtures, contract tests, integration scenarios, fault injection, replay/shadow procedure, soak durations, security tests;
- §27 — CI/CD quality gates;
- §28 — rollout phases and gate mapping;
- §31 — required ADRs before code freeze (including ADR 13: region radius and decimation tiers from the solver detection envelope);
- §32 — runbook set and feature-flag requirements.
