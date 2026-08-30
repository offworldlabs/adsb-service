# Normative core — requirements binding on server-only Phase A

> Machine-extracted from the binding docs by reader agents (2026-08-29).
> On any doubt, the spec/core text governs — verify against the source section.

### [MUST] AI-01 (core §1 AI-01)

"The solver obtains all ADS-B data exclusively through the node-local retina-adsb-agent API. It MUST NOT connect to ADSBHub, central or local readsb, tar1090 files, or receiver TCP ports (§2)."

*Notes: deferred (node-side, Phase C) — but note interaction with plan: after cutover nodes consume the interim adsb.lol v2 REST endpoint via tar1090-node proxy, not a retina-adsb-agent; that is diverged-by-plan and must be legitimized by the planned spec amendment. No node consumes it during Phase A (cutover gated on Q7/soak), so Phase A itself does not violate.*

### [MUST] AI-02 (core §1 AI-02)

"The local path (receiver → sidecar → agent → solver) has no cloud dependency. The agent MUST start, serve its API, and report DEGRADED_NO_SOURCES even with zero sources. Disabling contribution or central consumption MUST never disable local service (§20.4, §32)."

*Notes: deferred — entirely node-side (Phase C). No server-side Phase A obligation.*

### [MUST] AI-03 (core §1 AI-03)

"Local and central candidates are kept separate per aircraft and field group. The central feed MUST NOT enter the decoder instance that supplies the local candidate (§7.4)."

*Notes: deferred — node-side fusion (Phase C). Server-side corollary already covered by AI-04 (no central data re-fed upstream).*

### [MUST] AI-04 (core §1 AI-04)

"Node contribution carries only local receiver data; central or redistributed data MUST NEVER be re-fed to the server. The ADSBHub contribution entitlement MUST be satisfied by a designated receiver feeding ADSBHub directly — never with hub-aggregate or central-derived data — and MUST be documented (§4.1, §7.4)."

*Notes: satisfied-in-Phase-A (applies server-side): designated station is the receiver at static IP 47.46.206.146 (plan §0); Phase A MUST confirm it actively feeds ADSBHub from its own decoder (plan A0) and record it in deployment docs. Nothing in Phase A may forward hub/central data to ADSBHub.*

### [MUST] AI-05 (core §1 AI-05)

"Default node integration is the isolated sidecar (Mode A): --net-only, read-only Beast client of the existing decoder, never opens the SDR, and leaves existing readsb/tar1090 service, config, and data paths unchanged (§6.2, §7.1). Optional Mode B ... MUST preserve all existing command-line arguments and service overrides and validate port availability first (§7.2)."

*Notes: deferred — node-side (Phase C, Mode A only per plan §3).*

### [MUST] AI-06 (core §1 AI-06)

"Public exposure is exactly TCP 443 (distribution gRPC, mTLS) and TCP 7444 (contribution feed, mTLS). No raw readsb, metrics, admin, or node-local port is Internet-exposed (§19)."

*Notes: partial + diverged-by-plan: the second sentence binds fully in Phase A (readsb-hub 31003/31004/31005 loopback/private only, metrics 9090+ monitoring network only, per spec §19). The first sentence is diverged: Phase A exposes neither 443-gRPC nor 7444; it exposes an interim HTTPS REST endpoint not in the AI-06 enumeration — that surface MUST NOT be unauthenticated-public (AC-S-01, plan A2/Q5: mTLS or network policy so only RETINA nodes/operators reach it). Spec amendment should record the interim exposure.*

### [MUST] AI-07 (core §1 AI-07)

"The feed gateway maps each client certificate to exactly one node_id and expected receiver UUID, rejects mismatches, and MUST NOT act as a general TCP tunnel (§6.1)."

*Notes: deferred — retina-feed-gateway is not built in Phase A (no node contributions until Phase B/C).*

### [MUST] AI-08 (core §1 AI-08)

"Every queue, cache, history buffer, and reconnect loop is explicitly bounded. Retries use exponential backoff with jitter, capped at 30 s (node distribution) and 60 s (ADSBHub); 60 s of stable connection resets backoff (§24)."

*Notes: satisfied-in-Phase-A (must implement): adsbhub-adapter reconnect uses exponential backoff + jitter capped at 60 s, reset after 60 s stable (spec §24 default progression 250 ms, 500 ms, 1 s, 2 s, 4 s ... cap); adapter relay buffer, serving-tier request/response sizes, and rate limits all explicitly bounded (plan A2 'bounded everything').*

### [MUST] AI-09 (core §1 AI-09)

"No provider-specific value, field name, or failure mode crosses the canonical contract (§3.3)."

*Notes: deferred/diverged-by-plan: no canonical contract exists in Phase A; the interim surface deliberately serves readsb/adsb.lol-shaped fields. Core §4 treats the §11 proto as the binding contract; the plan treats the adsb.lol format as a documented temporary contract with a sunset (requires the spec amendment). Keep the interim surface minimal to limit lock-in (plan §4 risk).*

### [MUST] AI-10 (core §1 AI-10)

"readsb-hub performs Mode-S decoding, CPR resolution, and message deduplication; RETINA MUST NOT reimplement those algorithms in v1 (§13.1)."

*Notes: satisfied-in-Phase-A: readsb-hub from the adsb.lol container lineage does all decode/CPR/dedup; adapter only validates/relays SBS lines; serving tier only reads readsb-hub state.*

### [MUST] aircraft-key (core §2 / spec §9.1)

aircraft_key is exactly six lowercase hexadecimal characters for ICAO (a1b2c3); non-ICAO keeps leading ~ plus six lowercase hex (~abc123). Invalid, empty, broadcast, or malformed keys are rejected. Key is a correlation identifier, not proof of identity.

*Notes: satisfied-in-Phase-A at the adapter boundary: SBS ICAO-syntax validation per §6.1 S1; lowercase normalization per §12.1. readsb-hub then re-keys internally; serving tier passes readsb 'hex' through.*

### [MUST] units-no-silent-conversion (core §2)

Units: lat/lon WGS-84 decimal degrees double; altitude feet signed int, geometric and barometric kept separate; speeds knots; track/heading degrees [0,360); vertical rate ft/min; radii meters; absolute time int64 Unix ns; durations ms on wire / seconds in Prometheus names. "Values MUST NOT silently convert altitude reference, heading reference, or speed type."

*Notes: partial-in-Phase-A: applies to the adapter (SBS fields relayed unconverted) and serving tier (readsb passthrough keeps feet/knots). SBS carries no geometric altitude, so alt_geom is absent from served aircraft — never substitute alt_baro for it (plan §0 req 3). Field groups/ObservationMeta themselves are deferred (Phase B).*

