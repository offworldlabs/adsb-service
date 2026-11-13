# Conformance ledger — normative core vs Phase A

> The acceptance review surface plan §4 demands: every requirement id from
> `docs/requirements/normative-core.md`, honestly classified. Statuses:
> **satisfied** (implemented + tested in Phase A), **partial** (the Phase-A
> slice is done, the rest is deferred or operational), **deferred-phase-B** /
> **deferred-phase-C** (attaches to components not built in Phase A —
> recorded, not silently omitted), **diverged** (Phase A deliberately does
> something else; legitimized by ADR + spec amendment),
> **pending-operational** (needs the live entitled feed / soak / scan, not
> more code).
>
> "Where/Evidence" cites the component per DESIGN.md, tests, metrics, or the
> governing ADR. Design authority: `docs/DESIGN.md`; divergence authority:
> ADR-0004/0006 + `docs/spec-amendment-interim-serving.md` (DRAFT).

## Core requirements (all 66 ids, source order)

| Requirement | Status | Where | Evidence / note |
|---|---|---|---|
| `AI-01` — solver gets ADS-B only via node-local agent API | deferred-phase-C | — | Node-side. Interaction recorded: post-cutover nodes consume the interim REST endpoint via tar1090-node proxy, legitimized by the spec amendment (ADR-0004). No node consumes during Phase A (cutover gated on Q7/soak), so Phase A itself does not violate. |
| `AI-02` — local path has no cloud dependency | deferred-phase-C | — | Entirely node-side; no server-side Phase A obligation. |
| `AI-03` — local/central candidates kept separate | deferred-phase-C | — | Node fusion, Phase C. Server corollary covered by AI-04. |
| `AI-04` — contribution never central-derived; designated station documented | partial | docs/licenses.md (station record); plan A0; ADR-0008 | Designated station documented (feeds ADSBHub from its own decoder); nothing on the hub forwards hub/central data to ADSBHub. Since ADR-0008 anonymous feeders contribute on 7444: "local receiver data only" is a stated feeder obligation the hub **cannot verify** without identity (recorded, not enforced). Remaining: A0 live confirmation the station is actively feeding — pending-operational. |
| `AI-05` — sidecar Mode A default | deferred-phase-C | — | Node-side (Mode A only per plan §3). |
| `AI-06` — public exposure exactly 443/7444 | diverged | ADR-0005 (Accepted, Q5 option 3); DESIGN §1; deploy/nginx/public.conf | Second sentence satisfied: all readsb/metrics/admin ports loopback-only; through the front `/metrics` is 403 and every non-v2 path 404 (asserted by deploy/enable-public-front.sh on every run). First sentence diverged: Phase A exposes 443 (+80 ACME/redirect) as an **unauthenticated** public TLS REST front (adsb.lol parity) at adsb.retina.fm, not 443-gRPC-mTLS; and since ADR-0008 **7444 as an unauthenticated Beast contribution port** (plain or TLS, no client certificate, no account — adsb.lol ingest model) rather than the mTLS feed; recorded in the spec amendment §7/§7a. 7444 is reached at the origin, outside the Cloudflare shield. |
| `AI-07` — feed gateway cert↔node mapping | diverged | ADR-0008; deploy/nginx/feed.conf; deploy/enable-feed-port.sh | No gateway and no certificates: contributions are accepted from any source on 7444, attributed by source address only (PROXY v1 → readsb `--net-ingest`); no `node_id`/UUID mapping exists to enforce. The front is not a general TCP tunnel (fixed upstream 127.0.0.1:31004 only). Cert↔node mapping returns with the Phase B gateway; the mTLS switch itself is front-only. |
| `AI-08` — everything bounded; backoff+jitter, 60 s cap ADSBHub, 60 s reset | satisfied | services/adsbhub_adapter (backoff, relay queue 8 MiB/20k lines), services/serving_api (rate limiter, bounded LRU); test/unit backoff+bounds tests | Initial delay 1 s (configurable) per ADR-0006 §1 — recorded resolution of §18.2 vs §24. |
| `AI-09` — no provider specifics cross the canonical contract | diverged | ADR-0004 | No canonical contract exists in Phase A; the interim surface deliberately serves readsb/adsb.lol-shaped fields as a documented temporary contract with a sunset (amendment §8). Surface kept minimal to limit lock-in (plan §4). |
| `AI-10` — reuse readsb decode/CPR/dedup | satisfied | readsb-hub (pinned, ADR-0002); adapter validates/relays only; serving reads readsb state | No decode/CPR/dedup reimplementation anywhere in services/. |
| `aircraft-key` — 6 lowercase hex, optional `~` | satisfied | adapter SBS validation (ICAO syntax per §6.1 S1, 1–6 hex left-padded to 6 before relay — ADR-0007); readsb re-keys `%06x` (json_out.c:626/845); serving passthrough of readsb `hex` | ADSBHub omits leading zeros (`D0EEC` for 0D0EEC, ≈2.5% of aircraft); the adapter pads them so the key readsb emits is the §9.1 6-hex one instead of the line being dropped. Unit tests: accept/reject key cases, padding + case preservation, golden capture `test/fixtures/sbs/golden_icao_unpadded.txt`; fuzz properties "relayed ⇒ re-validates unchanged" and "relayed bytes differ only by ICAO leading zeros". |
| `units-no-silent-conversion` | partial | adapter (verbatim relay, no conversion — one enumerated exception: ICAO left-zero-padding, ADR-0007, counted on `_records_normalized_total{normalization="icao_zero_pad"}`); serving (verbatim passthrough keeps feet/knots) | SBS has no geometric altitude → `alt_geom` absent, never substituted by `alt_baro` (amendment §3). The ICAO padding converts no unit and loses no information (`int(body,16)` unchanged) — it re-spells the address in the width readsb keys on; any further rewrite needs its own ADR and label. Field groups/ObservationMeta deferred (Phase B). |
| `time-monotonic` | satisfied | adapter backoff/idle/relay timers; serving /health cache window | Monotonic clocks wherever two local instants are compared: adapter timeout/retry/age logic (unit-tested backoff schedule uses monotonic source) and the serving /health 1 s cache window. Necessary exception: the /health freshness verdict compares the wall clock against the envelope's epoch-ms `now` — an epoch comparison that cannot be monotonic; a host clock step therefore skews the staleness verdict (disclosed; guarded operationally by the ClockOffset alert). |
| `time-sync` — chrony + ClockOffset alert | pending-operational | observability/prometheus/alerts.yml (ClockOffset: >100 ms/5 m warn, >500 ms/2 m crit on `node_timex_offset_seconds`) | Rules shipped but blind until armed: they read node_exporter's timex collector, and neither node_exporter nor chrony is deployed by install.sh (dependency called out in the rule file — without the exporter the rules silently never fire). Host chrony + node_exporter deployment and verification is the A3/install item. |
| `readsb-time-mapping` — observed_at = now − seen_pos; seen never substitutes | satisfied | serving passthrough; amendment §4 | Semantics load-bearing for blah2-api reconstruction; `seen`-vs-`seen_pos` distinction documented and fixture-asserted (envelope fixture). |
| `timestamp-quantization` — measure + record | partial | docs/readsb-pin.md (TODO slot for measured value); test/integration quantization measurement | SBS field-8 ms precision, seen 0.1 s / seen_pos 1 ms output resolution pinned; measured value filled by integration run. time_uncertainty_ms propagation + CI constant validation deferred to Phase B (no ObservationMeta). |
| `provider-age-penalty` — §10.5 penalties | diverged | ADR-0004 | Canonical penalty mechanics deferred to Phase B. Interim translation: served seen_pos includes actual upstream latency via SBS timestamps (verified: 3.0 s lag → 3.414 served) — more accurate than the fixed 1,000 ms penalty. Mechanism + degradation modes in ADR-0004/amendment §4. |
| `validation-general` — §12.1 boundary rules | satisfied | adapter validation (DESIGN §4): 4096 B pre-allocation cap, printable-ASCII/control checks, field bounds, right-trim callsign, lowercase keys, drop+count by reason, never terminate stream | Unit accept/reject per rule + hypothesis fuzz (never raises, never relays non-MSG). One-bad-line-never-kills vs dead-upstream-closes-relay distinction implemented + integration-tested. |
| `range-checks` — §12.2 numeric bounds | partial | adapter ranges: lat ±90, lon ±180 inclusive (+180 served as-is — ADR-0006 §6), alt configurable `adsbhub.altitude_min_ft`/`altitude_max_ft` (defaults −2000..100000 ft), gs 0..1500 kt, track any finite value with magnitude ≤ 3600 (widened per ADR-0006 §7 — readsb accepts any finite heading), vr ±20k, squawk 4 octal, callsign ≤16 B | §12.2's two normalization clauses (+180→−180 longitude; track into [0,360)) are deliberately NOT applied in Phase A: the verbatim relay may not rewrite fields, and both rewrites land in the Phase B canonical layer (ADR-0006 §§6–7); the relay's sole enumerated rewrite is ICAO left-zero-padding (ADR-0007), which changes no value. Empty field = absent, never zero-default; NIC/NAC/SIL never fabricated (moot for SBS but binds output). Unit-tested per field. |
| `plausibility-checks` — §12.3 jump/staleness rejection | deferred-phase-B | — | State-layer function. **FLAG: Phase A served data has NOT passed §12.3 plausibility/jump rejection** — acknowledged interim gap, disclosed in ADR-0004 consequences + amendment. Phase A relies on readsb native handling + adapter field ranges + timestamp guard (future 1 s per §12.3 analogue). |
| `sbs-parsing` — §12.4 tested variants, CSV, config-not-compiled | satisfied | adapter; ADR-0006 §4 (variants = MSG 1–8, fixture per subtype); ADR-0007 (ICAO 1–6 hex, left-padded to 6 before relay, line-length cap re-checked after padding); config YAML (host/port/parsing all config) | Provider timestamps parsed UTC with sanity bounds (ADR-0006 §5); unknown types counted+ignored; no integrity fields fabricated. The ICAO padding is the single enumerated exception to verbatim relay and is idempotent — relayed bytes re-validate identically, so validator and readsb never read a relayed line differently. |
| `readsb-golden-fixtures` — §12.5 fixtures + upgrade gate | partial | test/fixtures/sbs/, test/fixtures/api/; runbooks/readsb-upgrade-rollback.md; ADR-0002 | Fixtures cover every field the SBS path can produce + absence assertions (alt_geom, nic etc.); Beast-only fields (geom_rate, category, nac_p…) not producible from SBS input — covered as omitted-field fixtures only. Upgrade shadow-replay gate binding and runbooked. |
| `fusion-selection` — §13 engine | deferred-phase-C | — | Node-side with Phase C (plan §2); §14 invariants/truth table/vectors go with it. |
| `freshness-classes-ttls` — §14.1/14.3 | deferred-phase-B | — | Canonical classes/TTLs Phase B/C. The EXPIRED >5,000 ms boundary = the node's 5 s budget → measured against ADSBHub latency in A3 (Q7, `interim-latency-gate` row). |
| `precision-eligibility` — §14.2 + vector 15 | deferred-phase-B | — | Flag exists only in the canonical contract. Binding consequence honored now: nothing served implies RF-exact timing/integrity (nic:0 passthrough, rssi placeholder documented, no fabricated fields — amendment §3). |
| `streaming-snapshots` — §15 gRPC semantics | deferred-phase-B | — | Phase A serves stateless HTTP GET instead (diverged-by-plan, interim; ADR-0004). |
| `local-agent-api` — §16 | deferred-phase-C | — | Node-side. |
| `history-bounds` — §17 | deferred-phase-C | — | Node agent bounds; server-side boundedness governed by AI-08 row. |
| `config-rules` — §18.1 | partial | adapter + serving YAML (config_version:1, unknown keys fatal, --check-config, redacted hash + build version); config/*.yaml examples | Fleet-signing clause deferred (no fleet). Vendored-boundary resolution: readsb gets generated verified args (deploy/gen-readsb-args.py per §18.3), not YAML retrofit; serving-api is new code and complies fully. |
| `feature-flags` — independent disables | partial | adapter unit independently stoppable: `systemctl stop`/`disable adsbhub-adapter` IS the ADSBHub-ingest flag (no config-file enable flag exists) | The systemd stop/disable mechanism is the only §32 flag expression applicable server-side in Phase A; the other five flags attach to deferred components. |
| `node-certificates` — §21.1–21.2 | deferred-phase-B | ADR-0005 | Node identity/PKI Phase B/C. Q5 chose the public front (no client identity in Phase A); the CA/enrollment procedure (ADR #4) arrives with the Phase B mTLS contract. |
| `input-resource-protection` — §21.3 | satisfied | adapter (pre-parse size limits, bounded queues); serving (token bucket ~1 Hz sizing, LRU cap, size caps); fuzz gate in default pytest; metrics carry no aircraft-key/callsign/cert/node-id labels | Log escaping + no-full-frames per §21.5/§22.3; bounded diagnostic capture opt-in only. |
| `compliance-gate` — §4.1 SBOM/license/terms/station | partial | docs/licenses.md; vendor/VENDOR.md | License review + SBOM snapshot done; station + whitelist coupling documented. Remaining operational: pre-launch terms recheck (annual cadence set), A0 station-feeding confirmation, SHOULD coverage comparison (A3 independent view). |
| `data-governance` — §21.5 retention/access/disclosure policy | partial | flagged: docs/licenses.md open items | **Gap: policy not yet written and no plan task owns it** — must exist before production rollout; also governs the SBS diagnostic/fixture captures themselves. Needs an owner (A0/A3 docs scope). |
| `perf-objectives` — §25.2 | partial | design: bounded queues + explicit drops (adapter relay drop-on-overflow counted; serving 429s) | Distribution SLOs attach to Phase B gRPC tier. Binding-now zero-lost-updates/explicit-overload principle implemented. Provider-to-server latency reported (delay histogram), not an SLO — but measured for Q7. §31 ADR #12 (SLO reference model) not yet written — open item, see `adrs` row. |
| `capacity-tests` — §25.3/§26.6 | partial | runbooks/readsb-upgrade-rollback.md (≥30-min replay gate); adapter close-relay + 60 s stale guard (no-backlog-replay, integration-tested) | 2×/5× capacity tests are Gate 2, deferred. Replay-comparison gate for readsb/parser changes binds now and is runbooked. |
| `AC-gates-mapping` — Phase A ≈ spec Phase 1 | diverged | this ledger; DESIGN §10 | **Phase 1 component delta recorded:** spec Phase 1 also runs feed-gateway + state server in shadow; Phase A runs only adapter + readsb-hub + interim serving. Phase 1 exit criteria (72 h soak, no unbounded growth, parse errors understood, freshness dashboards, independent-view comparison) are Phase A's acceptance bar; no core Gate closes in Phase A. |
| `AC-F-11` — no feedback loop incl. §4.1 path | partial | docs/licenses.md (station record); ADR-0008 | §4.1 half binds now: station feeds from its own decoder (A0 verify — pending-operational); server never forwards central data to ADSBHub (no such path exists). Node-contribution half: feeders on 7444 are told to send only their own receiver's data; a feeder re-feeding central data cannot be detected without identity (ADR-0008 — diverged by policy, spec §33 note 6 says a node's own report returning in the aggregate is harmless). |
| `AC-R-05a` — 72 h server soak | pending-operational | runbooks/ soak checklist; A3 | Operational, not a build artifact (DESIGN §10). Pass: no unbounded memory/queue/disk/descriptor growth. 48 h node soak deferred. |
| `AC-S-01` — public scan clean | diverged | ADR-0005 (Accepted); deploy/nginx/public.conf; deploy/enable-public-front.sh; A3 external-scan check | Default-closed as installed. With the Q5 option 3 front enabled the scan from an arbitrary address shows 22 and, since ADR-0008, **7444** (80/443 are admitted from Cloudflare's ranges alone; the public endpoint is Cloudflare's edge, proxied); the 443 endpoint is TLS but **unauthenticated** and 7444 is an **unauthenticated** Beast ingest (plain or TLS) — both recorded divergences; no raw readsb/metrics/admin/node-local port is reachable (7444 is nginx, forwarding only to the loopback Beast listener; front enumerates `/v2/` + `/health` only; ufw default-deny). Actual external scan is A3 pending-operational and re-runs after network changes. |
| `AC-S-04` — fuzz: no crash/hang/unbounded/secret | satisfied | test/unit hypothesis fuzz over SBS validator (default pytest = release gate); corpus seeded from fixtures; ADR-0001 (fuzz-gate-regardless mitigation) | Properties: arbitrary bytes never raise; never relay non-MSG; relayed ⇒ re-validates. Continuous corpus retention per §26.8. |
| `AC-S-05` — SBOM, least privilege, no node keys | satisfied | ADR-0003 (systemd translation); deploy/systemd hardening inventory; docs/licenses.md (SBOM) | Adapted per spec §20.1's "hardened system services" allowance: separate users, ProtectSystem=strict, MemoryMax, loopback-only. No node private keys exist server-side. |
| `AC-O-01a` — dashboards 1–3 | partial | dashboards/pipeline.json, freshness.json (items 1–2) | Item 3 (node source selection) deferred-phase-C. **Node-cohort panels N/A until Phase C** — recorded scope note (DESIGN §10) so Gate 1 evidence reads correctly. |
| `AC-O-02` — each §24 failure → documented signal, no alert storm | partial | adapter/serving failure handling + observability/prometheus/alerts.yml (disconnect, format change, readsb crash via restart visibility, malformed line counters; memory pressure for the two Python services; clock jump + disk full rules require node_exporter — see `time-sync`/`alerts` rows) | Every server §24 row has a signal, but three are conditional: clock-jump and disk-full alerting arm only once node_exporter is deployed, and memory-pressure alerting excludes readsb (no RSS metric). Node rows deferred. No-alert-storm property verified during soak — pending-operational element. |
| `AC-O-03` — staleness attribution | partial | `retina_adsb_provider_record_delay_seconds` (before-ingest) vs `last_record_timestamp` + api-cache freshness (processing); freshness dashboard | Before-ingest vs server-processing legs bind now and are instrumented; delivery/node-selection legs deferred. |
| `AC-O-04` — build/schema/config hashes visible | partial | both services expose `retina_adsb_build_info{version,commit,schema_version,config_hash}` (config hash redacted; replaces the earlier adapter-only `retina_adsb_adapter_build_info`); readsb pin self-reports version | Server half done; node half deferred. |
| `AC-O-05a` — runbooks | partial | runbooks/adsbhub-disconnected.md, parse-error-surge.md, readsb-upgrade-rollback.md (+ accidental public-port-exposure check) | Server subset per plan A3; node-receiver-loss and certificate runbooks deferred. Each: symptoms/dashboards/diagnostics/decision tree/remediation/rollback/verification/escalation. |
| `deferred-acceptance` — all other AC rows | deferred-phase-B/C | — | AC-F-01..10, AC-F-12, AC-R-01..04/06/07, AC-P-01..07, AC-S-02/03/06, AC-O-01b, AC-O-05b attach to the agent/fusion/gRPC/contribution stack. Recorded as not-applicable-in-Phase-A rather than silently omitted. |
| `invariants-test-vectors` — §14 properties + vectors 1–15 | deferred-phase-C | — | Ship with the Phase C fusion engine. Phase A test surface instead: SBS fuzz corpus, golden readsb fixtures, envelope fixture vs tar1090-node proxy mapping. |
| `contract-authority` — §11 proto is the contract | diverged | ADR-0004; docs/spec-amendment-interim-serving.md (DRAFT) | **The** recorded divergence: interim adsb.lol v2 REST replaces §11 until Phase B commits the proto. Treated via the core's own conflict rule — amendment drafted, sunset defined, surface minimal. Amendment awaits spec-owner signoff. |
| `s1-adapter-duties` — §6.1 S1 | satisfied | services/adsbhub_adapter per DESIGN §4 (one upstream conn, backoff+jitter, framing cap, validation, drop+count, relay loopback-only after upstream healthy, close-relay-on-failure, state metrics, opt-in bounded diagnostics, no Internet listener, no canonical schemas) | Language: Python per ADR-0001 (Q4 default). Integration test: relay closes on upstream kill and readsb aircraft go stale. |
| `readsb-hub-topology` — §18.3 generated verified args | satisfied | deploy/gen-readsb-args.py; test/unit/test_deploy_args.py (generated vector = DESIGN §3 set; `--systemd` output = frozen unit ExecStart) + install.sh §18.3 gate (re-checks the unit line against the generator over /etc/retina-adsb/readsb.yaml, refuses on mismatch); DESIGN §3 | Deltas from §18.3's expected shape recorded in DESIGN §3: 31004 omitted (no gateway until Phase B), no --net-ingest/--net-receiver-id (SBS carries no receiver identity), re-api 30152 + write-json instead of 31005 json_out as the serving read path (per-position json_out is the Phase B tap). Loopback binds per §19. |
| `stream-health-thresholds` — §14.4 rows 1–2 | partial | Row 1 satisfied: adapter /healthz `state` degrades at 10 s and goes unhealthy at 30 s, mirrored by the ADSBHubDegraded (warning) / ADSBHubDown (critical) alerts. Row 2 partial: CentralStateStalled (byte-correlated) detects the condition, but detection latency is MINUTES, not the spec's 15 s — readsb state reaches Prometheus only via stats.prom (rewritten once/min as a rolling 1-min window) scraped at 30 s, so measured worst-case is ~2–2.5 min (≤60 s file rewrite + ≤30 s scrape + 15 s hold) | Row 2 latency recorded honestly rather than pretending 15 s; identified future fix: poll readsb re-api (`?status`/aircraft state) directly into a fresher metric. Distinguishes active-empty-feed from dead connection via bytes, never aircraft count alone. Rows 3–5 deferred. |
| `provider-metrics` — §22.1 | partial | adapter :9101 emits the full provider set (`connected`, `_bytes_total`, `_records_total{result}`, `_last_record_timestamp_seconds`, `_reconnects_total{reason}`, `_record_delay_seconds` histogram) + `_records_normalized_total{normalization}` (not a §22.1 name — the enumerated relay rewrites, only `icao_zero_pad`, ADR-0007) + `retina_adsb_build_info{version,commit,schema_version,config_hash}` (emitted by BOTH services — adapter :9101 and serving :8080; replaces the earlier `retina_adsb_adapter_build_info`); readsb hub-state via native stats.prom textfile | Assignment resolution recorded: `retina_adsb_readsb_json_records_total`/`aircraft_live` normatively belong to retina-adsb-server (absent in Phase A) — readsb's native metric names (`readsb_aircraft_with_position` etc.) substitute via stats.prom; retina-named hub-state metrics arrive with Phase B. Serving HTTP metrics are new names following the same conventions (spec silent on REST). |
| `structured-logs` — §22.3 | partial | adapter + serving stdlib logging: plain-text single-line records; no secrets/tokens/full payloads logged (basis: code inspection — the adapter logs raw upstream data only as an opt-in bounded 64-byte diagnostic excerpt; serving runs uvicorn `access_log=False` with route-template-only metric labels — plus test/unit/test_serving_logging.py: caplog at DEBUG during a live /v2 request asserts no client IP or URL-with-query is logged) | **Known gap: the §22.3 structured-JSON format is NOT implemented** — logs are plain text, not JSON. When JSON logging lands it must carry the §22.3 field set: ts/severity/service/version/host/event/message/connection_id/error_code/retry_in_ms. Aircraft key debug-only. |
| `alerts` — §22.5 server set | partial | observability/prometheus/alerts.yml: ADSBHubDown (30 s), ADSBHubDegraded (warning, >10 s), CentralStateStalled (byte-correlated), ParseErrorSurge (>5%/5 m, min 100 — ADR-0006 §3), AdapterHealthEndpointDown; host group: ClockOffset, MemoryPressure, DiskUsage | Provider/pipeline rules are self-contained on scraped series. Host rules carry honest caveats: ClockOffset + DiskUsage require node_exporter (NOT deployed by install.sh) and silently never fire without it; MemoryPressure covers only the two Python services (`process_resident_memory_bytes` vs their 256M MemoryMax) — readsb exposes no RSS metric, so its 1G bound is enforced by cgroup + restart visibility only. Distribution/freshness-regression/node alerts deferred with their components. |
| `health-readiness` — §23 | satisfied | both services expose `/livez` (pure liveness, always 200 while the process serves); adapter `/healthz` returns JSON with a `state` field — healthy \| degraded (no valid record for 10–30 s) \| unhealthy; serving `/health` ready only when readsb reachable + envelope fresh (verdict computed at most once per second — 1 s cache) | Liveness and readiness are separate endpoints per §23; empty airspace never a liveness failure (byte-based health). |
| `failure-behaviors` — §24 server rows + backoff | satisfied | adapter (disconnect→backoff; format change→drop+count+degraded; no backlog replay per §33n11 via close-relay + 60 s stale guard); systemd Restart (readsb crash); monotonic ages (clock jump); tmpfs + rate-limited logs (disk); bounded queues (memory) | Backoff initial 1 s configurable — recorded divergence from §24's 250 ms progression start (ADR-0006 §1). |
| `testing-ci` — §26/§27 | partial | test/unit (every validation rule, backoff, ratelimit, config strictness), fuzz-in-default-pytest, golden fixtures, test/integration end-to-end vs real readsb, envelope fixture | Property/contract/proto CI gates deferred with their components; 72 h soak operational (AC-R-05a row); 30-min replay runbooked. |
| `adrs` — §31 before code freeze | partial | docs/adr/0001–0006 | Mapping to §31 numbering: 0001=#1 language, 0002=#2 readsb pin, 0003=#3 platform, 0004=#10 timestamp trust/latency, 0005≈#4 boundary (Accepted 2026-09-05: Q5 option 3, public front), 0006=conflict resolutions; #11 licensing conclusions recorded in docs/licenses.md + vendor/VENDOR.md. **Open: #12 SLO reference hardware/traffic model not yet written** (low Phase A impact — no distribution SLOs active — but required before code freeze per §31). #6 spatial index N/A (inherited from readsb re-api). #13 ADR deferred with decimation. |
| `interim-honest-seen-pos` — plan §0 req 1 | satisfied | ADR-0004 mechanism (readsb SBS-timestamp clocks + adapter guard + delay histogram); integration test: 3 s injected lag appears in served seen_pos (≥3 s) | The top A2 design problem, resolved: upstream latency included natively, degradation modes bounded + observable. Documented in amendment §4. |
| `interim-envelope` — plan §0 req 4 | satisfied | serving verbatim jv2 passthrough (envelope built by readsb); live e2e assertion `test/integration/test_e2e.py::test_envelope_shape` (ac array, ms now, "ground" string, seen_pos, absent alt_geom) which also records the actual served body to `test/fixtures/api/jv2_envelope_recorded.json` (regenerated each integration run) | Radius unit pinned: nautical miles, clamp 250 (amendment §1) — resolves the nmi-vs-meters ambiguity before node cutover. Unit tests mock the upstream with the hand-synthesized `jv2_envelope.json` (provenance: test/fixtures/api/README.md). |
| `interim-latency-gate` — plan §0 req 2 / Q7 | pending-operational | `retina_adsb_provider_record_delay_seconds` + freshness dashboard; A3 characterization procedure | Requires the live entitled feed + a receiver visible both directly and via ADSBHub. **Gates any node cutover**; if latency ~5 s the central-fallback proposition needs re-examination. |
| `interim-rate-limits` — plan §0 req 5 | satisfied | serving token bucket (2/s, burst 10, per client IP, LRU 10k), 429 + Retry-After; GET-only, request-line caps | Interim expression of §21.3 token buckets; sized for ~1 Hz node polling. |
| `tar1090-operator` — §6.1 S3 MAY | pending-operational | deploy/README.md "Operator visualization (optional)" | **Not shipped in Phase A**: tar1090 is not installed or configured anywhere in this repo (a MAY per §6.1 S3 — visualization is optional). Install path documented (wiedehopf/tar1090 standard install, json dir `/run/readsb`); if an operator adds it, it must stay behind the Q5 boundary — never public, never a node/solver contract. |
| `sbs-timestamp-caution` — §33 notes 2/5/11 | diverged | ADR-0004 (note 5); adapter (notes 2, 11) | Notes 2 (seen≠seen_pos) and 11 (no backlog replay: close-relay + 60 s stale guard) satisfied. Note 5's "default to conservative receipt-based until validated" deliberately replaced by guarded timestamp trust + always-on validation histogram — recorded resolution, rationale + rejected alternative in ADR-0004. |

## Plan §0 serving-side requirements 1–5

| Req | Status | Where | Evidence / note |
|---|---|---|---|
| `plan-req-1` — honest per-aircraft age (seen_pos includes upstream latency) | satisfied | ADR-0004; amendment §4 | Same as `interim-honest-seen-pos` above; integration-verified (3 s lag ≥3 s served). |
| `plan-req-2` — measure ADSBHub aggregate latency vs 5 s budget (Q7) | pending-operational | delay histogram + A3 procedure | Same as `interim-latency-gate`; gates node cutover. |
| `plan-req-3` — alt_geom absent for ADSBHub aircraft | satisfied | amendment §3; envelope fixture asserts absence; never synthesized (§33n9) | blah2-api falls back to alt_baro (fine); adsb2dd hard-drop is a node-side blocker only if the dormant path revives. |
| `plan-req-4` — match adsb.lol v2 envelope | satisfied | verbatim readsb jv2 passthrough; live e2e shape assertion + recorded fixture `test/fixtures/api/jv2_envelope_recorded.json` vs tar1090-node v0.2.0 proxy/server.js:90-129 expectations | Same as `interim-envelope`. |
| `plan-req-5` — per-node rate limiting ~1 Hz | satisfied | serving token bucket | Same as `interim-rate-limits`. |

## Known divergences (DESIGN §10) — index

1. **Interim REST replaces §11 gRPC** — rows `contract-authority`, `AI-09`,
   `streaming-snapshots`, `AI-06`; ADR-0004; amendment DRAFT; sunset =
   Phase B native contract adoption.
2. **Phase 1 exit component delta** — row `AC-gates-mapping`: spec Phase 1
   includes feed-gateway + state server in shadow; Phase A omits both.
   Coverage comparison must use an independent view (A3).
3. **Dashboards node-cohort panels N/A** — row `AC-O-01a`: no cohorts exist
   until Phase C; dashboard-2 scope note recorded.
4. **72 h soak is operational** — row `AC-R-05a`: a checklist-driven live
   activity, not a build artifact; runbooked.
5. **Backoff initial 1 s / §33n5 timestamp-trust replacement / thresholds** —
   ADR-0006 + ADR-0004 recorded resolutions (rows `AI-08`,
   `failure-behaviors`, `sbs-timestamp-caution`, `alerts`, `sbs-parsing`).
6. **ICAO left-zero-padding before relay** — ADR-0007: the one enumerated
   exception to verbatim relay (rows `aircraft-key`,
   `units-no-silent-conversion`, `range-checks`, `sbs-parsing`), without
   which the whole 000000–0FFFFF address block is invisible.
7. **Account-less Beast contribution port on 7444** — ADR-0008 (owner
   decision 2026-09-06; spec declared guidance, version 1.4 status note):
   rows `AI-06`, `AI-07`, `AC-S-01`, `AI-04`, `AC-F-11`; amendment §7a. No
   mTLS, no cert↔node mapping, no UUID marker; TLS optional; bounded per
   source address; `--json-reliable=2`; 7444 outside the Cloudflare shield.

## New surfaces the spec does not name (registered here per DESIGN)

- **Feed front internals (ADR-0008):** readsb `--net-ingest` writes
  `/run/readsb/clients.json` every 10 s (feeder source address, receiver id
  hashed from it, rates) and logs `disc:` lines with the address to the
  journal — operational data under the §21.5 policy (not written yet; feeder
  addresses are the first personal-data-like field the hub retains). nginx
  keeps no feed access log. Loopback `127.0.0.1:7445` is the TLS terminator
  behind 7444 (never reachable directly).

- Adapter metrics/health HTTP on `127.0.0.1:9101` (`/metrics`, `/healthz`) —
  interim surface, loopback-only.
- Serving-api HTTP metric names (requests by route/status, upstream latency
  histogram, rate-limit drops) — spec is silent on REST; names follow §22
  conventions.
- `retina_adsb_provider_records_normalized_total{provider,normalization}` —
  counts the enumerated exceptions to verbatim relay (only `icao_zero_pad`,
  ADR-0007); spec §22.1 has no such metric because it assumes no rewrite
  exists. Label is the fixed rule name, never an aircraft key (§21).
- Serving-api port `127.0.0.1:8080` and readsb re-api `127.0.0.1:30152`
  (adsb.lol convention) — outside the §19 port plan; loopback-only,
  amendment documents the exposure path.
- **Debug SBS inject port off by default (DESIGN §3 agrees):** the shipped
  `config/readsb.yaml` comments out `sbs_debug_port`, so the generated/frozen
  invocation carries NO `--net-sbs-in-port=32006`; DESIGN §3's example
  invocation omits the flag and documents the commented-out config key.
  Rationale (sec review): the debug injection listener
  feeds readsb lines that bypass the adapter's §12.4 validation and the
  ADR-0004 stale/future guard — test/debug overlays only, never the
  production ExecStart. `deploy/verify-units.sh` asserts the flag stays off
  the frozen unit line.

## Open gaps (honest list)

- §21.5 data-governance policy unwritten, no owner (`data-governance` row) —
  blocks production rollout.
- §31 ADR #12 (SLO reference model) unwritten (`adrs` row).
- Q5 decided (ADR-0005 Accepted: public TLS front, adsb.lol parity;
  `AC-S-01`/`AI-06` diverged) — cutover no longer boundary-blocked; still
  gated on Q7, the A3 soak and the external scan.
- Q7 latency measurement + 72 h soak + external scan + A0 station
  confirmation — pending-operational set.
- Spec amendment is DRAFT — divergences are recorded but not yet legitimized
  in the binding docs (the docs now carry a 1.4 status note that they are
  guidance, per the owner; the amendment integration remains the clean way
  to fold the divergences in).
- Feeder identity (ADR-0008): none. Abuse of 7444 is bounded per source
  address only; AI-04's node half is unenforceable until the Phase B
  gateway. 7444 is unshielded from volumetric attack (Cloudflare carries
  HTTP only); Spectrum or a separate ingest host if that materializes.
- Feed TLS hostname: the front presents the `adsb.retina.fm` certificate,
  a name that resolves to Cloudflare, so TLS feeders must connect to the
  origin IP with SNI/verification set to that name. A DNS-only `feed.<zone>`
  record + `enable-feed-port.sh --tls-host feed.<zone>` fixes that (opens
  80/tcp to anyone for HTTP-01).
