# Phase A design — binding decisions for implementation

> Written 2026-08-29 from the reader-agent extraction in `docs/requirements/`.
> Scope authority: `plan.md`. Requirement authority: `RETINA_ADSB_NORMATIVE_CORE.md`
> (and the full-spec sections it incorporates). This document locks the concrete
> design choices so all components agree; where it resolves a spec ambiguity or
> conflict, the resolution is recorded here and in `docs/adr/`.

## 1. Topology (single host, Phase A)

```
data.adsbhub.org:5002  ──TCP out──►  adsbhub-adapter (Python, asyncio)
                                        │  loopback relay listen 127.0.0.1:31003 (SBS)
                                        ▼  (readsb dials out: --net-connector=127.0.0.1,31003,sbs_in)
                                    readsb-hub (pinned v3.16.16 d9a4c62, plain build)
                                        │  re-api HTTP 127.0.0.1:30152   │  /run/readsb/aircraft.json (tar1090)
                                        ▼                                │  /run/readsb/stats.prom (metrics)
                                    serving-api (Python, FastAPI) 127.0.0.1:8080
                                        ▼
                                    [Q5 boundary: nginx mTLS / private network — default CLOSED]
                                        ▼
                                    GET /v2/lat/{lat}/lon/{lon}/dist/{radius}   (adsb.lol v2 envelope)
```

All listeners bind loopback except the (optional, operator-enabled) nginx front.
Exactly zero public ports out of the box — AC-S-01 satisfied by default-closed;
the Q5 decision (mTLS vs private network) is pending stakeholders and shipped as
two documented nginx configs (see §8).

## 2. Ports and paths (fixed)