### [MUST] time-monotonic (core §3 / spec §10.3)

"All timeout, age-since-receipt, retry, and expiry logic uses a monotonic clock. Clock corrections MUST NOT make a cached field younger: effective_age(now) = age_at_local_receipt + monotonic_elapsed_since_receipt."

*Notes: satisfied-in-Phase-A (must implement): adapter backoff/relay timers and any serving-tier staleness logic use monotonic clocks.*

### [MUST] time-sync (core §3 / spec §10.3)

"Every host runs monitored time sync (e.g., chrony); alert at >100 ms offset for 5 min (warning) and >500 ms for 2 min (critical)."

*Notes: satisfied-in-Phase-A: single droplet host runs chrony (or equivalent) with the ClockOffset alert (spec §22.5) at exactly these thresholds — part of A3 observability.*

### [MUST] readsb-time-mapping (core §3 / spec §10.4)

"observed_at = json.now − json.seen_pos. Reject negative seen_pos (clamp tiny float artifacts to zero) and values above the configured hard maximum. seen (any message) MUST NOT substitute for seen_pos (position)."

*Notes: satisfied-in-Phase-A: serving tier freshness derives only from seen_pos, never seen; interim consumers (blah2-api) reconstruct observation time as wall clock − seen_pos, so this semantic is load-bearing (plan §0 req 1).*

### [MUST] timestamp-quantization (core §3 / spec §10.4)

"The pinned build's timestamp resolution is measured and recorded as source_timestamp_quantization_ms (commonly 100 ms); time_uncertainty_ms for READSB_SEEN_AGE groups MUST be at least this value, and every constant bounded below by it (exact_sample_window_ms, same_source_reorder_tolerance_ms) MUST be validated against the measured value in CI."

*Notes: partial-in-Phase-A: the measurement/recording is a Phase A A0 task (plan: pin readsb build, ADR 2, verify WITH_UUIDS, golden fixtures, measure quantization). The time_uncertainty_ms propagation and CI constant validation are deferred to Phase B (no ObservationMeta yet).*

### [MUST] provider-age-penalty (core §3 / spec §10.5)

Groups whose timing derives from TIME_BASIS_INGEST_RECEIPT or lack receiver attribution receive provider_age_penalty_ms (default 1,000 ms, included in age_ms_at_emit) and ingest_uncertainty_floor_ms (default 1,000 ms on time_uncertainty_ms), carry upstream_latency_known=false and provider_latency_unknown, MUST NOT be classed FRESH, and are never precision-eligible.

*Notes: diverged-by-plan (interim analog): canonical penalty mechanics deferred to Phase B, but plan §0 req 1 translates the intent — served seen_pos MUST include ADSBHub upstream/ingest latency ('the §10.5 penalty in interim clothing'), because blah2-api applies a hard |dt| > 5 s extrapolation refusal with no staleness cutoff on the live association path. AMBIGUOUS: mechanism undefined — readsb's seen_pos counts from readsb-hub receipt, not upstream observation, and stock adsblol/api cannot add upstream latency; design must choose (e.g., measured/configured additive penalty, or SBS provider-timestamp-derived offset after A3 validation). Spec §33 note 5: default to conservative receipt-based time until SBS timestamps are validated.*

### [MUST] validation-general (core §5 / spec §12.1)

At every untrusted-input boundary: "reject NaN/infinity; enforce maximum message/line/frame sizes before allocation; bound collection counts and string lengths; right-trim callsigns preserving internal characters; lowercase aircraft keys and receiver UUIDs; validate UTF-8 and control characters; count rejects by reason without logging full payloads; never terminate a stream because one record is malformed."

*Notes: satisfied-in-Phase-A (must implement): binds the adsbhub-adapter SBS boundary and any serving-tier request parsing. Note the distinction from S1's close-relay-on-upstream-failure: one malformed line never kills the session; a dead upstream MUST close the relay.*

### [MUST] range-checks (core §5 / spec §12.2)

latitude [-90, 90] reject outside; longitude [-180, 180], normalize +180 to −180 consistently; baro/geom altitude [-2,000, 100,000] ft (configurable) reject outside; ground speed [0, 1,500] kt reject outside; track/heading normalize finite values modulo 360, reject extreme parser overflow; vertical rate [-20,000, 20,000] ft/min reject outside; callsign ≤16 UTF-8 bytes after trim (v1 SHOULD restrict to printable subset); squawk exactly four octal digits; NIC/NAC/SIL unknown is absent, never zero-by-default.

*Notes: satisfied-in-Phase-A: applies to every SBS field the adapter parses (lat, lon, altitude, gs, track, vr, callsign, squawk). NIC/NAC/SIL rule is moot for SBS (fields absent) but binds the design to never fabricate them.*

### [MUST] plausibility-checks (core §5 / spec §12.3)

Hard-reject a new position if: invalid coordinates; age > position_hard_reject_ms (30,000); timestamp > future_tolerance_ms (1,000) ahead; implied speed > 1,500 kt airborne / 250 kt on-ground after a 250 m spatial tolerance over dt_effective = max(observed_dt, jump_check_dt_floor_ms (250), u_a + u_b); same-source sequence regression beyond reorder window; configured geographic exclusion. One failure to bounded diagnostic ring; ≥2 mutually consistent samples MAY re-establish track.

*Notes: deferred (mostly): these are state-layer (retina-adsb-server, Phase B) functions; Phase A relies on readsb-hub's native handling and per-field range checks at the adapter. FLAG for ledger: Phase A served data has NOT passed §12.3 jump/staleness rejection — an acknowledged gap of the interim surface.*

### [MUST] sbs-parsing (core §5 / spec §12.4)

"Only explicitly tested BaseStation variants are parsed; unknown types are counted and ignored" until a fixture and mapping are added. "CSV parsing MUST correctly handle empty fields and MUST NOT use naive string splitting if quoting is possible in the selected format. ADSBHub host, port, and parsing behavior MUST be configuration, not compiled constants." Provider timestamps parsed in a configured timezone (default UTC) and never trusted without sanity checks.

*Notes: satisfied-in-Phase-A (core adapter requirement, plan A1). SBS carries no NIC/NACp/SIL and cannot distinguish direct ADS-B from MLAT/TIS-B — ADSBHub-derived groups carry provider_surveillance_type_unknown, never populate integrity fields, never satisfy precision eligibility (§14.2); in Phase A the flag itself is deferred but the never-fabricate-integrity consequence binds the interim output.*

### [MUST] readsb-golden-fixtures (core §5 / spec §12.5)

