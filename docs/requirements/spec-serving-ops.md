# Phase A requirements — serving, security, observability, testing, rollout

> Machine-extracted from the binding docs by reader agents (2026-08-29).
> On any doubt, the spec/core text governs — verify against the source section.

### [MUST] ports-31003-adapter-relay (spec §19 (line 1406))

Server port 31003, SBS/TCP, exposure 'Loopback/private only', purpose 'Adapter relay; readsb connects with sbs_in'. This is the adsbhub-adapter → readsb-hub relay port for Phase A.

### [MUST] ports-31005-readsb-out (spec §19 (line 1408))

Server port 31005, 'JSON lines/TCP', exposure 'Loopback/private only', purpose 'readsb → state server'. In Phase A the consumer of readsb-hub output is the adsblol/api serving tier instead of retina-adsb-server; the loopback/private bind rule still applies to whatever transport adsblol/api uses against readsb-hub.

*Notes: adsblol/api may consume readsb via a different mechanism (e.g. Redis/aircraft dump); the binding rule is the exposure class, not the specific protocol. Flag for design: pick the actual readsb→api transport and keep it loopback/private.*

### [MUST] ports-9090-metrics (spec §19 (line 1409))

Metrics/health HTTP on '9090+', exposure 'Monitoring network only'. Reinforced by §21.3: 'Admin and metrics endpoints require network policy; they are not exposed on public ingress.'

### [INFO] ports-443-distribution (spec §19 (line 1404))

Port 443 is specified as 'gRPC/HTTP2/TLS' Distribution API for 'Nodes/approved clients'. Phase A serves REST v2 (adsb.lol format) instead of the gRPC contract; the spec assigns no port to a REST serving surface.

*Notes: AMBIGUOUS: design must choose the public serving port (443 behind the edge proxy is the natural analog) and record it; AC-S-01 requires that only approved TLS endpoints appear on a public scan.*

### [INFO] ports-7444-gateway-deferred (spec §19 (line 1405))

Port 7444 (BeastReducePlus over mTLS, contributed feed gateway) and port 31004 (gateway → readsb Beast) serve node contributions — deferred with the feed gateway to Phase B/C per plan.md.

### [MUST] ports-collision-check (spec §19 (line 1416))

'No deployment automation may assume these ports are free; collision detection is an install precondition.'

### [INFO] deploy-single-host-components (spec §20.1)

One server host may run the logical components 'with containers or hardened system services': edge proxy, feed-gateway, adsbhub-adapter, readsb-hub, retina-adsb-server, optional tar1090, OpenTelemetry Collector/metrics agent. Phase A subset: adsbhub-adapter, readsb-hub, serving tier (adsblol/api standing in for retina-adsb-server), optional tar1090, metrics agent, edge proxy; feed-gateway deferred.

*Notes: 'hardened system services' explicitly permits the no-Docker systemd deployment.*

### [MUST] deploy-isolation (spec §20.1)

Components MUST have separate users/containers, read-only roots where practical, dropped Linux capabilities, resource limits, restart policies, and private networking.

*Notes: No Docker available → translate to per-service systemd units: distinct User=/DynamicUser, ProtectSystem=strict/ProtectHome, CapabilityBoundingSet=, MemoryMax/CPUQuota/TasksMax, Restart=, and IPAddressAllow/Deny or nftables.*

### [MUST] deploy-netns-separation (spec §20.1)

'readsb-hub and adapters MUST not share the API's public network namespace unless firewall policy provides equivalent isolation.' On a single host without containers, the firewall-equivalence branch applies: readsb-hub and adsbhub-adapter bind loopback/private only and host firewall blocks external access to 31003/31005/30xxx/readsb net ports.

### [MUST] deploy-scaling-order (spec §20.2)

When one host is insufficient, split in this order: (1) public node-feed ingress replicas; (2) readsb ingest tier and hub tier (ADSB.lol ingest → hub → planes pattern); (3) state/distribution replicas fed by an internal bounded pub/sub stream; (4) regional sharding by geospatial cell; (5) optional durable event capture. 'v1 MUST NOT depend on a broker solely for conceptual purity' — a broker MAY be introduced only when multiple state-service replicas require one ordered update fanout.