| What | Value | Why |
|---|---|---|
| ADSBHub upstream | `data.adsbhub.org:5002` (config, not compiled) | spec §6.1 S1 |
| Adapter relay listen | `127.0.0.1:31003` | spec §18.3 |
| Adapter metrics/health HTTP | `127.0.0.1:9101` (`/metrics`, `/healthz` readiness, `/livez` liveness) | new (interim surface, recorded in ledger) |
| readsb re-api | `127.0.0.1:30152` (TCP; path `/re-api/` cosmetic) | adsb.lol convention |
| readsb JSON dir | `/run/readsb` (systemd `RuntimeDirectory=readsb`) | readsb default; tar1090 reads it |
| serving-api | `127.0.0.1:8080` (`/v2/...`, `/health` readiness, `/livez`, `/metrics`) | new |
| readsb debug SBS inject | `127.0.0.1:32006` (`--net-sbs-in-port`) — **disabled by default** (bypasses the adapter's validation and stale guard; test overlays only) | adsb.lol pattern |

## 3. readsb-hub (reused, not reimplemented — AI-10)

Pinned build (ADR-0002): wiedehopf/readsb v3.16.16, commit `d9a4c62`, plain
`make -j$(nproc) OPTIMIZE="-O2"` (all SDR backends off by default;
`WITH_UUIDS=no` — verified: it only enables re-api `&filter_uuid`, useless for a
single anonymous SBS upstream). Build deps: build-essential, pkg-config,
libncurses-dev, zlib1g-dev, libzstd-dev.

Invocation (generated from config by `deploy/gen-readsb-args.py` per §18.3 —
do not hand-edit the unit):

```
readsb --net --quiet
  --net-bind-address=127.0.0.1
  --net-connector=127.0.0.1,31003,sbs_in
  --net-connector-delay=5
  --max-range=99999
  --write-json=/run/readsb --write-json-every=1 --json-reliable=1
  --net-api-port=30152 --api-shutdown-delay=2
  --write-prom=/run/readsb/stats.prom
```

(`--net-sbs-in-port=32006` is available via the `sbs_debug_port` config key but
ships commented out: it would accept unvalidated SBS on loopback, bypassing the
adapter's §12.4 validation and the ADR-0004 stale guard.)

MUST NOT set: `--lat/--lon` (would arm the 450 nmi range filter and drop far SBS
positions), `--json-separate-alt-ground`/`--json-separate-ground` (would break the
`alt_baro:"ground"` v2 envelope), a second `--net-api-port` (single port only).
`--db-file` deferred (r/t fields not consumed by the node proxy) — config option later.
`--json-reliable=1` is readsb's default: SBS positions arrive `+2` reliable, so
they are immediately JSON-visible either way; keep the default, record in ledger
(adsb.lol uses 2 for multi-feeder garbage rejection — not applicable to one
validated upstream).

readsb facts the design relies on (verified live by readers):
- SBS lines: only `MSG`, length 20–199, ≥22 comma fields, 6-hex ICAO (optional `~`);
  MSG types 1–8 parsed identically; malformed lines counted
  (`readsb_messages_basestation_invalid`) and never kill the connection.
- SBS "generated" timestamp (fields 7/8, UTC, ms) drives `a->seen/seen_pos`;
  clamp to arrival wall clock if ts > arrival+1 s or < arrival−20 min.
  **Therefore served `seen_pos` honestly includes upstream+ingest latency**
  (verified: 3.0 s injected lag → `seen_pos` 3.414) — the plan §0 req 1 mechanism.
- Idle connector force-reconnect after 2×`--net-heartbeat` = 120 s without bytes;
  readsb reconnects a closed connector every `--net-connector-delay` (5 s).
- Aircraft leave JSON/api ~60 s after last message; position >60 s old renders as
  `lastPosition` (circle queries only match current positions).
- re-api `?circle=<lat>,<lon>,<radius nmi>&jv2` returns the adsb.lol v2 envelope
  `{"ac":[...], "msg":"No error", "now":<ms>, "total":N, "ctime":<ms>, "ptime":<ms>}`
  with per-aircraft `dst`/`dir`; `now`/`ctime` = api cache build time (refreshed every
  `--write-json-every`, so ≤1 s added staleness). `GET /?status` → 200 = healthy.

## 4. adsbhub-adapter (new Python service — `services/adsbhub_adapter/`)

Single asyncio process. Data path: upstream TCP client → line framing (max
4096 B before allocation, spec §18.2/§12.1) → SBS validation → relay to at most
readsb. **The adapter forwards only lines that passed validation, verbatim**
(no rewriting — readsb's parser is the canonical consumer; we pre-filter, not
transform).

Upstream session:
- One long-lived outbound TCP connection; host/port/all tunables from YAML config.
- Reconnect: exponential backoff, `initial=1s` (per §18.2; §24's 250 ms start
  recorded as conflict, resolution = configurable initial, ADR-0006), doubling,
  cap 60 s, full jitter, reset after 60 s stable connection (§24).
- `idle_timeout=30s`: no bytes for 30 s → treat session dead, close, reconnect
  (aligns with §14.4 unhealthy-at-30 s; TCP keepalive also enabled). Resolution
  of the unspecified §18.2 `idle_timeout` semantics — ADR-0006.
- Connection failure ⇒ close all relay client connections immediately
  (§6.1 S1 close-relay-on-upstream-failure) and stop listening until the
  upstream is re-established and has delivered ≥1 valid line.

SBS validation (per §12.4 + §12.2, pre-filtering to readsb's accepted subset):
- Structural: printable ASCII (reject NUL/control chars except CR/LF), field
  count ≥ 22, field 1 == `MSG`, MSG type ∈ {1..8} (the "explicitly tested
  variants" set — golden fixture per subtype in `test/fixtures/sbs/`), ICAO
  6 hex chars (optional leading `~`), line length 20–199 (readsb's own cap —
  validate BEFORE relay so we count, not readsb).
- Ranges (only when field non-empty; empty = absent, never zero-default):
  lat [−90,90], lon [−180,180], baro alt [−2000,100000] ft (configurable:
  `adsbhub.altitude_min_ft`/`altitude_max_ft`, §12.2), gs [0,1500] kt,
  track any finite value with |t| ≤ 3600 (spec §12.2 says normalize-mod-360,
  which a verbatim relay cannot do and pinned readsb accepts any finite
  heading — ADR-0006 §7; only extreme overflow rejected), vrate
  [−20000,20000] ft/min, squawk 4 octal digits, callsign ≤16 bytes
  A-Z/0-9/space after right-trim. Numeric fields must be strtod-compatible
  (no `_`, inf/nan, hex) so the validator parses the same value readsb will.
- Timestamp guard (resolves the readsb clamp hazards; ADR-0004): parse fields
  7/8 as UTC **with exact readsb parity** (net_io.c:3141-3161): the seconds
  prefix via strptime-equivalent parsing, the fractional remainder with strtol
  semantics as a *literal integer count of milliseconds* (`.5` = 5 ms, not
  500 ms; `.abc` = 0) — so guard decisions and the delay metric operate on the
  same value readsb will compute (verified by differential fuzz against a C
  transcription, and by hypothesis properties in `test_sbs_fuzz.py`).
  Reject (count, don't relay) lines whose generated timestamp is
  > `future_tolerance` (1 s, §12.3 analogue) ahead of adapter wall clock or
  older than `max_record_age` (default 60 s) — prevents the "readsb clamps
  >20 min stale to arrival ⇒ stale looks fresh" failure. Unparseable timestamp
  (exactly when readsb's own strptime would fail and stamp arrival):
  relay (arrival = conservative) but count separately. Timestamps are UTC by
  readsb parity, not configuration (ADR-0006 §8).
  Per-line delay (wall − generated) feeds the delay histogram (§22.1 + Q7 data).
- Malformed input: drop + count by reason, never terminate the stream (§12.1);
  never log full payloads (bounded diagnostic sample only when explicitly
  enabled with reason/duration — §21.5).

Relay (loopback listener 127.0.0.1:31003):
- Listen ONLY while upstream is healthy per §14.4 row 1: established + a valid
  record within the 30 s threshold. When freshness lapses (e.g. an upstream
  that stays connected but sends only invalid lines), the relay stops
  listening and closes clients; it resumes on the next valid line.
- Send `\r\n` heartbeat every 25 s when no data flows (readsb kills idle
  connectors at 120 s).
- Bounded per-client outbound queue (default 8 MiB / 20k lines); on overflow
  drop the client connection (bounded-everything, AI-08), count it.
- Accept up to `max_relay_clients` (default 2 — readsb + one debug tap).

Config: YAML, strict (`config_version: 1` required, unknown keys fatal,
`--check-config` mode prints redacted hash and exits — §18.1). Prometheus
metrics on 127.0.0.1:9101 with the §22.1 names, `provider="adsbhub"`:
`retina_adsb_provider_connected`, `_bytes_total`, `_records_total{result=ok|invalid_structure|invalid_range|stale|future|unparseable_ts}`,
`_last_record_timestamp_seconds`, `_reconnects_total{reason}`,
`_record_delay_seconds` (histogram, buckets 0.1–60 s), plus
`retina_adsb_relay_connected_clients`, `retina_adsb_relay_dropped_lines_total`,
and `retina_adsb_build_info{version,commit,schema_version,config_hash}`
(§22.1 name; commit from `RETINA_ADSB_COMMIT` env, schema_version
`interim-v2`; emitted by both Python services). The last-record gauge is
initialized to 0 at startup so the silent-feed alert clause has a series
immediately after restart.
`/healthz` (readiness, §14.4 row 1): JSON `state` healthy | degraded
(last valid record 10–30 s old, still HTTP 200) | unhealthy (503: disconnected
or >30 s). `/livez` (liveness, §23): 200 whenever the event loop responds,
independent of upstream health — the probe systemd/monitoring restarts on.
Relay and metrics binds are loopback-enforced by the config loader
(§6.1 S1); `allow_nonloopback_bind: true` is the deliberate escape hatch.
Relay client close is bounded: graceful close with a 1 s timeout, then
`transport.abort()` — a hung client can never stall reconnect or shutdown.

## 5. serving-api (new slim Python service — `services/serving_api/`)

New ~200-line FastAPI app reusing the vendored `reapi.py` pattern (BSD-3),
NOT the vendored `app.py` (hard-requires Redis, phones adsb.lol infra).

Routes:
- `GET /v2/lat/{lat}/lon/{lon}/dist/{radius}` and alias
  `GET /v2/point/{lat}/{lon}/{radius}`: typed validated params
  (lat float −90..90, lon float −180..180, radius float 0..250 — the vendored
  annotation-injection performed NO validation; radius clamped `min(radius,250)`
  nmi matching adsb.lol). Maps to `?circle={lat},{lon},{radius}&jv2` against
  `http://127.0.0.1:30152/re-api/`; response body passed through **verbatim**
  (envelope built by readsb — byte-compatible with adsb.lol by construction).
- `GET /health`: proxies readsb `?status` + checks envelope `now` freshness
  (stale if wall − now > 5×write-json-every) + reports adapter `/healthz` state
  (best-effort). 200/503 + JSON detail.
- `GET /metrics`: prometheus (request counts by route/status, upstream latency
  histogram, rate-limit drops).

Hardening over vendored code: one shared `aiohttp.ClientSession` (5 s total /
1 s connect timeouts); upstream failure → 502 JSON (`{"msg": "upstream unavailable"}`),
timeout → 504; never a bare 500. Upstream bodies are stream-read against a
byte budget (`readsb_response_max_bytes`, default 32 MiB; oversize → 502) so a
runaway readsb response cannot OOM the 256M-capped process. No client IP +
full-URL stdout logging (vendored `reapi.py:27` leaks query history — replaced
by counters). Rate limiting: in-app token bucket per client IP (default rate
2/s, burst 10 — sized for ~1 Hz node polling), 429 + `Retry-After` on excess.
Behind the deploy/nginx fronts the direct peer is the proxy; peers listed in
`rate_limit.trusted_proxies` (default empty) have the bucket keyed on the
rightmost `X-Forwarded-For` entry — the hop appended by our own proxy — while
XFF from any unlisted peer is ignored as spoofable. Bucket table is
bounded (LRU, 10k clients) plus a global new-key admission bucket
(`rate_limit.new_key_rate` 50/s, burst 200) so key-rotation floods (e.g. an
IPv6 /64) cannot evict established buckets for fresh bursts. `/health` result
is cached 1 s behind a lock, so looping health callers cost at most one
upstream probe fan-out per second; `/livez` answers from the event loop alone.
Body size N/A (GET only); request line length capped by uvicorn defaults.

Config: same YAML conventions as the adapter (config_version, unknown-keys
fatal, `--check-config`). Bind 127.0.0.1:8080; a non-loopback `bind.host` is
refused at load unless `allow_nonloopback_bind: true` (mirrors the adapter
loader; AC-S-01 default-closed).

## 6. Repository layout (Python adaptation of spec §30 — divergence recorded)

```
services/adsbhub_adapter/   # S1 (spec cmd/adsbhub-adapter + internal/sbs)
services/serving_api/       # interim serving tier (vendored-api derivative)
deploy/systemd/*.service    # hardened units (separate users, see §8)
deploy/nginx/               # Q5 boundary options (mtls.conf, private-net.conf)
deploy/install.sh           # idempotent single-host install
deploy/build-readsb.sh      # pinned build (ADR-0002)
deploy/gen-readsb-args.py   # §18.3 config → verified readsb args
config/adapter.yaml, serving.yaml, readsb.yaml   # config_version:1 examples == defaults
test/fixtures/sbs/          # golden SBS lines per MSG subtype + malformed corpus
test/fixtures/api/          # recorded jv2 envelope fixtures
test/unit/                  # validator, backoff, ratelimit, config tests + hypothesis fuzz
test/integration/           # live end-to-end against built readsb (marker: integration)
dashboards/                 # grafana: pipeline.json, freshness.json
observability/prometheus/   # scrape config + alert rules yml
runbooks/                   # adsbhub-disconnected.md, parse-error-surge.md, readsb-upgrade-rollback.md
docs/adr/                   # 0001..0006
docs/conformance-ledger.md  # normative-core → satisfied/deferred/diverged
docs/spec-amendment-interim-serving.md   # plan §0 amendment draft
vendor/                     # reference clones (api BSD-3, infra BSD, readsb GPL-3) + VENDOR.md
```

Python: single `pyproject.toml` at repo root, package dir `services/`
(packages `adsbhub_adapter`, `serving_api`), console scripts
`adsbhub-adapter` and `retina-serving-api`, venv `.venv/` (git-ignored).
Deps: fastapi, uvicorn, aiohttp, prometheus-client, pyyaml;
dev: pytest, pytest-asyncio, hypothesis, httpx, aioresponses.
Python 3.12 (host). Style: PEP 8, type hints, no framework magic.

## 7. Testing (the §21.3 / §26 gates that apply server-side)

- Unit: every validation rule (accept + reject cases), backoff schedule +
  jitter bounds + reset, timestamp guard edges (future/stale/unparseable),
  rate limiter, config strictness (unknown key fatal, missing version fatal).
- Fuzz (release gate §21.3): hypothesis property tests over the SBS validator —
  arbitrary bytes never raise, never relay non-MSG, relayed ⇒ re-validates;
  corpus seeded from golden + malformed fixtures. Runs in default `pytest`.
- Integration (this host, marker `integration`): synthetic ADSBHub server →
  adapter → real pinned readsb → serving-api → assert: v2 envelope shape
  (`ac`, ms `now`, `alt_baro":"ground"` for ground movers, no `alt_geom`),
  injected 3 s upstream lag appears in served `seen_pos` (≥3 s), relay closes on
  upstream kill AND readsb aircraft go stale (no fresh `seen_pos` after failure),
  malformed lines dropped + counted while stream continues, `?status` health.
- Envelope fixture: the integration suite records the live jv2 response to
  `test/fixtures/api/jv2_envelope_recorded.json` (overwritten each run) and
  asserts the fields the tar1090-node proxy maps (plan §0 req 4) are present
  with correct types; the hand-synthesized `jv2_envelope.json` serves only as
  the unit tests' upstream mock (provenance in `test/fixtures/api/README.md`).
  (The proxy repo is not on this machine; its expectations are encoded from
  plan.md: `ac` array, ms `now`, `"ground"` string, `seen_pos` s.)
- Golden readsb fixtures + measured `source_timestamp_quantization_ms` (§10.4):
  integration test feeds known-timestamp lines and records observed quantization
  (expect: ms-level from SBS field 8, `seen` 0.1 s, `seen_pos` 1 ms resolution)
  into `docs/readsb-pin.md`.

## 8. Deployment & security (systemd translation of §20.1 — ADR-0003)

Three units, three system users (`retina-adapter`, `readsb`, `retina-api`),
each: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `MemoryMax` (adapter 256M,
readsb 1G, api 256M), `Restart=always` with rate limit. readsb unit:
`RuntimeDirectory=readsb`. No unit binds a non-loopback address. nginx configs
shipped but NOT enabled by install.sh: `mtls.conf` (server 443, client cert
required, proxy to 8080 — recommended Q5 answer) and `private-net.conf`
(bind private interface + allowlist). install.sh: creates users, installs venv +
services + units, builds readsb via build-readsb.sh, runs `--check-config`,
does NOT open firewall ports. A3's external port-scan check goes in the runbook.

## 9. Observability (A3)

Prometheus scrape targets: adapter :9101, serving-api :8080/metrics, readsb
via textfile `/run/readsb/stats.prom` (node_exporter textfile collector or
scrape-file exporter — config provided). Alert rules (§22.5 + §14.4):
- `ADSBHubDown` (two rules, one alertname, §14.4 row 1):
  `retina_adsb_provider_connected == 0` `for: 30s` (disconnect debounce), and
  `time() - retina_adsb_provider_last_record_timestamp_seconds > 30` with
  `for: 0s` (the age clause carries its own 30 s window, so it pages at ~30 s,
  not ~60 s).
- `CentralStateStalled`: `readsb_aircraft_with_position == 0 AND rate(retina_adsb_provider_bytes_total[1m]) > 0`
  for 15 s (§14.4 row 2 — byte-correlated, not naive quiet-sky).
- `ParseErrorSurge`: invalid/total ratio > 5% over 5 m AND total > 100
  (threshold undefined in spec — recorded choice, ADR-0006).
- `ADSBHubDegraded` (warning): no valid record for 10–30 s (§14.4 row 1
  degraded tier); `AdapterHealthEndpointDown`: adapter series absent.
- Host rules (require node_exporter/chrony, noted in the rules): `ClockOffset`
  (>100 ms/5 m warn, >500 ms/2 m crit, §22.5), `MemoryPressure` (process RSS
  >85% of MemoryMax 5 m), `DiskUsage` for /run.
Dashboards: `pipeline.json` (§22.4 item 1: connection state, rates by result,
reconnects, relay state, api requests), `freshness.json` (item 2: ingest delay
histogram percentiles — the Q7 chart vs the node 5 s budget — last-record age,
readsb position counts, serving upstream latency, /health verdicts; served
per-aircraft age has no readsb prometheus series — its distribution is
measured via the soak-checklist Q7 procedure over api responses, not a panel).
readsb's `stats.prom` series are 1-minute-window counts, not cumulative
counters — panels plot them raw (no `rate()`), and fast alerts key on
adapter-side cumulative counters. Node-cohort panels: N/A until Phase C
(ledger note).

## 10. Divergences & open questions carried in the ledger

- Interim REST surface replaces §11 gRPC contract (plan §0 decision) — spec
  amendment draft included; sunset when Phase B lands.
- Q5 unresolved: shipped default-closed + two boundary options; AC-S-01 not
  violated because nothing public exists until an operator enables a front.
- Q7 (ADSBHub latency vs 5 s node budget): measured continuously by
  `retina_adsb_provider_record_delay_seconds`; A3 characterization procedure in
  the freshness dashboard + runbook. Gates node cutover; cannot be answered
  without live entitled feed.
- §28 Phase 1 exit includes feed-gateway/receivers — out of Phase A scope,
  coverage comparison must use an independent view (ledger row).
- 72 h soak is operational, not a build artifact: soak checklist in runbooks.