Golden fixtures for every used field of the pinned readsb build: hex (incl. ~), now/seen/seen_pos, lat/lon, numeric altitude and the string "ground", alt_geom, gs, track, baro_rate, geom_rate, flight, squawk, emergency, category, nic/nac_p/sil/sil_type/rc, type/mlat/tisb hints, and omitted/unknown fields. "An upgrade is blocked if fixtures or a ≥30-minute shadow replay show incompatible mapping, parse-error increase, or freshness regression."

*Notes: satisfied-in-Phase-A: plan A0 pins the readsb build (ADR 2) and captures golden JSON fixtures; the upgrade shadow-replay gate binds any Phase A readsb-hub upgrade. Serving tier consumes these same fields via adsblol/api.*

### [MUST] fusion-selection (core §6 (spec §13))

Per (aircraft_key, field_group) candidate tables, deterministic select() with local_preference_window_ms=250, switch_advantage_ms=100 (config validation MUST require switch_advantage_ms < local_preference_window_ms), preferred_integrity_floor_nac_p=5, selection reasons, out-of-order rules (same_source_reorder_tolerance_ms 100 ms default; history_late_accept_ms 5,000 ms).

*Notes: deferred wholesale — node-side fusion engine ships with Phase C (plan §2/§3); the §14 property invariants, selection truth table, and fusion test vectors 1-14 go with it. No Phase A obligation.*

### [MUST] freshness-classes-ttls (core §7 (spec §14.1, §14.3))

Position freshness classes: FRESH 0–1,000 ms; USABLE >1,000–2,000 ms; STALE >2,000–5,000 ms (not precision-eligible); EXPIRED >5,000 ms (omit live position). Group TTLs: position fresh 1 s / selection TTL 5 s / 120 s history; altitudes+vectors 2 s/5 s/30 s; squawk 5 s/15 s/60 s; callsign 15 s/60 s/120 s; aircraft container removed after 60 s without any group. Expired fields removed from live output, never magic zero/null.

*Notes: deferred (canonical classes/TTLs are Phase B/C), but the EXPIRED >5,000 ms boundary is exactly the node's blah2-api 5 s extrapolation-refusal budget — Phase A A3 MUST measure ADSBHub aggregate end-to-end age against it, gating node cutover (plan §0 req 2, Q7).*

### [MUST] precision-eligibility (core §7 (spec §14.2) + test vector 15)

usable_for_precision_association computed by the emitting process; true only when FRESH + validation passed + time_basis RECEIVER_OBSERVATION/READSB_SEEN_AGE with time_uncertainty_ms ≤ precision_max_time_uncertainty_ms (250) + neither provider_latency_unknown nor provider_surveillance_type_unknown + NACp ≥ precision_min_nac_p (8) when reported + source_class not MLAT/TISB. AC-F-12: ADSBHub-derived positions never set it. Vector 15: ADSBHub-derived position selectable as fallback after §10.5 penalty, but usable_for_precision_association=false with provider_latency_unknown.

*Notes: deferred (flag exists only in the canonical contract, Phase B). Interim consequence that binds Phase A: the served format has no precision flag, so the honest-seen_pos requirement is the only freshness guard — nothing served may imply RF-exact timing or integrity SBS cannot carry.*

### [MUST] streaming-snapshots (core §8 (spec §15))

Subscribe/snapshot-barrier/delta/tombstone semantics, subscription_sequence, backpressure 10,000 msgs / 16 MiB, decimation defaults (full rate within 100 km, 1,000 ms min interval beyond), heartbeat 5 s, chunks ≤500 aircraft or 512 KiB, snapshot_timeout_ms 10,000, SHA-256 manifest.

*Notes: deferred — gRPC distribution is Phase B (retina-adsb-server). Phase A serves stateless HTTP GET responses instead (diverged-by-plan, interim).*

### [MUST] local-agent-api (core §9 (spec §16))

unix:///run/retina/adsb-agent.sock 0660 retina-adsb:retina-solver; GetSnapshot cap 5,000 aircraft; Watch replay ring ≥30,000 ms / 10,000 events; GetAtTime exact/interpolate/extrapolate rules (exact_sample_window_ms 100; interpolation gap ≤2.5 s; extrapolation ≤2.0 s).

*Notes: deferred — node-side (Phase C).*

### [MUST] history-bounds (core §10 (spec §17))

Per aircraft 120 s / ≤512 position samples; node-wide ≤5,000 aircraft, ≤500,000 history samples, agent memory ≤256 MiB; cross_source_duplicate_m 10 m; memory-only, no restart survival.

*Notes: deferred — node agent bounds (Phase C). Server-side Phase A boundedness is governed by AI-08 instead.*

### [MUST] config-rules (core §11 (spec §18.1))

"YAML with config_version; unknown keys fatal; secrets only by file/secret-manager reference; --check-config runs without opening sockets; reloadable values documented; processes expose a redacted config hash and build version; fleet configuration is signed and staged by cohort."

*Notes: satisfied-in-Phase-A for new code (adsbhub-adapter); fleet-signing clause deferred (no fleet). AMBIGUOUS: how far these rules retrofit onto vendored adsblol/api and readsb-hub (adsb.lol lineage uses its own config idioms) — design must decide the wrapper (e.g., generated args verified in integration tests per §18.3) and record it in the ledger.*

### [MUST] feature-flags (core §11 / spec §32)

"Feature flags MUST independently disable: ADSBHub ingest; contribution per node/cohort; central consumption per cohort; interpolation/extrapolation; optional ancillary groups; any new protocol-minor feature."

*Notes: partial-in-Phase-A: the ADSBHub-ingest disable flag applies now (adapter must be stoppable independently); the remaining five flags are deferred with their components.*

### [MUST] node-certificates (core §11 (spec §21.1–21.2))

Unique per-node client certificates (never shared by fleet image), SAN carries node_id; ATECC608B secure-element keys where present; certificate lifetime 30–90 days; renewal before 50% consumed; revocation propagates ≤15 minutes.

*Notes: deferred — node identity/PKI is Phase B/C. Phase A's interim serving auth boundary is open question Q5 (mTLS like the eventual contract, or private network + firewall); whichever is chosen must satisfy AC-S-01.*

### [MUST] input-resource-protection (core §11 (spec §21.3))

"Size limits enforced before parsing; per-node token buckets on bytes and frames; global connection/memory caps; parser fuzzing is a release gate; logs escape untrusted values and never contain keys, tokens, or full frames; aircraft key, callsign, certificate subject, and unrestricted node id MUST NOT be metric labels (§22)."