*Notes: For Phase A design this is a constraint to keep ingest → hub → serving boundaries separable (plan §0), not to introduce NATS/broker.*

### [MUST] deploy-adsbhub-active-standby (spec §20.3)

'ADSBHub ingestion is active/standby unless the provider explicitly permits multiple simultaneous connections; both instances use the authorized stable egress IP.' Phase A single host = single active connection; never run two concurrent connections to data.adsbhub.org:5002.

### [SHOULD] deploy-mender-note (spec §20.5 (line 1479))

Where the fleet operates the RETINA Mender OTA pipeline, node ADS-B artifacts MUST be delivered and rolled back through it — node-side, so deferred to Phase C. The versioned-symlink layout (/usr/lib/retina-adsb/<version>/ + /usr/lib/retina-adsb/current atomic symlink, /etc/retina/adsb/ config, /var/lib/retina/adsb/ state, /run/retina/) 'applies to hosts without an OTA manager' — the Phase A server host qualifies and SHOULD adopt it for upgrade/rollback of adapter+api+readsb-hub.

### [MUST] deploy-server-rollback (spec §20.6)

'Server rollback retains the current and previous schema-compatible build and switches traffic after readiness verification.'

### [MUST] sec-fuzz-release-gate (spec §21.3 (line 1537) + §26.8 + §29.4 AC-S-04)

'Parser fuzzing is a release gate.' §26.8: 'Parser fuzzing with continuous corpus retention.' AC-S-04 (Gate 1): 'Fuzz suites produce no crash, hang, unbounded allocation, or secret disclosure.' Applies to the Python SBS parser in adsbhub-adapter regardless of language (plan A1: 'Fuzz the SBS parser regardless (§21.3)').

*Notes: Exact requirements = the four failure classes of AC-S-04 plus corpus retention across runs. Malformed/fuzz-regression cases also become versioned golden fixtures (§26.2).*

### [MUST] sec-input-protection (spec §21.3)

Full §21.3 list: (a) 'Maximum frame/message sizes are enforced before parsing'; (b) 'Per-node token buckets limit bytes and decoded frames'; (c) 'Global connection and memory caps prevent exhaustion'; (d) fuzzing gate; (e) 'Logs escape user-controlled values and exclude raw certificate keys/tokens'; (f) 'Raw network ports remain private'; (g) 'Admin and metrics endpoints require network policy; they are not exposed on public ingress.'

*Notes: Server-side Phase A mapping: (a) SBS line-length cap before parse in adapter; (b) per-client rate limit on the v2 REST surface sized for ~1 Hz/consumer (plan A2); (c) connection/memory caps on adapter relay and API.*

### [MUST] sec-no-unauth-public (spec §29.4 AC-S-01 + plan A2)

AC-S-01 (Gate 1): 'Public scan exposes only approved TLS endpoints; no raw readsb, metrics, admin, or node-local ports.' Plan A2 derives: readsb-derived endpoints must not be publicly exposed unauthenticated — minimum mTLS or network policy so only RETINA nodes/operators reach the v2 endpoint (open question Q5).

*Notes: Q5 unresolved (mTLS vs private network + firewall) — orchestrator must pick and record, or block public exposure until decided.*

### [MUST] sec-metric-cardinality (spec §21.6 + §22.1 note + §33.7)

'No aircraft key, callsign, certificate subject, or node ID as unrestricted metric label' (§21.6 threat table); §22.1: node-specific breakdown belongs in authenticated logs or a bounded top-N diagnostics system, not an unbounded node_id Prometheus label.

### [MUST] sec-supply-chain (spec §21.4)

Pin source commits/container digests; generate SBOM and vulnerability scan in CI; verify upstream release signatures/checksums where provided; run as non-root; separate build and runtime images; security updates via canary/shadow validation, not floating tags. Plan A0 applies the SBOM/license gate to the vendored adsblol/infra and adsblol/api code.

### [MUST] sec-privacy-retention (spec §21.5)

'v1 keeps only short in-memory operational history by default.' RETINA MUST define retention, access, and disclosure policy. 'Diagnostic captures require an operator reason, duration limit, access audit, and deletion schedule' — applies to SBS capture files taken for fixtures/replay.

### [MUST] obs-conventions (spec §22 intro)

