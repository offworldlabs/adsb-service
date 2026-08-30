# Phase A requirements — ingest path (spec §6.1/§10/§12/§14/§18/§24)

> Machine-extracted from the binding docs by reader agents (2026-08-29).
> On any doubt, the spec/core text governs — verify against the source section.

### [MUST] s1-upstream-conn (spec §6.1 S1 (line 190))

Maintain ONE long-lived outbound TCP connection to `data.adsbhub.org:5002` from an ADSBHub-authorized stable egress IP (plan.md A0: the droplet's whitelisted static IP; contribution station is 47.46.206.146 but the data-access IP is whitelisted separately on the ADSBHub profile page).

### [MUST] s1-backoff (spec §6.1 S1 (line 191) + §24 (line 1714))

Use exponential backoff with FULL or EQUAL jitter after failure. Default progression: 250 ms, 500 ms, 1 s, 2 s, 4 s … capped at 60 s for ADSBHub (30 s cap applies only to node distribution, deferred). A successful stable connection of 60 seconds resets the backoff.

*Notes: CONFLICT: §18.2 example sets adsbhub.reconnect.initial: 1s, maximum: 60s, jitter_ratio: 0.20, while §24 default progression starts at 250 ms. See risks.*

### [MUST] s1-line-framing (spec §6.1 S1 (line 192) + §18.2 (line 1251))

Frame SBS/BaseStation lines with a maximum line length; config key `line_max_bytes: 4096`. Size limit enforced BEFORE allocation/parsing (§12.1 line 831, §21.3 line 1534).

### [MUST] s1-validate (spec §6.1 S1 (line 193))

Validate: field count, message type, ICAO syntax, numeric ranges, timestamp syntax, and UTF-8/ASCII safety.

### [MUST] s1-drop-count (spec §6.1 S1 (line 194))

Drop and count malformed input without terminating the process (also §12.1 line 837: never terminate the stream because one record is malformed; §24 'Local JSON line malformed': drop one line, count, continue).

### [MUST] s1-provider-ts (spec §6.1 S1 (line 195))

Retain provider timestamps as diagnostic metadata when valid (they are diagnostic only, not canonical time — see §12.4 sanity-check rule and §10.5 penalties).

### [MUST] s1-relay (spec §6.1 S1 (line 196) + §18.3 (line 1320))

Expose exactly one BOUNDED, LOOPBACK-ONLY SBS relay listener on 127.0.0.1:31003; readsb-hub connects to it as an SBS client via `--net-connector=127.0.0.1,31003,sbs_in`. The adapter listens on 31003 only AFTER it has established and validated the upstream feed.

### [MUST] s1-close-relay (spec §6.1 S1 (line 197))

Close the active internal relay connection when the upstream session fails, so readsb-hub does not mistake an idle dead provider for a live source.

### [MUST] s1-expose-state (spec §6.1 S1 (line 198) + §22.1 (lines 1577-1582))

Expose: connection state, byte/line counts, parse errors, reconnects, last-good-record time, and apparent provider delay. Concrete metric names (§22.1): retina_adsb_provider_connected{provider}, retina_adsb_provider_bytes_total{provider}, retina_adsb_provider_records_total{provider,result}, retina_adsb_provider_last_record_timestamp_seconds{provider}, retina_adsb_provider_reconnects_total{provider,reason}, retina_adsb_provider_record_delay_seconds{provider} (histogram).

### [MUST] s1-diag-capture (spec §6.1 S1 (line 199))

Support capture of a bounded, redacted diagnostic sample ONLY when explicitly enabled (see also §21.5: diagnostic captures require operator reason, duration limit, access audit, deletion schedule).

### [MUST] s1-no-internet (spec §6.1 S1 (line 201))

The adapter MUST NOT expose an Internet listener and MUST NOT define canonical downstream schemas. ADSBHub host, port, and parsing behavior MUST be configuration, not compiled constants.

### [MUST] t-quantization (spec §10.4 (line 486))

Measure and record the timestamp resolution of the pinned readsb JSON output as `source_timestamp_quantization_ms` (now/seen_pos family is commonly 0.1 s granular). `time_uncertainty_ms` for TIME_BASIS_READSB_SEEN_AGE groups MUST be >= this quantization, and every constant bounded below by it (`exact_sample_window_ms` §16.4, `same_source_reorder_tolerance_ms` §13.6) MUST be validated against the measured value in CI.

*Notes: Phase A action = A0 checklist item: pin readsb build, capture golden JSON fixtures, measure quantization. The dependent constants live in deferred Phase B/C components but the measurement is Phase A.*

### [MUST] t-seen-pos (spec §10.4 (lines 479-482))

observed_at = json.now − json.seen_pos. Reject negative seen_pos, clamp tiny negative floating-point artifacts to zero, reject values above the configured hard maximum. `seen_pos` is distinct from `seen`: a recent non-position message MUST NOT refresh position time.

*Notes: Binds the serving tier's honest per-aircraft age (plan.md §0 req.1) even though the canonical consumer (retina-adsb-server) is deferred.*

### [MUST] t-provider-penalty (spec §10.5 (line 499))

ADSBHub upstream latency may be unknowable: central records derived from it MUST carry `upstream_latency_known=false`. The server MUST apply `provider_age_penalty_ms` (default 1,000 ms, included in age_ms_at_emit) and `ingest_uncertainty_floor_ms` (default 1,000 ms, applied to time_uncertainty_ms) to every group whose timing derives from TIME_BASIS_INGEST_RECEIPT or that cannot be attributed to a receiver with known timing. Defaults stand until replaced by Phase 5 measured characterization. Independently of the arithmetic, a group carrying `provider_latency_unknown` MUST NOT be classed FRESH (§14.1) and is never precision-eligible (§14.2).

*Notes: Config keys in §18.2: provider_age_penalty: 1s, ingest_uncertainty_floor: 1s. Phase A interim translation (plan.md §0 req.1): served `seen_pos` must include ADSBHub upstream/ingest latency — the §10.5 penalty in interim clothing. Node-side values (Probe every 30 s, unknown_network_penalty_ms default 250 ms, max_network_penalty 2s) are Phase B/C.*

### [MUST] v-general (spec §12.1 (lines 828-837))

At every untrusted-input boundary: reject NaN and infinity; enforce max message/line/frame size before allocation; bound collection counts and string lengths; normalize callsign by trimming right padding but preserve internal characters; lowercase aircraft keys and receiver UUIDs; validate UTF-8, replace or reject control characters; retain an error-reason counter WITHOUT logging full attacker-controlled payloads; never terminate the stream because one aircraft record is malformed.

*Notes: Beyond the enumerated ask but directly binding on SBS ingest — §12 general rules apply to the adapter's untrusted boundary.*

### [MUST] v-ranges (spec §12.2 (lines 841-851))

Range checks: latitude [-90, 90] reject outside; longitude [-180, 180], normalize +180 to -180 consistently; baro/geom altitude [-2,000, 100,000] ft configurable, reject outside; ground speed [0, 1,500] kt reject outside; track/heading normalize finite value modulo 360, reject extreme parser overflow; vertical rate [-20,000, 20,000] ft/min reject outside; callsign max 16 UTF-8 bytes after trim (v1 SHOULD restrict to expected printable subset); squawk exactly four octal digits; NIC/NAC/SIL only values supported by canonical enum/range, unknown is ABSENT not zero-by-default.

*Notes: Applies to the adapter's 'numeric ranges' validation duty (§6.1 S1) for SBS fields.*

### [MUST] sbs-variants (spec §12.4 (line 870))

The ADSBHub adapter MUST support ONLY explicitly tested BaseStation message variants. Unknown message types are counted and ignored until a fixture and mapping are added.

*Notes: Spec does not enumerate which MSG subtypes are in the tested set — the design must choose and back each with a golden fixture (§26.2: representative ADSBHub SBS lines for each supported transmission subtype).*

### [MUST] sbs-csv (spec §12.4 (line 870))

CSV parsing MUST correctly handle empty fields and MUST NOT use naive string splitting if quoting is possible in the selected format.

### [MUST] sbs-timestamps (spec §12.4 (line 870))

Provider timestamps are parsed in a configured timezone (default UTC; config key §18.2 `timestamp_timezone: UTC`) and never trusted without sanity checks.

*Notes: AMBIGUOUS: the sanity checks themselves are not enumerated for SBS provider timestamps; nearest analogues are §12.3 future_tolerance_ms (default 1,000 ms) and position_hard_reject_ms (default 30,000 ms) — design must specify.*

### [MUST] sbs-quality (spec §12.4 (line 872))

SBS/BaseStation carries no NIC/NACp/SIL integrity fields and does not distinguish direct ADS-B from MLAT-/TIS-B-derived positions mixed upstream. ADSBHub-derived groups therefore carry the `provider_surveillance_type_unknown` quality flag, never populate integrity fields, and never satisfy precision eligibility (§14.2).

### [MUST] readsb-mapping-fields (spec §12.5 (lines 876-886))

The readsb mapping layer MUST have golden fixtures for every used field in the pinned readsb version and MUST explicitly support: `hex` (including `~` non-ICAO prefix); `now`, `seen`, `seen_pos`; `lat`, `lon`; numeric altitude AND the string "ground"; `alt_geom`, `gs`, `track`, `baro_rate`, `geom_rate`; `flight`, `squawk`, `emergency`, `category`; `nic`, `nac_p`, `sil`, `sil_type`, `rc`; `type`, `mlat`, `tisb` provenance hints; omitted fields and added unknown fields.

### [MUST] readsb-upgrade-gate (spec §12.5 (line 888))

An upstream readsb upgrade is BLOCKED if golden fixtures or a 30-minute shadow replay show incompatible mapping, parse-error increase, or freshness regression.

### [MUST] health-adsbhub (spec §14.4 row 1 (line 1063))

Health threshold — 'ADSBHub no valid record': degraded at 10 s, unhealthy at 30 s.

### [MUST] health-readsb-json (spec §14.4 row 2 (line 1064))

Health threshold — 'Central readsb no JSON positions while input is nonzero': degraded at 5 s, unhealthy at 15 s.

*Notes: Implies the health checker must correlate input byte counters with JSON position output — quiet-sky safe.*

### [MUST] health-empty-feed (spec §14.4 (line 1069))

No 'no aircraft' condition is itself an error at a quiet receiver. Health checks MUST distinguish an active empty feed from a dead connection using bytes, heartbeats, or decoder statistics.

*Notes: Rows 3-5 of §14.4 (node sidecar / node heartbeat / node feed-forwarder) are node-side, deferred to Phase B/C.*

### [MUST] cfg-rules (spec §18.1 (lines 1233-1239))

Configuration is YAML with explicit `config_version`; unknown keys FATAL by default; secrets/private keys are file or secret-manager references, never inline; every process supports `--check-config` without opening network sockets; reloadable values documented, unsafe changes require restart; running process exposes a redacted configuration hash and build version.

### [SHOULD] cfg-adsbhub-block (spec §18.2 (lines 1246-1258))

adsbhub config block, exact keys/defaults: host: data.adsbhub.org; port: 5002; connect_timeout: 10s; idle_timeout: 30s; line_max_bytes: 4096; timestamp_timezone: UTC; provider_age_penalty: 1s; ingest_uncertainty_floor: 1s; reconnect: {initial: 1s, maximum: 60s, jitter_ratio: 0.20}.

*Notes: §18.2 is labeled 'Server example' — shapes/keys are exemplary, but the embedded values restate normative defaults from §6.1/§10.5/§24. idle_timeout: 30s semantics (declare upstream dead after 30 s silence, matching §14.4 unhealthy) are not spelled out — design must define.*

### [SHOULD] cfg-readsb-client (spec §18.2 (lines 1270-1273))

readsb JSON-consumer config: json_source: 127.0.0.1:31005; reconnect_initial: 250ms; reconnect_maximum: 10s. Telemetry: metrics_listen: 127.0.0.1:9090; log_format: json.

*Notes: Consumer in the spec is retina-adsb-server (deferred); Phase A's serving tier (adsblol/api pattern) plays this role over readsb-hub.*

### [MUST] readsb-hub-args (spec §18.3 (lines 1308-1318))

Expected readsb-hub argument topology, exact: --net-only --net --net-bind-address=127.0.0.1 --net-ingest --net-receiver-id --net-bi-port=31004 --net-json-port=31005 --net-json-port-interval=0 --net-connector=127.0.0.1,31003,sbs_in. The deployment MUST generate arguments for the pinned build and verify them in integration tests.

*Notes: Port map: 31003 = adapter's loopback SBS relay (readsb connects OUT to it as SBS client); 31004 = Beast bi-port for retina-feed-gateway (Phase B, unused in Phase A but harmless to keep private); 31005 = per-position TCP JSON listener consumed by the serving tier. If process/network namespaces differ, use private service addresses + firewall policy, never publish these ports.*

### [MUST] readsb-hub-binds (spec §6.1 S3 (lines 231-233))

readsb-hub MUST bind ALL raw input/output and API ports to a private interface or loopback; NO readsb listener may be published directly to the Internet. Built-in readsb geographic HTTP API and tar1090 output MAY be exposed on an authenticated operator network for diagnostics only — not a supported node/solver contract; no production node may depend on their field stability or refresh interval.

### [MUST] fail-disconnect (spec §24 row 1 (line 1696))

ADSBHub TCP disconnect: backoff/reconnect; node contributions continue; no server crash. Operator signal: provider-down alert + reconnect counter (retina_adsb_provider_reconnects_total{provider,reason}).

### [MUST] fail-format-change (spec §24 row 2 (line 1697))

ADSBHub format change: drop unknown/malformed lines; trip degraded threshold (§14.4 row 1); preserve node feeds. Operator signal: parse-error alert + sampled schema diagnostic.

### [MUST] fail-readsb-crash (spec §24 row 3 (line 1698))

Server readsb crash: supervisor restarts (systemd in this deployment); state ages/expires; streams heartbeat/degrade or reconnect. Operator signal: readiness false + restart alert.

### [MUST] fail-misc-server (spec §24 rows (lines 1703, 1709, 1710))

Local JSON line malformed: drop one line, count, continue (parse counter). Disk full: operational state continues in memory, logs rate-limit/fallback, no raw unbounded writes (disk alert). Memory pressure: evict history/nonregional expired entries, never unbound a queue (eviction/memory alert).

### [MUST] fail-backoff-global (spec §24 (line 1714))

ALL retries MUST use exponential backoff with FULL or EQUAL jitter. Default progression 250 ms, 500 ms, 1 s, 2 s, 4 s … capped at 30 s for node distribution and 60 s for ADSBHub. A successful stable connection of 60 seconds resets the backoff.

### [MUST] fuzz-gate (spec §21.3 (line 1537) + plan.md A1)

Parser fuzzing is a RELEASE GATE — the SBS parser must be fuzzed regardless of implementation language. Also §21.3: max frame/message sizes enforced before parsing; raw network ports remain private; admin and metrics endpoints require network policy, not public ingress.

### [INFO] freshness-classes (spec §14.1 (lines 1010-1015))

Position freshness classes (application defaults, configurable): FRESH 0–1,000 ms; USABLE >1,000–2,000 ms; STALE >2,000–5,000 ms; EXPIRED >5,000 ms (omit live position). Interacts with §10.5: provider_latency_unknown groups can never be FRESH.

*Notes: Included because plan.md §0 req.1-2 pivot on the node's 5 s budget and honest ages; the classes themselves are enforced by deferred components, but the +1,000 ms provider penalty consuming 20% of the EXPIRED budget is a Phase A design fact.*

## Key facts

- Scope authority: /root/adsb-service/plan.md. Phase A = server-side only, single host, systemd + bare processes (no Docker/K8s despite adsblol/infra being container-based — extract its config patterns onto one host). Spec: /root/adsb-service/RETINA_ADSB_IMPLEMENTATION_SPEC.md (2,206 lines, v1.3).
- Data path Phase A: data.adsbhub.org:5002 --SBS--> adsbhub-adapter (validate, count, relay) --loopback 127.0.0.1:31003 SBS relay--> readsb-hub (sbs_in connector, all binds 127.0.0.1) --TCP JSON 127.0.0.1:31005--> serving tier (adsblol/api pattern, GET /v2/lat/{lat}/lon/{lon}/dist/{radius}). Port 31004 (Beast bi-port) is Phase B, keep loopback-bound and unused. Metrics on 127.0.0.1:9090.
- Exact numeric constants for ingest: line_max_bytes 4096; connect_timeout 10s; idle_timeout 30s; backoff progression 250ms→500ms→1s→2s→4s… cap 60s (ADSBHub), full-or-equal jitter, jitter_ratio 0.20 in the config example, reset after 60s stable; degraded/unhealthy 10s/30s (ADSBHub no valid record) and 5s/15s (readsb no JSON positions while input nonzero); provider_age_penalty_ms 1000; ingest_uncertainty_floor_ms 1000; timestamp_timezone UTC.
- Adapter identity/behavior invariants: one upstream connection; loopback relay opens only after upstream validated and MUST close when upstream fails (prevents readsb seeing a live-but-dead source); no Internet listener; host/port/parsing all configuration; drop-and-count malformed, never crash; only explicitly-tested SBS variants (unknown MSG types counted+ignored); CSV parser handles empty fields, no naive split if quoting possible; bounded redacted diagnostic capture opt-in only.
- Provider metrics contract (§22.1): retina_adsb_provider_{connected,bytes_total,records_total{provider,result},last_record_timestamp_seconds,reconnects_total{provider,reason},record_delay_seconds histogram}. Alert (§22.5): ADSBHubDown = disconnected or no valid records for 30 s. Metric labels must never include aircraft key/callsign (§21.6 cardinality).
- ADSBHub data is permanently second-class: carries provider_surveillance_type_unknown + upstream_latency_known=false, no integrity fields, +1,000 ms age penalty and 1,000 ms uncertainty floor, never FRESH, never precision-eligible. Phase A interim translation: served seen_pos must include ADSBHub ingest latency (plan.md §0 req.1) and alt_geom will be absent from SBS-derived aircraft.
- readsb pinning duties (A0): pin build (verify WITH_UUIDS), capture golden JSON fixtures for every §12.5 field (hex incl ~, now/seen/seen_pos, lat/lon, altitude incl string 'ground', alt_geom, gs, track, baro_rate, geom_rate, flight, squawk, emergency, category, nic, nac_p, sil, sil_type, rc, type, mlat, tisb, omitted/unknown fields), measure source_timestamp_quantization_ms (typically 100 ms). Upgrade gate: fixtures + 30-min shadow replay; blocked on incompatible mapping, parse-error increase, or freshness regression.
- systemd substitutes for the spec's 'supervisor' in §24 (readsb crash → restart; readiness false; state ages/expires). Config rules bind every new process: YAML + config_version, unknown keys fatal, --check-config with no sockets, secrets by file reference, redacted config hash + build version exposed.
- Fuzzing the SBS parser is a release gate (§21.3) independent of language choice (Q4: Python per adsb.lol lineage, pending confirmation).
- Deferred (do NOT design into Phase A): retina-feed-gateway (S2), retina-adsb-server (S4), gRPC/proto contract (§11), fusion (§13), node Probe/RTT penalty of §10.5 (unknown_network_penalty 250ms, max 2s, probe 30s), §14.4 rows 3-5, 30s node-distribution backoff cap.

## Risks / ambiguities

- Backoff-initial conflict: §24 default progression starts at 250 ms while the §18.2 config example sets adsbhub.reconnect.initial: 1s (both cap at 60 s). Design must pick one (recommend making it the configurable `reconnect.initial`, default per §18.2, progression doubling per §24) and record the choice.
- §12.4's 'explicitly tested BaseStation variants' set is not enumerated anywhere — the design must define the supported MSG transmission subtypes (ADSBHub emits MSG,1-8) and ship a golden fixture per supported subtype (§26.2) before enabling each.
- SBS provider-timestamp 'sanity checks' (§12.4) are unenumerated; nearest normative analogues are future_tolerance_ms 1,000 ms and position_hard_reject_ms 30,000 ms (§12.3). Design must state the exact bounds under which a provider timestamp is retained as diagnostic metadata vs discarded.
- idle_timeout: 30s semantics for the upstream connection are unspecified (kill-and-reconnect on 30 s silence? aligns with §14.4 unhealthy at 30 s and §22.5 ADSBHubDown). ADSBHub sends aggregate traffic that is rarely silent, but the design must define the action on expiry, and whether TCP keepalive is also used.
- §14.4 row 2 requires correlating readsb input-byte counters with JSON position output ('while input is nonzero') — needs readsb stats scraping or adapter byte counters in the health evaluator; easy to get wrong as a naive 'no positions for 15 s' alert that pages on quiet skies.
- Honest-age requirement (plan.md §0 req.1) has no mechanism specified for Phase A: readsb's seen_pos restarts at ingest, so ADSBHub upstream latency is invisible to it. Whether the serving tier adds a fixed configured penalty (interim §10.5) or measures it (A3 characterization vs the node's hard 5 s extrapolation budget, Q7) is an open design decision that gates node cutover.
- Relay lifecycle vs readsb connector: readsb's --net-connector reconnects on its own schedule; the adapter closing 31003 on upstream failure will cause readsb connector churn/log noise by design — integration tests must assert readsb treats the closed relay as source-down (no stale aircraft aging as live) rather than as an error loop.
- Whitelist coupling: port 5002 opens only for IPs configured on the ADSBHub profile page; if the droplet is rebuilt/moved the whitelist must be updated before cutover (plan.md A0 recommends a reserved/floating IP). A wrong egress IP manifests as connection-refused/timeout indistinguishable from provider-down — worth a distinct runbook entry.
- §18.2 is titled 'Server example': treat keys/shape as the contract to reproduce, but confirm each value against its normative source (§6.1/§10.5/§14.4/§24) rather than copying the block verbatim — one value (reconnect.initial) already conflicts.