*Notes: satisfied-in-Phase-A (must implement): SBS parser fuzzing is a release gate regardless of implementation language (plan A1, §4 risk note); serving tier gets per-node rate limits sized for ~1 Hz polling per consumer (plan §0 req 5) plus size caps; metric-label prohibition binds the adapter and serving-tier metrics.*

### [MUST] compliance-gate (core §11 (spec §4.1))

"SBOM and license/redistribution review before production rollout; ADSBHub terms rechecked before launch and at least annually; the designated ADSBHub contribution station documented (AI-04)." ADSBHub feed: data.adsbhub.org:5002, restricted to configured source IPs, requires contributing at least one station, may be processed/published/used commercially; accuracy disclaimed — "RETINA MUST treat it as untrusted sensor data." A coverage comparison against at least one alternative aggregator SHOULD be recorded before launch.

*Notes: satisfied-in-Phase-A: plan A0 covers license review of vendored adsblol/infra + adsblol/api ('§4.1 SBOM gate applies to them like any dependency'), terms recheck, and station documentation. The droplet's static IP must be whitelisted on the ADSBHub profile page (data-access IP is configured separately from the feeding station's IP — confirmed 2026-08-26); it is the 'stable egress IP' of §6.1 S1; use a reserved/floating IP or update the whitelist before any rebuild/move. Coverage comparison is SHOULD, feeds A3's independent-view check.*

### [MUST] data-governance (core §11 (spec §21.5))

"RETINA MUST define retention, access, and disclosure policy for ADS-B data."

*Notes: satisfied-in-Phase-A (must be written before production; no plan task currently names it — flag as a gap to add to A0/A3 docs).*

### [MUST] perf-objectives (core §12 (spec §25.2))

Reference load: 5,000 central aircraft, 200 node subscriptions, 100 contributing feeds, 5 pos updates/s/aircraft burst, ≤400 aircraft in 150 km region, CPU <70%. SLOs include: server accepted update → subscribed node p95 ≤250 ms / p99 ≤500 ms; central reconnect → committed snapshot p95 ≤5 s for 2,000 aircraft; central distribution availability ≥99.9% monthly; "Lost accepted updates due to internal unbounded blocking: 0; overload must be explicit drops/disconnects"; per-node distribution p95 ≤2 Mbps (≈650 GB/month/node).

*Notes: deferred (mostly): distribution SLOs attach to the Phase B gRPC tier. Binding now: the zero-lost-updates/explicit-overload principle (design pattern for adapter relay and serving tier), and spec §25.2's note that provider-to-server latency is reported but NOT an enforceable SLO for ADSBHub — A3 measures it anyway because the node 5 s budget depends on it.*

### [MUST] capacity-tests (core §12 (spec §25.3, §26.6))

"Release tests pass at 2× expected volume for 60 minutes (no memory growth, crash, invalid sequence, or p95 > 2× SLO); a 5× 60-second burst recovers within 30 seconds without replaying stale queues as fresh. Any readsb, parser, or fusion change requires a ≥30-minute replay comparison; rollout is blocked on unexplained loss, stale-age regression, or implausible positions."

*Notes: partial: 2×/5× capacity tests map to Gate 2 (AC-P-05, deferred). The ≥30-minute replay-comparison gate for any readsb or parser change binds Phase A immediately (readsb pin + fixtures + shadow-replay before first upgrade, plan §4 risk item). No-stale-backlog-replay also binds the adapter (spec §33 note 11).*

### [INFO] AC-gates-mapping (core §13 preamble / spec §28)

Acceptance criteria stage into Gate 1 (canary production, Phase 3 — before any production solver consumes the agent API) and Gate 2 (fleet rollout, Phase 4). Phase A ≈ spec Phase 1 (server shadow ingest), whose exit is: "72-hour soak, no unbounded growth, parse error understood, freshness dashboards operational" plus comparison of central state with independent known-good views.

*Notes: No gate formally closes in Phase A; the Phase 1 exit criteria are Phase A's acceptance bar (plan A3). NOTE: spec Phase 1 also runs feed gateway and state server in shadow — Phase A omits both (diverged-by-plan; only adapter + readsb-hub + interim serving tier run).*

### [MUST] AC-F-11 (core §13.1 AC-F-11)

"Node contribution contains only local receiver data and cannot form a feedback loop (including the §4.1 ADSBHub contribution path)." Gate 1.

*Notes: partial-in-Phase-A: the §4.1 ADSBHub-contribution-path half binds now (station 47.46.206.146 feeds directly from its own decoder — verify in A0); node-contribution half deferred. Spec §33 note 6: the aggregate returning a node's own report is expected and harmless when source tables stay separate.*

### [MUST] AC-R-05a (core §13.2 AC-R-05a)

"72-hour server soak and 48-hour node soak show no unbounded memory, queue, disk, or descriptor growth." Gate 1.

*Notes: partial-in-Phase-A: the 72-hour server soak is Phase A A3's shadow soak (also the §28 Phase 1 exit); the 48-hour node soak is deferred. AC-R-05b (7-day node with fault cycling; 24 h server at 2×) is Gate 2, deferred.*

### [MUST] AC-S-01 (core §13.4 AC-S-01)

"Public scan exposes only approved TLS endpoints; no raw readsb, metrics, admin, or node-local ports." Gate 1.

*Notes: satisfied-in-Phase-A (must implement, adapted): this is the criterion the plan cites forbidding unauthenticated public exposure of readsb-derived endpoints (plan A2). Phase A's approved surface = the authenticated interim REST endpoint (Q5: mTLS or network policy) + nothing else; readsb-hub ports 31003/31004/31005 loopback only, metrics 9090+ monitoring network only, tar1090 on authenticated operator network only (spec §6.1 S3: readsb HTTP API/tar1090 MAY be exposed on an authenticated operator network for diagnostics, never as a node/solver contract). Verify with an actual external scan.*

### [MUST] AC-S-04 (core §13.4 AC-S-04)

"Fuzz suites produce no crash, hang, unbounded allocation, or secret disclosure." Gate 1.

*Notes: satisfied-in-Phase-A (must implement): binds the new SBS parser (plan A1: 'Fuzz the SBS parser regardless (§21.3)'); a Phase A release gate independent of language decision Q4.*

### [MUST] AC-S-05 (core §13.4 AC-S-05)

"Images have SBOMs, pass vulnerability policy, run with least privilege, and contain no node private keys." Gate 1.

*Notes: satisfied-in-Phase-A (adapted to systemd/bare processes — no Docker/K8s available): spec §20.1 explicitly allows 'containers or hardened system services'; components MUST have separate users, read-only roots where practical, dropped Linux capabilities, resource limits, restart policies, private networking. SBOM per §4.1/§21.4 (pin commits, non-root, separate build/runtime).*