Metrics follow Prometheus base-unit and _total counter conventions, avoid high-cardinality labels; logs are structured JSON; OpenTelemetry traces for control/API requests; per-aircraft hot-path traces sampled or represented by metrics.

### [MUST] obs-provider-metrics (spec §22.1)

Exact required provider metric set (labels in braces): retina_adsb_provider_connected{provider}; retina_adsb_provider_bytes_total{provider}; retina_adsb_provider_records_total{provider,result}; retina_adsb_provider_last_record_timestamp_seconds{provider}; retina_adsb_provider_reconnects_total{provider,reason}; retina_adsb_provider_record_delay_seconds{provider} (histogram).

*Notes: These are the metrics the adsbhub-adapter must emit (plan A1 '§22.1 provider set'). record_delay_seconds is the instrument for the A3 ADSBHub-latency characterization (Q7 vs the node's 5 s budget).*

### [MUST] obs-server-metrics-phaseA (spec §22.1)

Other §22.1 metrics applicable to the Phase A server: retina_adsb_readsb_json_records_total{result}; retina_adsb_readsb_last_position_timestamp_seconds; retina_adsb_aircraft_live; retina_adsb_positions_total{source_class,result}; retina_adsb_position_age_seconds{source_class} (histogram); retina_adsb_state_events_total{type}; retina_adsb_build_info{version,commit,schema_version}. The retina_adsb_grpc_requests_total / retina_adsb_grpc_request_duration_seconds pair is gRPC-specific; the REST serving tier needs an HTTP analog (e.g. request count/duration by route+code) — spec names none.

*Notes: AMBIGUOUS: no spec-defined metric names for the interim REST surface; design should mirror the grpc pair as retina_adsb_http_* or reuse adsblol/api's existing instrumentation and document the divergence. feed_* and subscription_* metrics belong to deferred gateway/gRPC streaming.*

### [MUST] obs-log-fields (spec §22.3)

Every log record includes: timestamp, severity, service, version, host/node_id, environment, event, message, trace_id (when present), connection_id (when relevant), source_class, error_code, retry_in_ms. Aircraft key only in debug/targeted sampling. 'Private keys, certificate contents, raw tokens, and full untrusted frames MUST NOT be logged' — i.e. never log full raw SBS lines at default level (sampled/truncated schema diagnostics only).

### [MUST] obs-dashboards-1-2 (spec §22.4 items 1–2 + §29.5 AC-O-01a)

Required Phase A dashboards: (1) 'end-to-end pipeline: provider/node input → readsb → state → subscriptions' — Phase A scope: ADSBHub provider → adapter → readsb-hub → serving tier; (2) 'freshness: position age percentiles by source and node cohort' — Phase A scope: position age percentiles by source (no node cohorts yet). AC-O-01a (Gate 1) formally requires items 1–3, but item 3 (node source selection) has no data before Phase C.

*Notes: §28 Phase 1 exit requires 'freshness dashboards operational', so dashboard 2 is a Phase A exit blocker, not just a Gate 1 item.*

### [MUST] obs-alert-adsbhubdown (spec §22.5)

Alert ADSBHubDown, default condition: 'disconnected or no valid records for 30 s'.

### [MUST] obs-alert-centralstatestalled (spec §22.5)

Alert CentralStateStalled, default condition: 'input bytes increase but no state update for 15 s'.

### [MUST] obs-alert-parse-error-surge (spec §22.5 + §24 row 2)

No §22.5 alert is literally named 'parse-error surge'. The malformed-input alert in §22.5 is FeedAuthAttack: 'auth/malformed rate over threshold' (threshold unspecified). §24 row 'ADSBHub format change' independently requires: behavior 'Drop unknown/malformed lines; trip degraded threshold; preserve node feeds', operator signal 'parse-error alert, sampled schema diagnostic'. So Phase A needs a parse-error-rate alert on retina_adsb_provider_records_total{provider="adsbhub",result="error"...} with a locally chosen threshold.

*Notes: AMBIGUOUS: threshold undefined in extracted sections; plan A1 points at '§14.4 rows 1–2' (health/degraded thresholds for the provider path, outside the lines extracted here) — orchestrator should read §14.4 to derive the numeric degraded threshold and reuse it as the alert condition.*

### [SHOULD] obs-alert-memorypressure (spec §22.5)

MemoryPressure alert: '>85% limit for 5 min or history eviction surge' — server-relevant during the 72-hour soak (no-unbounded-growth exit criterion).

### [MUST] health-three-endpoints (spec §23)

Every process exposes: liveness ('process event loop/parser is responsive; does not depend on upstream health'), readiness ('safe to receive new traffic or serve the declared role'), and detailed status ('authenticated/redacted dependency state for operators').

### [MUST] health-adapter-semantics (spec §23)

'adsbhub-adapter may be live but not ready to be considered a healthy provider while disconnected' — liveness stays true through ADSBHub outages; readiness reflects upstream connection + valid-record flow.

### [MUST] health-serving-semantics (spec §23)

'retina-adsb-server is ready only after connecting to readsb, building an initial state (which may be empty), and starting the distribution listener' — adapted to Phase A: the serving tier (adsblol/api) is ready only after its readsb-hub data source is connected/populated (empty airspace allowed) and the HTTP listener is up. 'Empty airspace is not a liveness failure.' The gRPC health.v1 recommendation is N/A for the REST tier (HTTP health endpoints instead).

### [MUST] failure-backoff (spec §24 (line 1714))

Retries MUST use exponential backoff with full or equal jitter; default progression 250 ms, 500 ms, 1 s, 2 s, 4 s … capped at 60 s for ADSBHub (30 s for node distribution); 'A successful stable connection of 60 seconds resets the backoff.' §24 row 1 (ADSBHub TCP disconnect): 'Backoff/reconnect; node contributions continue; no server crash', signals = provider-down alert + reconnect counter. Plan A1 additionally requires close-relay-on-upstream-failure so readsb sees the outage.

*Notes: §24 is outside the section list requested but is directly bound by plan A1 ('backoff+jitter (cap 60 s, §24)') and by the runbook/alert semantics — included for completeness.*

### [MUST] perf-provider-latency-not-slo (spec §25.2 (line 1748))

'Provider-to-server latency is reported but is not an enforceable RETINA SLO for ADSBHub.' It must still be measured (retina_adsb_provider_record_delay_seconds) and, per plan A3/Q7, characterized end-to-end against the node's 5 s extrapolation budget as a gate on any node cutover.

*Notes: Reporting is required; meeting a latency number is not.*

### [SHOULD] perf-availability (spec §25.2)

'Central distribution availability ≥99.9% monthly initially' — the only availability SLO applicable to the Phase A serving tier. 'Lost accepted updates due to internal unbounded blocking: 0; overload must be explicit drops/disconnects' applies to the adapter relay and API (bounded-everything, AI-08 per plan).

*Notes: AMBIGUOUS: no REST request-latency SLO exists for the interim v2 endpoint; the nearest analog is 'Server accepted update → subscribed node p95 ≤250 ms; p99 ≤500 ms' (gRPC push, deferred). Design should declare its own latency target for GET /v2 and note it in the conformance ledger. §25.2 reference-load context: 5,000 tracked aircraft, ≤400 aircraft in a 150 km region, instance CPU <70%.*

### [SHOULD] perf-capacity-headroom (spec §25.3 + §29.3 AC-P-05)

'Release load tests MUST pass at 2× expected aircraft/update volume for 60 minutes without memory growth, process crash, invalid sequence, or p95 latency exceeding 2× its SLO. A 5× burst for 60 seconds must recover within 30 seconds without stale queues being replayed as fresh data.' AC-P-05 stages this at Gate 2 (not Phase A exit), but §26.7's 24 h at 2× follows the Phase A 72 h soak.

*Notes: Phase 1 exit needs only the 72-hour soak; 2× tests gate later phases. 'Stale queues replayed as fresh' also echoes caution §33.11 (reconnect must not replay deep backlog as current) — binding on the adapter's relay buffer design.*

### [MUST] test-unit-server (spec §26.1)

Server-applicable required unit coverage: 'SBS framing/parsing, empty fields, quoted fields, invalid dates, long lines'; 'readsb JSON mapping, omitted/unknown fields, ground altitude'; 'time conversion, negative/future ages, monotonic age behavior'; 'every validation/range rule'; 'configuration validation and secret redaction'. (Selector/fusion/precision/interpolation/snapshot items are Phase B/C.)

### [MUST] test-golden-fixtures (spec §26.2)

Repository MUST contain versioned, license-reviewed fixtures: 'representative ADSBHub SBS lines for each supported transmission subtype'; 'readsb TCP JSON objects from the pinned version'; 'malformed/fuzz-regression cases'. 'Fixtures containing real data MUST be short, access-controlled where necessary, and documented.' (Beast/BeastReducePlus and snapshot/delta stream fixtures are deferred.) Plan A0 ties fixture capture to pinning the readsb build (ADR 2) and measuring source_timestamp_quantization_ms.

### [MUST] test-integration-server (spec §26.4)

Hermetic integration stack, Phase A subset: synthetic SBS provider → adsbhub-adapter → readsb-hub → serving tier (+ fixture-based envelope check against tar1090-node v0.2.0 proxy/server.js:90-129 per plan A2). Server-side verifications applicable now: 'fields expire at exact configured boundaries' and 'slow client isolation'; the fusion/gateway/tombstone checks are deferred with their components.

### [MUST] test-fault-injection (spec §26.5)

Automate (server-applicable): 'dropped, duplicated, reordered, truncated, and delayed frames'; '1%, 10%, and 30% packet loss plus 50–2,000 ms latency'; 'half-open TCP' connections; 'server restart'; 'DNS failure'; 'out-of-memory pressure and full filesystem'; 'malformed/fuzzed provider ... input'; '5× traffic burst'. (Certificate-expiry, HTTP/2, node-clock-jump, slow-solver cases attach to deferred components.)

### [MUST] test-replay-shadow (spec §26.6)

Before each readsb or parser change: (1) 'replay a minimum 30-minute representative capture through old and new builds'; (2) 'compare aircraft count, accepted/dropped updates, coordinates, source choices, and age distributions'; (3) manually inspect statistically significant differences; (4) 'block rollout on unexplained loss, stale-age regression, or implausible positions.' Plan risk list makes this the gate before the first readsb upgrade (§12.5 shadow-replay).

### [MUST] test-soak (spec §26.7 + §29.2 AC-R-05a/b)

'Server: 72 hours at expected load, then 24 hours at 2×.' Pass condition: 'no unbounded memory/file descriptor growth, deadlock, revision regression, or freshness drift.' Gate staging: the 72-hour server soak (with a 48-hour node soak, deferred) gates canary production (Gate 1, AC-R-05a: 'no unbounded memory, queue, disk, or descriptor growth'); the 2× extension is Gate 2 (AC-R-05b). Phase A (plan A3) = the 72-hour shadow soak.

### [MUST] test-security (spec §26.8)

Security tests server-applicable in Phase A: 'Parser fuzzing with continuous corpus retention'; 'Rate-limit and connection-exhaustion tests'; 'Secret/log redaction tests'; 'Container/user/capability and public-port scan' (systemd-unit + host-port scan in the no-Docker deployment); 'Dependency and container vulnerability scan'. TLS downgrade/invalid-chain/wrong-name/expired/revoked cases apply to whatever TLS fronts the v2 endpoint (Q5-dependent).

### [MUST] ci-gates (spec §27)

A change cannot reach production unless: (1) formatting, lint, static analysis, unit tests pass; (2) race/concurrency tests pass where supported; (3) generated Protobuf code clean + compatibility gate [N/A Phase A — no proto committed yet]; (4) integration and required fault tests pass; (5) SBOM and vulnerability policy pass; (6) 'container image is immutable, signed if supported, and tied to a source commit' — no-Docker adaptation: versioned, checksummed release artifact pinned to a source commit; (7) config schema and example validation pass; (8) 'replay comparison is attached for parser/decoder/fusion changes' — applies to SBS-parser and readsb changes; (9) 'canary dashboards and rollback artifact exist'; (10) release notes identify schema/config/operational changes.

*Notes: Gate 3 is the only one fully inapplicable in Phase A. Gate 6's container wording needs an ADR-recorded systemd translation.*

### [MUST] rollout-phase1-exit (spec §28 Phase 1)

Phase 1 'server shadow ingest' activities: run ADSBHub adapter, feed gateway, readsb-hub, and state server 'without production node consumption'; 'connect a few test receivers'; 'compare central tar1090/state with independent known-good views and local captures'; 'tune parser and staleness metrics'. EXACT exit criteria: '72-hour soak, no unbounded growth, parse error understood, freshness dashboards operational.'

*Notes: DIVERGENCE to ledger: plan defers the feed gateway and test-receiver contributions to Phase B/C, so Phase A's coverage comparison uses an independent view (e.g. the station at 47.46.206.146 / adsb.lol) instead of contributed test receivers. Plan A3 adds a non-spec exit item: ADSBHub end-to-end latency characterization vs the node 5 s budget (Q7), gating node cutover.*

### [MUST] rollout-phase0-prereqs (spec §28 Phase 0)

Phase 0 items binding on Phase A server work: 'Confirm stable server egress IP and ADSBHub account/feed entitlement' (plan A0: whitelist the droplet's static IP on the ADSBHub profile page; use a reserved/floating IP so it never changes; contribution station 47.46.206.146 must be actively feeding) and 'create test fixtures'. Node-inventory items deferred.

### [SHOULD] repo-layout (spec §30 (lines 2037–2059))

Recommended layout under retina-adsb/: proto/retina/adsb/v1/, cmd/adsbhub-adapter/, cmd/retina-feed-gateway/, cmd/retina-adsb-server/, cmd/retina-adsb-agent/, cmd/retina-feed-forwarder/, internal/canonical/, internal/fusion/, internal/history/, internal/readsbjson/, internal/sbs/, deploy/server/, deploy/node/, config/schema/, test/fixtures/, test/integration/, test/replay/, dashboards/, runbooks/, docs/adr/. Phase A populates: cmd/adsbhub-adapter/, internal/sbs/, internal/readsbjson/, deploy/server/, config/schema/, test/fixtures/, test/integration/, test/replay/, dashboards/, runbooks/, docs/adr/ (plus the vendored adsblol/api serving code — spec has no slot for it; vendor/ exists in the working repo).

*Notes: Layout is 'recommended' (cmd/internal is Go-flavored; Q4 language decision is Python) — keep directory roles, adapt naming. §30 also: components need not share a language, but shared canonical behavior MUST be driven by common fixtures and contract tests.*

### [MUST] adrs-phase-a (spec §31)

'Before code freeze, the team MUST approve ADRs.' Needed for Phase A: #1 implementation language(s) and shared-library boundaries (resolves Q4: Python vs hardened Go/Rust for the adapter); #2 'exact pinned readsb build and packaging method' (plan A0 explicitly invokes ADR 2); #3 'server container/system-service platform' (records the no-Docker systemd decision); #10 'provider timestamp trust policy and ADSBHub latency penalty' (drives the honest seen_pos requirement and caution §33.5); #11 'licensing/SBOM conclusions' (adsblol/infra + adsblol/api vendoring); #12 'SLO reference hardware and traffic model'. Conditionally: #6 spatial index — only if the serving tier's geo query implementation is chosen rather than inherited from adsblol/api. Deferred with their components: #4 (CA/PKI — unless Q5 resolves to mTLS now), #5, #7, #8, #9, #13.

### [MUST] runbook-structure (spec §32)

'Each runbook contains symptoms, dashboards, safe diagnostics, decision tree, remediation, rollback, verification, and escalation.'

### [MUST] runbooks-phase-a-set (spec §32 + §29.5 AC-O-05a + plan A3)

Phase A required runbooks (plan A3 subset of the §32 required list): (1) 'ADSBHub disconnected/no data'; (2) 'ADSBHub parse-error surge after format change'; (3) 'readsb upgrade rollback'. AC-O-05a (Gate 1) independently requires runbooks for 'provider outage ... and rollback' — covered by (1) and (3). AC-O-05b (full §32 set of 13) is Gate 2.

*Notes: Runbook content anchors: (1) uses ADSBHubDown alert (30 s), backoff cap 60 s, reconnects_total{provider,reason}, close-relay behavior, IP-whitelist check (ADSBHub opens the TCP port only for profile-configured IPs); (2) uses records_total{result} error-rate, drop-don't-crash per §24, sampled schema diagnostic, degraded threshold (§14.4); (3) uses §20.6 retain-previous-build, §26.6 replay comparison, versioned-symlink rollback per §20.5 layout.*

### [MUST] feature-flags (spec §32)

Feature flags MUST permit independent disablement of 'ADSBHub ingest' — the only §32 flag server-side in Phase A (node contribution/central consumption/interpolation flags attach to deferred components).

### [MUST] cautions-server-side (spec §33)

Server-relevant cautions: (1) aircraft.json is diagnostics/bootstrap only, not the low-latency path — --net-json-port emits per-position objects; (2) 'seen is age of any message and MUST NOT substitute for seen_pos' — critical because seen_pos is the node's only freshness signal (plan §0 req.1); (3) readsb data formats change between versions — pin and test; (5) 'ADSBHub SBS timestamps may reflect upstream receiver clocks and timezone behavior. Keep the raw parsed value as diagnostics but default freshness to conservative receipt-based time until validated' — directly shapes the honest seen_pos implementation; (7) no aircraft/node metric labels; (8) 'No aircraft is plausible. Use connection/byte/heartbeat/decoder health rather than aircraft count alone' — shapes readiness + ADSBHubDown; (9) 'Do not equate geometric and barometric altitude' — SBS has no alt_geom, never synthesize it; (11) 'Do not let reconnect replay a deep backlog as current' — bound the adapter relay buffer, discard/age-out backlog on reconnect; (12) keep central update logic replaceable — the interim serving surface must not entangle ADSBHub specifics into the contract.

*Notes: Cautions 4 (Beast timestamps), 6 (own-report loopback), 10 (extrapolation labeling) attach to deferred node/gateway/history components.*

## Key facts

- Spec docs: /root/adsb-service/RETINA_ADSB_IMPLEMENTATION_SPEC.md (2206 lines, sections verified at the stated line numbers); scope authority /root/adsb-service/plan.md; normative core RETINA_ADSB_NORMATIVE_CORE.md and vendor/ also present in the repo.
- Phase A port map: 31003 SBS/TCP loopback/private (adapter → readsb-hub via sbs_in); 31005 JSON-lines/TCP loopback/private (readsb → state/serving); 9090+ HTTP metrics on monitoring network only; public serving port unspecified for REST (spec assigns 443 to the deferred gRPC distribution API); 7444/31004 deferred with the feed gateway; port-collision detection is an install precondition.
- Exact alert conditions: ADSBHubDown = 'disconnected or no valid records for 30 s'; CentralStateStalled = 'input bytes increase but no state update for 15 s'; parse-error surge has no named §22.5 alert — build it from provider_records_total{result} rate with the §14.4 degraded threshold (FeedAuthAttack's 'auth/malformed rate over threshold' has no number); MemoryPressure = '>85% limit for 5 min'.
- Exact provider metric names: retina_adsb_provider_connected{provider}, retina_adsb_provider_bytes_total{provider}, retina_adsb_provider_records_total{provider,result}, retina_adsb_provider_last_record_timestamp_seconds{provider}, retina_adsb_provider_reconnects_total{provider,reason}, retina_adsb_provider_record_delay_seconds{provider} histogram.
- ADSBHub backoff: exponential, full/equal jitter, 250 ms → 500 ms → 1 s → 2 s → 4 s … cap 60 s; 60 s of stable connection resets it; ingestion is active/standby (one connection) from the authorized stable egress IP (droplet IP, whitelisted on the ADSBHub profile page; contribution station 47.46.206.146 must feed independently).
- Phase 1 exit criteria, verbatim: '72-hour soak, no unbounded growth, parse error understood, freshness dashboards operational' (§28); soak pass condition: no unbounded memory/FD growth, deadlock, revision regression, or freshness drift (§26.7); 24 h at 2× and 5×-burst tests are Gate 2, not Phase A.
- Fuzzing gate: parser fuzzing is a release gate (§21.3) with continuous corpus retention (§26.8); pass = no crash, hang, unbounded allocation, or secret disclosure (AC-S-04, Gate 1); applies to the Python SBS parser regardless of language choice.
- Health semantics: liveness never depends on upstream (adapter live-but-not-ready while ADSBHub disconnected); serving tier ready only after readsb connection + initial (possibly empty) state + listener up; empty airspace is never a liveness failure; use connection/byte/heartbeat health, not aircraft count.
- Deployment on the no-Docker host: §20.1 explicitly allows 'hardened system services'; per-component separate users, read-only roots, dropped capabilities, resource limits, restart policies, private networking; readsb-hub/adapter must not share the API's public network exposure unless firewall gives equivalent isolation; §20.5 versioned-symlink layout (/usr/lib/retina-adsb/<version>/ + current symlink) applies to hosts without an OTA manager; server rollback keeps current + previous schema-compatible build (§20.6).
- Provider-to-server latency is reported, not an enforceable SLO (§25.2); but plan A3/Q7 makes measured ADSBHub end-to-end age vs the node's 5 s extrapolation budget a gate on any node cutover; retina_adsb_provider_record_delay_seconds is the instrument.
- Phase A ADR set (§31, MUST approve before code freeze): #1 language (Q4), #2 pinned readsb build+packaging, #3 server platform (systemd), #10 provider timestamp trust / ADSBHub latency penalty, #11 licensing/SBOM for vendored adsblol code, #12 SLO reference model; #6 spatial index only if not inherited from adsblol/api.
- Runbooks for Phase A: ADSBHub disconnected/no data; ADSBHub parse-error surge after format change; readsb upgrade rollback — each with symptoms, dashboards, safe diagnostics, decision tree, remediation, rollback, verification, escalation; plus a feature flag to independently disable ADSBHub ingest.
- Caution §33.5 + plan §0 req.1: default freshness to conservative receipt-based time (raw SBS timestamps kept as diagnostics only) and make served seen_pos include ADSBHub upstream/ingest latency; §33.2: never substitute seen for seen_pos; §33.9: never synthesize alt_geom from SBS; §33.11: bound the relay buffer so reconnects never replay backlog as current.

## Risks / ambiguities

- Parse-error-surge alert threshold is undefined in the extracted sections: §22.5 FeedAuthAttack says only 'over threshold' and §24 says 'trip degraded threshold'. Plan A1 points to §14.4 rows 1–2 (lines outside the requested ranges) for provider degraded thresholds — the orchestrator should read §14.4 (and §12.4 SBS validation rules, §6.1 S1, §18.3 readsb config) before writing the design; those sections bind Phase A but were not in my extraction scope.
- The interim REST serving surface is off-spec by design: no spec port, no HTTP metric names, no REST latency SLO, no HTTP health-endpoint convention (spec assumes gRPC health.v1). Each gap needs an explicit design decision recorded in the plan's conformance ledger, plus the spec amendment plan.md already calls for (fields served, seen_pos semantics incl. upstream age, staleness guarantees, sunset).
- Q5 (auth boundary for the serving tier) is unresolved and blocking: AC-S-01 forbids unauthenticated public exposure of readsb-derived endpoints, so the design cannot expose GET /v2/... publicly until mTLS vs private-network+firewall is chosen; if mTLS is chosen, ADR #4 (CA/enrollment) gets pulled into Phase A.
- Container-worded requirements (§20.1 isolation, §26.8 container scan, CI gate 6 'container image immutable, signed, tied to a source commit') need a documented systemd/bare-process translation; without recording it (ADR #3), acceptance review against §29 will read as failing.
- §28 Phase 1 as written includes the feed gateway and 'connect a few test receivers' — deferred by plan.md; the coverage-comparison exit evidence must come from an independent view instead. Track as a recorded divergence in the conformance ledger or Phase 1 exit review will not match the spec text.
- Dashboard 2 requires 'position age percentiles by source and node cohort' and AC-O-01a nominally includes dashboard 3 (node source selection) — no node cohorts or selection data exist until Phase C; Gate 1 evidence needs a recorded scope note.
- §26.2 golden fixtures require 'representative ADSBHub SBS lines for each supported transmission subtype' — the supported-subtype list comes from §12.4 (not extracted); fixture capture also depends on A0's readsb pin and ADSBHub entitlement, both external dependencies that can stall the fuzz/replay gates.
- §21.5 diagnostic-capture rules (operator reason, duration limit, access audit, deletion schedule) apply to the very SBS captures needed for fixtures and the 30-minute replay corpus — the design must include the retention/access policy or the captures themselves violate the spec.