### [MUST] AC-O-01a (core §13.5 AC-O-01a)

"Pipeline, freshness, and node source-selection dashboards (§22.4 items 1–3) are operational." Gate 1.

*Notes: partial-in-Phase-A: items 1 (end-to-end pipeline: provider input → readsb → state) and 2 (freshness: position age percentiles by source) are Phase A A3 deliverables; item 3 (node source selection) deferred. Remaining §22.4 dashboards (4-7) are AC-O-01b, Gate 2.*

### [MUST] AC-O-02 (core §13.5 AC-O-02)

"Each failure in §24 produces the documented status/metric/alert without an alert storm." Gate 1.

*Notes: partial-in-Phase-A: server-relevant §24 rows bind now — ADSBHub TCP disconnect (backoff/reconnect, provider-down alert, reconnect counter); ADSBHub format change (drop+count unknown lines, trip degraded threshold, parse-error alert, sampled schema diagnostic); server readsb crash (supervisor restart, readiness false); malformed local JSON line (drop one, count, continue); clock jump; disk full; memory pressure. Node rows deferred.*

### [MUST] AC-O-03 (core §13.5 AC-O-03)

"Operators can identify whether staleness originates before server ingest, in server processing, network delivery, or node selection when timing information permits." Gate 1.

*Notes: partial-in-Phase-A: before-server-ingest vs server-processing attribution binds now — the adapter's 'apparent provider delay' metric and A3's ADSBHub latency characterization supply it; delivery/node-selection legs deferred.*

### [MUST] AC-O-04 (core §13.5 AC-O-04)

"Build, schema, and redacted config hashes are visible for server and every node." Gate 1.

*Notes: satisfied-in-Phase-A for server processes (adapter, serving tier, readsb-hub pin): expose build version + redacted config hash (§18.1, §22.1 retina_adsb_build_info{version,commit,schema_version}); node half deferred.*

### [MUST] AC-O-05a (core §13.5 AC-O-05a)

"Runbooks exist for provider outage, node local receiver loss, certificate failure, and rollback." Gate 1.

*Notes: partial-in-Phase-A: plan A3 requires the server-only subset of the §32 set — ADSBHub disconnected/no data; ADSBHub parse-error surge after format change; readsb upgrade rollback. Add: accidental public raw-port exposure (server-relevant §32 item). Node receiver loss and certificate runbooks deferred. Each runbook: symptoms, dashboards, safe diagnostics, decision tree, remediation, rollback, verification, escalation.*

### [INFO] deferred-acceptance (core §13)

All remaining acceptance criteria — AC-F-01..AC-F-10, AC-F-12, AC-R-01..AC-R-04, AC-R-06, AC-R-07, AC-P-01..AC-P-07, AC-S-02, AC-S-03, AC-S-06, AC-O-01b, AC-O-05b — attach to the agent/fusion/gRPC/contribution stack.

*Notes: deferred to Phase B/C gates; record in ledger as not-applicable-in-Phase-A rather than silently omitted.*

### [MUST] invariants-test-vectors (core §14)

Property invariants (selected candidate eligible; coordinates from one candidate; effective age never decreases without newer observation; expired local cannot block eligible central; central failure cannot delete eligible local; sequence gap cannot produce partial snapshot commit) + selection truth table + fusion vectors 1–15.

*Notes: deferred — all fusion/selection tests ship with the Phase C engine ('The fusion/selection logic (§13) and its property tests live node-side and come with Phase C', plan §2). Server-relevant Phase A testing instead: SBS parser fuzz corpus, golden readsb fixtures, envelope fixture vs tar1090-node v0.2.0 proxy/server.js:90-129.*

### [MUST] contract-authority (core §4 + core preamble)

"The contract is the Protocol Buffer definition in full-specification §11, incorporated here verbatim and binding, until proto/retina/adsb/v1/adsb.proto exists in the repository." Core preamble: "On conflict, this document governs, and the conflict is a defect to fix in both documents in one change."

*Notes: diverged-by-plan: the interim adsb.lol v2 REST surface is not the §4/§11 contract and the core contains NO interim-serving provision. The core's own conflict rule supplies the treatment: the divergence is a documented defect resolved by a spec amendment in both documents — plan §0 commits to 'a short spec amendment ... (fields served, seen_pos semantics including upstream age, staleness guarantees, sunset) once the serving surface is confirmed'. The design MUST produce that amendment; the interim surface stays minimal and documented as temporary (lock-in risk, plan §4). Phase B commits the proto and transfers contract authority to the repo.*

### [MUST] s1-adapter-duties (spec §6.1 S1 (via plan A1))

adsbhub-adapter: one long-lived outbound TCP to data.adsbhub.org:5002 from an ADSBHub-authorized stable egress IP; exponential backoff with jitter after failure; frame SBS lines with a maximum line length; validate field count, message type, ICAO syntax, numeric ranges, timestamp syntax, UTF-8/ASCII safety; drop+count malformed without terminating; retain valid provider timestamps as diagnostic metadata; expose one bounded loopback-only SBS relay listener for readsb-hub's sbs_in connector; "close the active internal relay connection when the upstream session fails so readsb-hub does not mistake an idle dead provider for a live source"; expose connection state, byte/line counts, parse errors, reconnects, last-good-record time, apparent provider delay; bounded redacted diagnostic capture only when explicitly enabled. "It MUST NOT expose an Internet listener or define canonical downstream schemas."

*Notes: satisfied-in-Phase-A — this is the A1 deliverable, new code (adsb.lol infra has no ADSBHub ingest). Language: Python per adsb.lol lineage unless Q4 hardening review says otherwise; fuzzing gate applies regardless. Relay listens on loopback 31003 only AFTER upstream feed established and validated (spec §18.3).*

### [MUST] readsb-hub-topology (spec §18.3 + §19 (incorporated via plan A1))

"The deployment MUST generate arguments for the pinned build and verify them in integration tests." Expected shape: --net-only --net --net-bind-address=127.0.0.1 --net-ingest --net-receiver-id --net-bi-port=31004 --net-json-port=31005 --net-json-port-interval=0 --net-connector=127.0.0.1,31003,sbs_in. Port plan: 31003 SBS adapter relay, 31004 Beast gateway input, 31005 JSON out — all loopback/private only; metrics 9090+ monitoring network only; port collision detection is an install precondition.

*Notes: satisfied-in-Phase-A: readsb-hub from adsb.lol container lineage configured per §18.3 with the sbs_in connector to the adapter relay, loopback/private binds only (AI-06/§19, plan A1). 31004 unused until Phase B (no gateway) — MAY omit. If process network namespaces differ, use private addresses + firewall policy rather than publishing ports. --net-json-port-interval=0 gives per-position JSON emission (spec §33 note 1: aircraft.json is diagnostics/bootstrap, not the low-latency path — though the interim adsblol/api serving tier reads readsb state, which the amendment must cover).*

### [MUST] stream-health-thresholds (spec §14.4 (incorporated by core §15))

"ADSBHub no valid record: degraded 10 s, unhealthy 30 s. Central readsb no JSON positions while input is nonzero: degraded 5 s, unhealthy 15 s." Health checks MUST distinguish an active empty feed from a dead connection using bytes/heartbeats/decoder statistics, never aircraft count alone; "no aircraft" at a quiet receiver is not an error.

*Notes: satisfied-in-Phase-A: plan A1 requires exactly rows 1–2 for the provider path. Rows 3–5 (node sidecar/heartbeat/forwarder) deferred.*

### [MUST] provider-metrics (spec §22.1 (incorporated by core §15))

Required provider metric set: retina_adsb_provider_connected{provider}; retina_adsb_provider_bytes_total{provider}; retina_adsb_provider_records_total{provider,result}; retina_adsb_provider_last_record_timestamp_seconds{provider}; retina_adsb_provider_reconnects_total{provider,reason}; retina_adsb_provider_record_delay_seconds{provider} (histogram). Also server-relevant: retina_adsb_readsb_json_records_total{result}, retina_adsb_readsb_last_position_timestamp_seconds, retina_adsb_aircraft_live, retina_adsb_build_info{version,commit,schema_version}. Prometheus base-unit/_total conventions; no high-cardinality labels; aircraft address/callsign never labels.

*Notes: satisfied-in-Phase-A: the provider set is the A1 adapter metrics contract ('§22.1 provider set'). readsb_json/aircraft_live metrics normatively belong to retina-adsb-server (absent in Phase A) — AMBIGUOUS which Phase A process emits hub-state metrics (serving tier or a small exporter); design must assign. gRPC/subscription metrics deferred; interim REST serving-tier HTTP metric names are unspecified by the spec — design must define equivalents (requests_total, duration histogram) following the same conventions.*

### [MUST] structured-logs (spec §22.3 (incorporated by core §15))

Structured JSON logs with: timestamp, severity, service, version, host/node_id, environment, event, message, trace_id (when present), connection_id (when relevant), source_class, error_code, retry_in_ms. Aircraft key only in debug/targeted sampling; private keys, certificate contents, raw tokens, full untrusted frames MUST NOT be logged.

*Notes: satisfied-in-Phase-A (must implement) for adapter and serving tier.*

### [MUST] alerts (spec §22.5 (incorporated by core §15; via plan A3))

Server-relevant alerts with default conditions: ADSBHubDown = disconnected or no valid records for 30 s; CentralStateStalled = input bytes increase but no state update for 15 s; ClockOffset = >100 ms/5 min warning, >500 ms/2 min critical; MemoryPressure = >85% limit for 5 min or history eviction surge; parse-error surge (ADSBHub format change → parse-error alert per §24).

*Notes: satisfied-in-Phase-A: plan A3 names ADSBHubDown, CentralStateStalled, parse-error surge. DistributionUnavailable/FreshnessRegression/node alerts deferred. AC-O-02 requires no alert storms.*

### [MUST] health-readiness (spec §23 (incorporated by core §15))

Every process exposes liveness (event loop/parser responsive, independent of upstream health), readiness (safe to serve declared role), and authenticated/redacted detailed status. "adsbhub-adapter may be live but not ready to be considered a healthy provider while disconnected." "Empty airspace is not a liveness failure."

*Notes: satisfied-in-Phase-A (must implement) for adapter, readsb-hub (via supervisor + stats), and serving tier (ready only after it can reach readsb-hub state).*

### [MUST] failure-behaviors (spec §24 (incorporated by core §15))

Server-relevant required behaviors: ADSBHub TCP disconnect → backoff/reconnect, no server crash; ADSBHub format change → drop unknown/malformed lines, trip degraded threshold; server readsb crash → supervisor restarts, state ages/expires; local JSON line malformed → drop one line, count, continue; clock jump → monotonic ages nondecreasing; disk full → in-memory operation continues, logs rate-limit, no raw unbounded writes; memory pressure → evict, never unbound queues. "Retries MUST use exponential backoff with full or equal jitter. Default progression: 250 ms, 500 ms, 1 s, 2 s, 4 s ... capped at ... 60 s for ADSBHub. A successful stable connection of 60 seconds resets the backoff."

*Notes: satisfied-in-Phase-A (must implement); each row's operator signal feeds AC-O-02. Spec §33 note 11 additionally: reconnect must never replay a deep backlog as current data.*

### [MUST] testing-ci (spec §26, §27 (incorporated by core §15))

Test program: unit/property coverage, golden fixtures, contract tests, integration scenarios, fault injection, replay/shadow procedure, soak durations, security tests; CI/CD quality gates.

*Notes: partial-in-Phase-A: binds new adapter code + serving tier (fuzzing AC-S-04, golden fixtures §12.5, envelope fixture vs node proxy, 72 h soak §26.7/§28); property/contract/proto CI gates deferred with their components.*

### [MUST] adrs (spec §31 (incorporated by core §15))

Required ADRs before code freeze, including ADR 2 (readsb pin) and ADR 13 (region radius and decimation tiers from the solver detection envelope).

*Notes: partial-in-Phase-A: ADR 2 (pin the readsb build, verify WITH_UUIDS, fixtures, quantization measurement) is a plan A0 task. ADR 13 and others attach to deferred components. AMBIGUOUS: full ADR list lives in spec §31 (not re-read here); design should enumerate which ADRs Phase A code freeze needs.*

### [MUST] interim-honest-seen-pos (plan §0 req 1 (derives core §3/§10.5))

"seen_pos is the node's only freshness signal ... The served seen_pos MUST therefore include ADSBHub upstream/ingest latency (the §10.5 penalty in interim clothing), or aged aggregate data will silently degrade truth association." blah2-api reconstructs observation time as wall clock − seen_pos with hard |dt| > 5 s extrapolation refusal and no staleness cutoff on the live association path.

*Notes: diverged-by-plan (interim translation of core §10.5) — top design problem for A2: stock adsblol/api serves readsb's seen_pos, which excludes upstream latency. Mechanism must be designed (additive measured/configured penalty, or validated SBS provider-timestamp delta) and documented in the spec amendment.*

### [MUST] interim-envelope (plan §0 req 4 + A2)

Serve GET /v2/lat/{lat}/lon/{lon}/dist/{radius} matching the adsb.lol v2 envelope the node proxy maps from: ac array, ms-scale now, "ground" string for alt_baro. Verify against tar1090-node v0.2.0 proxy/server.js:90-129 with a recorded fixture. Radius unit is the adsb.lol convention (nmi in that API — verify against the proxy fixture), distinct from the canonical API's meters (core §2).

*Notes: diverged-by-plan (interim contract): reusing adsblol/api should give the envelope for free but MUST be fixture-verified. alt_geom will be absent for SBS-derived aircraft (blah2-api falls back to alt_baro — fine; dormant adsb2dd path hard-drops aircraft lacking alt_geom/flight — blocker only if revived). AMBIGUOUS: exact radius unit of the v2 route not stated in plan/core — resolve from adsblol/api source + proxy fixture.*

### [MUST] interim-latency-gate (plan §0 req 2 / A3 / Q7)

Measure ADSBHub aggregate end-to-end age in A3 against a receiver visible both directly and through ADSBHub; compare to the node's 5 s extrapolation budget. "Gate any node cutover on this."

*Notes: satisfied-in-Phase-A (A3 task): if latency routinely approaches/exceeds 5 s, the central-fallback value proposition needs re-examination before any node points at the service.*

### [MUST] interim-rate-limits (plan §0 req 5 / A2)

Per-node rate limiting sized for ~1 Hz polling per consumer; size caps; bounded everything (AI-08) on whatever is exposed.

*Notes: satisfied-in-Phase-A (must implement) — interim expression of §21.3 token buckets.*

### [MAY] tar1090-operator (spec §6.1 S3 (via plan A2))

"The built-in readsb geographic HTTP API and tar1090 output MAY be exposed on an authenticated operator network for diagnostics. They are not a supported RETINA node/solver contract and no production node may depend on their field stability or refresh interval."

*Notes: satisfied-in-Phase-A: tar1090 for operator visualization explicitly allowed (plan A2) — authenticated operator network only, never public, never a node contract. The no-node-dependence clause is the very norm the interim serving diverges from, reinforcing the amendment requirement.*

### [MUST] sbs-timestamp-caution (spec §33 notes 2, 5, 11 (implementation cautions))

Note 2: seen MUST NOT substitute for seen_pos. Note 5: "ADSBHub SBS timestamps may reflect upstream receiver clocks and timezone behavior. Keep the raw parsed value as diagnostics but default freshness to conservative receipt-based time until validated." Note 11: "Do not let reconnect replay a deep backlog as current. Old updates age out or are discarded before live selection."

*Notes: satisfied-in-Phase-A (must implement in adapter): provider timestamps are diagnostics-only until A3 validation; freshness defaults to receipt-based; reconnects never replay stale backlog as live.*

## Key facts

- Document set: /root/adsb-service/RETINA_ADSB_NORMATIVE_CORE.md (429 lines, v1.3, read in full — the '~950 lines' in the tasking overstates it) is the binding acceptance surface; /root/adsb-service/RETINA_ADSB_IMPLEMENTATION_SPEC.md (2,206 lines, v1.3, versions in lockstep) carries §n targets; on conflict the core governs and the conflict is a defect fixed in both docs in one change. §n references that supply parameters (core §15 list) are incorporated by reference and equally binding.
- Core §15 incorporated-by-reference sections and Phase A applicability: §11 proto (deferred, Phase B commits proto/retina/adsb/v1/adsb.proto); §14.4 stream-health thresholds (APPLIES: ADSBHub no valid record 10 s degraded / 30 s unhealthy; readsb no JSON positions while input nonzero 5 s / 15 s); §20 deployment isolation (APPLIES adapted to systemd hardened services — spec §20.1 allows 'containers or hardened system services': separate users, read-only roots where practical, dropped capabilities, resource limits, restart policies, private networking); §22 metrics/logs/dashboards/alerts (APPLIES: provider metric set, structured log fields, dashboards 1-2, alerts ADSBHubDown/CentralStateStalled/ClockOffset/parse-error); §23 health semantics (APPLIES: adapter live-but-not-ready while disconnected; empty airspace never a liveness failure); §24 failure behaviors (APPLIES: server rows + backoff 250 ms→60 s cap ADSBHub, 60 s stable resets); §26 test program (parser fuzzing, golden fixtures, 30-min replay, 72 h soak APPLY); §27 CI gates (apply to new code); §28 rollout (Phase A ≈ Phase 1 shadow ingest; exit = 72-hour soak, no unbounded growth, parse errors understood, freshness dashboards operational); §31 ADRs (ADR 2 readsb pin applies now); §32 runbooks/feature flags (server subset applies: ADSBHub disconnected, parse-error surge, readsb upgrade rollback, accidental public raw-port exposure; ADSBHub-ingest disable flag).
- Phase A component set = adsbhub-adapter (new Python code per spec §6.1 S1) + readsb-hub (adsb.lol lineage, spec §18.3 arg shape: --net-only, --net-bind-address=127.0.0.1, --net-ingest, --net-receiver-id, --net-bi-port=31004, --net-json-port=31005, --net-json-port-interval=0, --net-connector=127.0.0.1,31003,sbs_in) + adsblol/api serving GET /v2/lat/{lat}/lon/{lon}/dist/{radius} + tar1090 (operator-only) + observability. NOT built in Phase A: retina-feed-gateway, retina-adsb-server, proto contract, node sidecar/forwarder/agent, fusion engine — spec Phase 1 nominally includes gateway+state server in shadow, so Phase A is a narrower slice even of Phase 1.
- Contract divergence treatment: core §4 makes the §11 proto the only contract and has no interim-REST provision; the plan's adsb.lol v2 interim serving is a recorded divergence resolved through the core's own conflict rule — a short spec amendment (fields served, seen_pos semantics including upstream age, staleness guarantees, sunset) must be written once the serving surface is confirmed. During Phase A no node consumes the endpoint (cutover gated on Q7 latency measurement + soak), so divergence materializes only at first node cutover.
- Interim serving hard requirements derived from actual node behavior (plan §0): (1) served seen_pos MUST include ADSBHub upstream/ingest latency — the §10.5 penalty (default 1,000 ms provider_age_penalty_ms in canonical form) 'in interim clothing' — because blah2-api computes observation time as wall clock − seen_pos with a hard |dt| > 5 s extrapolation refusal and no staleness cutoff; (2) A3 must measure ADSBHub aggregate latency vs the 5 s budget and gate node cutover on it; (3) alt_geom absent for SBS aircraft (SBS carries no geometric altitude); (4) envelope must match adsb.lol v2 (ac array, ms-scale now, 'ground' string for alt_baro), fixture-verified against tar1090-node v0.2.0 proxy/server.js:90-129; (5) per-node rate limits sized for ~1 Hz polling.
- ADSBHub externals: feed at data.adsbhub.org:5002, TCP port opened only for IP addresses configured on the profile page (data-access IP separate from feeding-station IP, confirmed 2026-08-26); entitlement requires contributing ≥1 station — designated contribution station is the receiver at static IP 47.46.206.146 feeding ADSBHub directly from its own decoder (AI-04, must be documented); droplet static IP = the 'stable egress IP' of §6.1 S1 (reserved/floating IP recommended); terms rechecked before launch and at least annually; feed treated as untrusted sensor data; SBS carries no NIC/NACp/SIL and cannot distinguish direct ADS-B from MLAT/TIS-B.
- Exact SBS validation numbers for the adapter: lat [-90,90]; lon [-180,180] with +180→−180 normalization; altitude [-2,000,100,000] ft; ground speed [0,1,500] kt; track mod 360; vertical rate [-20,000,20,000] ft/min; callsign ≤16 UTF-8 bytes right-trimmed; squawk exactly 4 octal digits; max line length enforced before allocation; only explicitly tested BaseStation variants parsed, unknown types counted+ignored; CSV empty-field handling, no naive splitting if quoting possible; host/port/parsing behavior as configuration, never compiled constants; provider timestamps configured TZ (default UTC), diagnostics-only until validated; parser fuzzing is a release gate (AC-S-04) regardless of the Q4 language decision.
- Provider metrics contract (spec §22.1 provider set, binds A1): retina_adsb_provider_connected{provider}, _bytes_total{provider}, _records_total{provider,result}, _last_record_timestamp_seconds{provider}, _reconnects_total{provider,reason}, _record_delay_seconds{provider} histogram; plus build_info{version,commit,schema_version}; aircraft key/callsign/cert subject/unrestricted node id never metric labels; Prometheus base units and _total conventions.
- Phase A acceptance bar: spec §28 Phase 1 exit (72-hour soak with no unbounded growth, parse errors understood, coverage compared against an independent view, freshness dashboards operational) — no core Gate 1/Gate 2 closes in Phase A (Gate 1 = Phase 3 canary, Gate 2 = Phase 4 fleet). Server-applicable acceptance rows now: AC-S-01 (public scan: only the authenticated interim endpoint visible; readsb 31003/31004/31005 loopback, metrics 9090+ monitoring-only), AC-S-04 (fuzz), AC-S-05 (SBOM/least-privilege adapted to systemd), AC-R-05a (72 h server soak half), AC-O-01a items 1-2, AC-O-02 server rows, AC-O-03 (ingest vs processing attribution), AC-O-04 (build+redacted-config hashes), AC-O-05a server runbooks, AC-F-11 (§4.1 contribution-path half).
- Open stakeholder questions affecting the design: Q4 adapter language (Python default per adsb.lol lineage; fuzzing gate regardless); Q5 interim serving auth boundary (mTLS vs private network + firewall — AC-S-01 binds either way); Q6 soak timeline; Q7 measured ADSBHub latency vs 5 s node budget (gates cutover). Environment constraint from tasking: single host, systemd + bare processes, no Docker/Kubernetes — spec §20.1 permits this ('hardened system services') provided per-component users/limits/isolation.

## Risks / ambiguities

- Honest-seen_pos mechanism is undesigned: readsb's seen_pos counts from hub receipt, excluding ADSBHub upstream latency; stock adsblol/api cannot satisfy plan §0 req 1 unmodified. Options (fixed configured penalty per §10.5's 1,000 ms default, measured A3 offset, or validated SBS provider-timestamp delta) have materially different accuracy; spec §33 note 5 mandates conservative receipt-based freshness until provider timestamps are validated. This is the single largest A2 design decision and must appear in the spec amendment.
- If measured ADSBHub aggregate latency routinely approaches/exceeds 5 s, honest seen_pos + blah2-api's |dt| > 5 s refusal means central-fallback data would be largely unusable for truth association — the central-fallback value proposition fails and node cutover is blocked (Q7). Measure early in A3.
- Served Phase A data bypasses canonical safeguards: no §12.3 plausibility/jump rejection, no §10.5 penalties, no precision-eligibility flag, no §7 TTL/EXPIRED enforcement — nodes get readsb-hub state as-is. Acceptable only while the surface is documented interim with a sunset; adsb.lol-format lock-in (solvers depending on interim fields/latency) makes §11-contract migration harder (plan §4 risk).
- Ambiguity — how far spec §18.1 config rules (config_version YAML, unknown-keys-fatal, --check-config, redacted config hash) and §22.1 metric names retrofit onto vendored adsblol/api and readsb-hub: the spec wrote them for RETINA-authored components. Design must state the boundary (e.g., generated verified args per §18.3 for readsb-hub; wrapper config for the API tier) or the conformance ledger will contain untestable rows. Also unassigned: which Phase A process emits retina_adsb_readsb_json_records_total / retina_adsb_aircraft_live (normatively retina-adsb-server's, which does not exist yet).
- Ambiguity — interim v2 route radius unit (adsb.lol uses nautical miles; canonical API uses meters, core §2): must be pinned by the recorded proxy fixture, else node cutover silently changes query geometry. Related: deployed tar1090-node v0.2.0 proxy sends no User-Agent and has no cache (caused an adsb.lol 403 fleet outage) — next node release should ship the HEAD proxy before pointing at this service.
- AC-S-01/AI-06 exposure of the interim endpoint depends on unresolved Q5 (mTLS vs network policy); until decided, the design cannot finalize the edge (TLS termination, cert issuance for node clients, firewall rules). The spec forbids unauthenticated public exposure of readsb-derived endpoints in every reading.
- Single-host systemd deployment must reproduce §20.1 isolation without containers (separate users, dropped capabilities, resource limits, private networking between adapter relay 31003 / readsb 31005 / API tier); accidental public raw-port exposure has a dedicated §32 runbook — include an external port-scan check in A3.
- §21.5 retention/access/disclosure policy for ADS-B data is a MUST with no owning task in the plan — add to A0/A3 documentation scope. Likewise the §4.1 SHOULD coverage comparison against an alternative aggregator before launch.
- Q4 language risk: Python SBS parser is untrusted-input surface; §21.3 fuzzing is a release gate regardless, and budgeting for it (corpus, harness, CI time) is explicitly called out in plan §4.
- Spec Phase 1 nominally includes feed gateway and state server in shadow; Phase A omits them, so '≈ spec Phase 1 exit' in the plan is an approximation — the conformance ledger should record the Phase 1 component-set delta explicitly to keep acceptance review honest.
