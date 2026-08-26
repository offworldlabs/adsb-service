# RETINA ADS-B Fusion and Serving System

**Implementation specification**  
**Document version:** 1.2  
**Status:** Proposed implementation baseline  
**Date:** 2026-08-26  
**Audience:** RETINA backend, node-platform, solver, infrastructure, security, QA, and operations teams

**Revision history:** 1.0 initial draft. 1.1 (2026-08-26): provider trust defaults and ADSBHub contribution rules (§4.1, §10.5, §12.4, §13.1); precision-association eligibility and quality-flag registry (§14.2); bandwidth budget, default region radius, and update decimation (§15.1, §15.8, §25.2); timestamp-quantization and jump-check fixes (§10.4, §12.3, §13.6, §16.4); cross-source history and queue/replay-ring fixes (§15.6, §16.3, §17); `GetRegionAtTime` (§11, §16.4); selection-boundary hysteresis (§13.4); gated acceptance criteria (§29); Mender/secure-element alignment (§20.5, §21.1). 1.2 (2026-08-26): binding requirements extracted to the companion `RETINA_ADSB_NORMATIVE_CORE.md`, which governs on conflict; this document now carries architecture, rationale, and operational guidance.

---

## 1. Purpose

This document specifies the production implementation of a RETINA-owned ADS-B data plane that:

1. ingests the worldwide ADSBHub SBS feed;
2. ingests ADS-B contributed by RETINA nodes with local receivers;
3. serves low-latency, geographically filtered aircraft state to RETINA nodes;
4. lets each node prefer its own fresh receiver data and fall back automatically to the central feed;
5. keeps the interface used by the RETINA radar/solver stable even if upstream providers or server internals change;
6. preserves existing node `readsb` and tar1090 behavior; and
7. exposes enough timing, provenance, health, and quality information for RETINA to reject stale or unsuitable data.

The binding requirement set for the first production release (“v1”) is maintained in the companion document `RETINA_ADSB_NORMATIVE_CORE.md` (“the core”), extracted from this specification. Implementation review and acceptance run against the core; on any conflict the core governs, and the conflict is a defect to fix in both documents in one change. This specification carries the architecture, rationale, data flows, configuration examples, and operational guidance behind those requirements — its requirement keywords are retained for context, and sections the core incorporates by reference (its Section 15) remain binding as cited. The two documents version in lockstep.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** express requirement strength. “Local” means data received directly by the node’s attached ADS-B receiver. “Central” means data delivered by the RETINA server, regardless of whether it originally came from ADSBHub or another RETINA receiver.

---

## 2. Executive design decision

The stable boundary is:

```text
RETINA solver and radar software
             │
             ▼
   retina-adsb-agent on localhost
             │
      ┌──────┴─────────┐
      ▼                ▼
local readsb       RETINA central stream
```

The solver MUST NOT connect directly to ADSBHub, central `readsb`, tar1090 JSON files, or a receiver TCP port. It MUST use the node-local `retina-adsb-agent` API.

The node agent maintains independent local and central candidates, applies explicit freshness and validity rules, retains short history, and serves one canonical interface. Loss or upgrade of the central system therefore cannot interrupt access to fresh local ADS-B. Loss of the local receiver causes automatic per-aircraft fallback to the central stream.

Existing node tar1090/readsb installations remain the receiver system of record. RETINA adds a non-invasive sidecar path; it does not replace tar1090 or place the central feed into the existing decoder.

---

## 3. Goals, non-goals, and design principles

### 3.1 Goals

| ID | Goal |
|---|---|
| G-01 | Preserve local ADS-B availability during every central outage or deployment. |
| G-02 | Prefer the freshest valid position; use local as the tie-breaker within a configurable equivalence window. |
| G-03 | Avoid polling latency in the operational path. |
| G-04 | Isolate ADSBHub-specific behavior to a server-side adapter. |
| G-05 | Accept contributed receiver data without exposing unauthenticated raw `readsb` ports to the Internet. |
| G-06 | Provide snapshots, ordered deltas, deletion/tombstone events, and deterministic recovery after reconnect. |
| G-07 | Make position age and time basis explicit in every solver-facing state. |
| G-08 | Retain enough node-local history to query/interpolate aircraft state at a radar observation timestamp. |
| G-09 | Support rolling upgrades and schema evolution without coordinated fleet downtime. |
| G-10 | Reuse maintained open-source ADS-B decoding and display components rather than writing a decoder. |

### 3.2 Non-goals for v1

- Building an ADS-B decoder or CPR decoder from scratch.
- Replacing the existing tar1090 user interface.
- MLAT calculation. Existing MLAT results may pass through if `readsb` exposes them, but generating MLAT is future work.
- A public, anonymous, Internet-facing flight-tracking API.
- Durable global flight history or analytics warehousing.
- Air-traffic-control-grade truth, certification, or safety-of-life use.
- Perfect knowledge of the original RF reception time for ADSBHub records. The upstream SBS feed cannot guarantee this.
- Combining the central feed into the node’s production receiver decoder.
- Extrapolating stale positions indefinitely.

### 3.3 Principles

1. **Local autonomy:** local receiver → agent → solver has no cloud dependency.
2. **Additive integration:** observe existing receiver outputs; do not take ownership of the existing decoder.
3. **Streaming first:** file/REST snapshots are bootstrap and diagnostic paths, not the live path.
4. **Freshness over origin:** a materially newer valid central position may beat local; local wins effectively equal comparisons.
5. **Paired fields stay paired:** latitude/longitude and speed/track observations are never assembled from different samples.
6. **Age is data:** observation age, receive time, server time, and time confidence travel with the state.
7. **Fail soft:** retain and age cached data, report degraded state, and reconnect; never block the localhost API on a remote dependency.
8. **Bounded resources:** all queues, caches, history buffers, and reconnect loops have explicit limits.
9. **Provider isolation:** no provider-specific value, field name, or failure mode crosses the canonical contract.
10. **Observable by default:** every pipeline boundary emits health, throughput, latency, drop, and freshness signals.

---

## 4. Open-source foundation

The project SHOULD pin known-good versions or immutable container digests and review upstream changes before promotion.

| Component/reference | RETINA use | Decision |
|---|---|---|
| [`readsb`](https://github.com/wiedehopf/readsb) | Decode local Beast traffic, aggregate server inputs, construct aircraft state, emit per-position JSON | Required |
| [`tar1090`](https://github.com/wiedehopf/tar1090) | Existing node visualization; optional central operator map | Preserve; not in solver path |
| [`combine1090`](https://github.com/wiedehopf/combine1090) | Pattern for a second non-interfering decoder instance | Reference/optional packaging |
| [`adsb-ultrafeeder`](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder) | Container/configuration model for multiple inputs and BeastReducePlus outputs | Reference; optional node packaging |
| [`adsblol/infra`](https://github.com/adsblol/infra) | Scalable ingest → hub → presentation separation | Server architecture reference |
| [`adsblol/api`](https://github.com/adsblol/api) | Geographic API and FastAPI implementation reference | Reference, not a required dependency |
| gRPC + Protocol Buffers | Versioned distribution and local interfaces | Required contract; implementation library selectable |
| Prometheus/OpenTelemetry | Metrics, logs, traces | Required instrumentation interfaces |

`readsb` supports multiple network connector types, a TCP JSON position stream, BeastReduce/BeastReducePlus, receiver IDs, and ingest-oriented modes. Its TCP JSON output can emit an aircraft object for every received position with a default per-aircraft minimum interval of zero; this is the required integration point instead of periodic `aircraft.json` polling. See the upstream [`readsb` README](https://github.com/wiedehopf/readsb/blob/dev/README.md) and [JSON format reference](https://github.com/wiedehopf/readsb/blob/dev/README-json.md).

The pinned server build MUST enable the upstream receiver-UUID feature (currently the `WITH_UUIDS=yes` build option where applicable), and CI MUST verify BeastReducePlus UUID preservation with a real encoded fixture. RETINA MUST treat upstream build flags as version-specific and revalidate them on upgrade.

tar1090 is a UI for an existing decoder, not a replacement for one. The RETINA data plane MUST therefore remain separate from tar1090’s browser/file refresh behavior. `combine1090` demonstrates the intended non-interfering second-instance pattern, while `adsb-ultrafeeder` demonstrates packaged management of `readsb`, tar1090, receiver inputs, and reduced Beast feeds.

The ADSB.lol infrastructure separates public ingest, hub aggregation, presentation, and API roles. RETINA v1 can deploy these roles on one host, but MUST preserve the same logical boundaries so they can be scaled independently later.

### 4.1 Licensing and data-use gate

Before production rollout, release engineering MUST create a software bill of materials and record the license, version, source URL, modifications, and redistribution obligations for every dependency and container image. Legal review MUST confirm deployment and redistribution obligations for the selected builds.

ADSBHub currently states that the aggregate SBS feed is available at `data.adsbhub.org:5002`, is restricted to configured source IPs, requires the user to contribute at least one station, and may be processed, published, or used commercially. See [ADSBHub’s current feed terms](https://www.adsbhub.org/howtogetdata.php). These terms are an external dependency and MUST be rechecked before launch and at least annually. ADSBHub also disclaims feed accuracy; RETINA MUST treat it as untrusted sensor data.

The contribution requirement MUST be satisfied by a designated receiver that feeds ADSBHub directly from its own decoder. It MUST NOT be satisfied by re-feeding the RETINA hub aggregate or any path containing central or redistributed data; doing so would create an external feedback loop through the provider (§7.4). The designated station and its data path MUST be recorded in deployment documentation. Because provider behavior is isolated behind the adapter (G-04), a coverage comparison against at least one alternative aggregator SHOULD be recorded before launch and revisited whenever provider coverage limits solver validation.

---

## 5. System context and trust boundaries

```text
                              INTERNET

   ADSBHub SBS/TCP                         RETINA receiver nodes
 data.adsbhub.org:5002                  BeastReducePlus over mTLS
           │                                      │
           ▼                                      ▼
 ┌──────────────────────────────────────────────────────────────┐
 │                    RETINA SERVER PLANE                       │
 │                                                              │
 │  adsbhub-adapter       feed-gateway                          │
 │  reconnect/validate    auth/rate-limit/UUID validation       │
 │          │                    │                               │
 │          └──────────┬─────────┘                               │
 │                     ▼                                         │
 │            readsb aggregation hub                             │
 │                     │ TCP JSON per position                   │
 │                     ▼                                         │
 │        retina-adsb-server state/history/router                │
 │                     │                                         │
 │               gRPC + TLS :443                                │
 └─────────────────────┼─────────────────────────────────────────┘
                       │ outbound connection from node
                       ▼
 ┌──────────────────────── RETINA NODE ─────────────────────────┐
 │                                                              │
 │  SDR → existing readsb → existing tar1090                    │
 │             │ Beast output                                   │
 │             ▼                                                │
 │      readsb-retina sidecar ──BRP──► TLS feed-forwarder       │
 │             │ JSON                       │                    │
 │             ▼                            └────► central       │
 │      retina-adsb-agent ◄──────── central gRPC stream         │
 │             │                                                │
 │             ▼ Unix socket / loopback gRPC                    │
 │      RETINA solver/radar                                     │
 └──────────────────────────────────────────────────────────────┘
```

Trust boundaries:

- ADSBHub and all RF-derived content are untrusted input.
- A node identity is trusted only after mutual TLS authentication and authorization.
- A valid node certificate does not make its ADS-B values trustworthy; quality and plausibility checks still apply.
- The server-to-node distribution endpoint is authenticated with TLS; production nodes MUST validate the server name and trust chain.
- The solver-facing API is local-only. A Unix-domain socket is preferred; loopback TCP with OS firewall restrictions is acceptable.

---

## 6. Component inventory and responsibilities

### 6.1 Server components

#### S1. `adsbhub-adapter`

Responsibilities:

- maintain one long-lived outbound TCP connection to `data.adsbhub.org:5002` from an ADSBHub-authorized stable egress IP;
- use exponential backoff with jitter after failure;
- frame SBS/BaseStation lines with a maximum line length;
- validate field count, message type, ICAO syntax, numeric ranges, timestamp syntax, and UTF-8/ASCII safety;
- drop and count malformed input without terminating the process;
- retain provider timestamps as diagnostic metadata when valid;
- expose one bounded loopback-only SBS relay listener; `readsb-hub` connects to it with an `sbs_in` network connector;
- close the active internal relay connection when the upstream session fails so `readsb-hub` does not mistake an idle dead provider for a live source;
- expose connection state, byte/line counts, parse errors, reconnects, last-good-record time, and apparent provider delay;
- support capture of a bounded redacted diagnostic sample only when explicitly enabled.

It MUST NOT expose an Internet listener or define canonical downstream schemas. ADSBHub host, port, and parsing behavior MUST be configuration, not compiled constants.

#### S2. `retina-feed-gateway`

Responsibilities:

- terminate mutual TLS for node-contributed BeastReducePlus streams;
- map the client certificate identity to exactly one `node_id` and expected receiver UUID;
- validate Beast framing and the initial/subsequent receiver UUID markers;
- reject a UUID/certificate mismatch;
- enforce connection, byte-rate, message-rate, burst, idle, and malformed-frame limits;
- attach authenticated connection metadata for logs/metrics;
- forward only validated binary Beast traffic to the internal `readsb` Beast input;
- isolate slow or abusive clients so they cannot block other inputs.

The gateway MUST NOT be a general TCP tunnel. A compromised node MUST NOT be able to select an arbitrary internal destination.

#### S3. `readsb-hub`

Responsibilities:

- ingest validated SBS from the ADSBHub adapter;
- ingest validated BeastReducePlus from RETINA nodes;
- decode and deduplicate ADS-B/Mode-S messages;
- construct the best available central aircraft state;
- preserve receiver identifiers where supported;
- emit a per-position TCP JSON object stream to `retina-adsb-server`;
- optionally produce tar1090 JSON/history for operator visualization;
- expose internal statistics.

`readsb-hub` MUST bind all raw input/output and API ports to a private interface or loopback. No `readsb` listener may be published directly to the Internet.

The built-in `readsb` geographic HTTP API and tar1090 output MAY be exposed on an authenticated operator network for diagnostics. They are not a supported RETINA node/solver contract and no production node may depend on their field stability or refresh interval.

#### S4. `retina-adsb-server`

Responsibilities:

- parse and normalize `readsb` TCP JSON objects;
- validate canonical ranges and reject impossible/non-finite values;
- compute explicit field groups, ages, time confidence, and provenance;
- maintain the live central aircraft table and bounded recent position history;
- assign server epoch/revision identifiers;
- spatially index aircraft and route region-filtered snapshots/deltas;
- serve the versioned gRPC distribution API;
- send deletion tombstones when an aircraft leaves a subscription or expires;
- implement flow control, per-node queue limits, and slow-subscriber eviction;
- expose health, readiness, metrics, structured logs, and traces;
- never block ingest because a subscriber is slow.

#### S5. Edge proxy/load balancer

Responsibilities:

- publish distribution gRPC on TCP 443;
- terminate or pass through TLS according to the certificate design;
- apply connection limits and safe HTTP/2 settings;
- drain connections during deployment;
- route only declared services;
- preserve client identity where needed.

For v1, gRPC TLS MAY terminate in `retina-adsb-server` directly. Node-feed mTLS authentication MUST still terminate at `retina-feed-gateway` because it validates ADS-B feed identity.

### 6.2 Node components

#### N1. Existing receiver stack

The installed SDR → `readsb`/dump1090-fa → tar1090 path remains owned by the existing node installation. RETINA MUST NOT change its SDR device ownership, tar1090 paths, public UI, or primary ports unless the operator explicitly selects the direct-integration mode.

#### N2. `readsb-retina` sidecar

Responsibilities:

- connect read-only to the existing local Beast output, normally `127.0.0.1:30005`;
- decode a private copy of local receiver data;
- expose per-position JSON on a RETINA-only loopback port;
- emit BeastReducePlus with the stable receiver UUID to a local TLS forwarder;
- remain independent of the existing tar1090 data files and refresh period.

The sidecar MUST use `--net-only`; it MUST NOT open or claim the SDR.

If the current decoder already exposes a reliable loopback TCP JSON stream and a separately managed BeastReducePlus feed without configuration risk, operators MAY omit the second decoder. The default deployment remains the sidecar because it minimizes coupling and port/configuration collision.

#### N3. `retina-feed-forwarder`

Responsibilities:

- accept BeastReducePlus only on loopback from `readsb-retina`;
- create an outbound mTLS connection to the server feed gateway;
- validate the server name/certificate;
- reconnect with bounded exponential backoff and jitter;
- expose connection/byte/drop metrics;
- use a bounded memory buffer and drop oldest frames rather than write an unbounded disk queue.

It MUST forward only local receiver data. The central stream MUST NEVER be re-fed to the server.

#### N4. `retina-adsb-agent`

Responsibilities:

- consume the sidecar’s local TCP JSON stream;
- consume the central gRPC stream;
- keep local and central candidates separate by aircraft and field group;
- select the freshest valid candidate using Section 13;
- retain bounded position history;
- provide a stable local gRPC API to the solver;
- report staleness, source, timing confidence, and selection reason;
- continue serving local/cached state during every remote outage;
- reconnect/resnapshot automatically;
- expose readiness, metrics, structured logs, and a diagnostic status endpoint.

The agent is the only ADS-B dependency the solver is allowed to know.

---

## 7. Node integration modes

### 7.1 Mode A: isolated sidecar (required default)

```text
existing readsb --net-bo-port 30005
              │
              ▼
readsb-retina --net-only --net-connector 127.0.0.1,30005,beast_in
              ├── JSON :32005 → retina-adsb-agent
              └── BeastReducePlus → loopback forwarder → central
```

Advantages: no SDR contention, no tar1090 change, separate update cadence, and rollback by stopping RETINA services. Cost: one additional low-overhead decode process.

### 7.2 Mode B: direct existing-decoder integration (optional)

Use only after automated discovery and a node-specific change review. The installer may add a loopback JSON listener and an outbound BeastReducePlus connector to the existing `readsb`. It MUST preserve all existing command-line arguments and service overrides and MUST validate that no port is already in use.

### 7.3 Mode C: ultrafeeder-managed node (optional future standard)

Nodes already managed with `adsb-ultrafeeder` may express the equivalent input/output connectors in that deployment’s supported configuration. The canonical agent API and fusion policy do not change.

### 7.4 Prohibited topology

The central feed MUST NOT be connected to the same local decoder instance that supplies the “local” candidate. Doing so erases provenance and permits a delayed copy of a node’s own message to masquerade as fresh local data.

The same prohibition applies externally: the ADSBHub contribution requirement MUST NOT be met with hub-aggregate or central-derived data (§4.1), which would create a second feedback path through the provider.

---

## 8. Data-flow sequences

### 8.1 Local position to solver

1. Aircraft broadcasts a position.
2. Existing receiver decoder outputs Beast data.
3. `readsb-retina` decodes the position and writes one JSON line.
4. The agent validates and records a LOCAL position candidate with its `seen_pos`-derived age.
5. The selector recomputes the winning field groups.
6. If solver-visible state changes, the agent increments `local_revision` and publishes a delta to local watchers.
7. The same RF stream may be reduced and forwarded centrally, but this is not on the solver’s critical path.

### 8.2 ADSBHub position to node

1. `adsbhub-adapter` receives and validates an SBS line.
2. `readsb-hub` updates central aircraft state.
3. Its TCP JSON position output reaches `retina-adsb-server`.
4. The server normalizes it, updates its revision, and routes it to matching subscriptions.
5. The node agent stores it as a CENTRAL candidate.
6. The selector uses it only if eligible under the freshness/quality rules.

### 8.3 Node-contributed position returning to that node

The node’s raw local message may reach the central hub and return through the distribution stream. It remains a CENTRAL candidate. The original LOCAL candidate will normally win because it is fresher or falls within the local-preference window. The system MUST NOT attempt to infer that the returned record is identical unless receiver provenance is available; correctness cannot depend on that inference.

### 8.4 Startup or reconnect

1. Agent opens the central stream and sends `Subscribe`.
2. Server sends `SnapshotBegin`, chunks, and `SnapshotEnd` against one barrier revision.
3. Agent builds a shadow central table.
4. On valid `SnapshotEnd`, agent atomically swaps the table and applies buffered deltas.
5. A malformed/incomplete snapshot is discarded; the previously committed table continues aging.
6. Local candidates remain live throughout.

---

## 9. Canonical identity and units

### 9.1 Aircraft key

`aircraft_key` is a string:

- ICAO addresses: exactly six lowercase hexadecimal characters, e.g. `a1b2c3`.
- Non-ICAO addresses exposed by `readsb`: preserve the leading `~` followed by six lowercase hexadecimal characters, e.g. `~abc123`.
- Invalid, empty, broadcast, or malformed keys are rejected.

No business logic may assume every key is a registered ICAO address. The key is an observation correlation identifier, not proof of identity.

### 9.2 Units

| Value | Unit/representation |
|---|---|
| Latitude/longitude | WGS-84 decimal degrees, `double` |
| Altitude | Feet, signed integer; geometric and barometric kept separate |
| Ground speed/air speed | Knots |
| Track/heading | Degrees in `[0, 360)` |
| Vertical rate | Feet per minute |
| Containment radius | Meters |
| Distance query radius | Meters in canonical API; config may accept km/nmi suffixes |
| Absolute time | Signed Unix epoch nanoseconds (`int64`) |
| Durations/ages | Milliseconds on wire where noted; seconds in Prometheus metric names |

Values MUST NOT silently convert altitude reference, heading reference, or speed type.

### 9.3 Field groups

Fusion occurs on atomic groups, not arbitrary scalar fields:

| Group | Atomic members | Rationale |
|---|---|---|
| `POSITION` | latitude, longitude, position integrity fields | Never combine coordinates from different observations |
| `BARO_ALTITUDE` | barometric altitude or on-ground state | Preserve altitude type |
| `GEOM_ALTITUDE` | geometric altitude | Preserve altitude type |
| `GROUND_VECTOR` | ground speed, true track | Same kinematic sample where possible |
| `VERTICAL_RATE_BARO` | barometric vertical rate | Separate reference |
| `VERTICAL_RATE_GEOM` | geometric vertical rate | Separate reference |
| `IDENTITY` | callsign | Slow-changing metadata |
| `SQUAWK_EMERGENCY` | squawk, emergency, SPI/alert | Safety/status group |
| `NAVIGATION` | selected altitude/heading/modes | Optional v1 pass-through |
| `AIR_DATA` | IAS, TAS, Mach, headings | Optional v1 pass-through |

An output aircraft state may legitimately contain groups from different sources and times. Every populated group MUST include its own metadata.

---

## 10. Time model

### 10.1 Required timestamps

Each canonical field group carries:

- `observed_at_unix_ns`: best estimate of when the underlying observation became valid at its source;
- `source_received_at_unix_ns`: time the source adapter/decoder received or emitted it, if known;
- `server_ingested_at_unix_ns`: central server receipt time, for central records;
- `server_emitted_at_unix_ns`: central stream serialization time;
- `node_received_at_unix_ns`: assigned locally and not normally retransmitted;
- `age_ms_at_emit`: server’s conservative age estimate when emitting the group;
- `time_basis` and `time_uncertainty_ms`.

On the wire, `age_ms_at_emit`, `time_basis`, `time_uncertainty_ms`, and (for central records) `server_emitted_at_unix_ns` MUST be populated. The remaining absolute timestamps are optional fields and SHOULD be omitted from streamed upserts when reconstructible: an absent `observed_at_unix_ns` is defined as `server_emitted_at_unix_ns − age_ms_at_emit` within the stated uncertainty, and consumers MUST perform that reconstruction. This keeps per-update `ObservationMeta` compact (§25.2 bandwidth budget); diagnostic paths and snapshots MAY carry the full set.

### 10.2 Time bases

```text
TIME_BASIS_UNSPECIFIED
TIME_BASIS_RECEIVER_OBSERVATION   receiver/decoder timestamp believed synchronized
TIME_BASIS_READSB_SEEN_AGE        readsb `now - seen_pos` or equivalent
TIME_BASIS_PROVIDER_TIMESTAMP     upstream SBS timestamp passed sanity checks
TIME_BASIS_INGEST_RECEIPT         RETINA process receipt; upstream transit unknown
TIME_BASIS_VALUE_CHANGE_ESTIMATE  adapter inferred time from a state change
```

### 10.3 Monotonic clocks

All timeout, age-since-receipt, retry, and cache-expiry logic within one process MUST use a monotonic clock. Wall time is used only for cross-process timestamps and operator output.

Every host MUST run a monitored time-synchronization service such as chrony. Alert thresholds:

- warning: absolute clock offset > 100 ms for 5 minutes;
- critical: absolute clock offset > 500 ms for 2 minutes.

Clock corrections MUST NOT make a cached field younger. Store the initial age plus monotonic receipt time and compute:

```text
effective_age(now) = age_at_local_receipt + monotonic_elapsed_since_receipt
```

### 10.4 `readsb` JSON mapping

For a position object:

```text
observed_at = json.now - json.seen_pos
```

The adapter MUST reject negative `seen_pos`, clamp tiny negative floating-point artifacts to zero, and reject values above the configured hard maximum. `seen_pos` is distinct from `seen`; a recent non-position message MUST NOT refresh position time. The upstream JSON documentation explicitly defines this distinction.

When exact per-field message ages are not available, ancillary fields carried in the position object use the position observation time and `TIME_BASIS_VALUE_CHANGE_ESTIMATE` or `TIME_BASIS_READSB_SEEN_AGE`, with an uncertainty flag. Consumers MUST NOT treat this as RF-exact timing.

The timestamp resolution of the pinned `readsb` JSON output MUST be measured and recorded as `source_timestamp_quantization_ms` (fields in the `now`/`seen_pos` family are commonly 0.1 s granular). `time_uncertainty_ms` for `TIME_BASIS_READSB_SEEN_AGE` groups MUST be at least this quantization, and every constant bounded below by it (`exact_sample_window_ms` §16.4, `same_source_reorder_tolerance_ms` §13.6) MUST be validated against the measured value in CI.

### 10.5 Central network-delay penalty

The node measures distribution round-trip time with `Probe` at least every 30 seconds. For a central group:

```text
age_at_local_receipt = server_age_ms_at_emit
                     + clamp(last_rtt_ms / 2, 0, max_network_penalty_ms)
```

If no valid RTT sample exists, `unknown_network_penalty_ms` is used (default 250 ms). This conservative penalty prevents remote data from appearing newer merely because transport time is unknown.

ADSBHub’s upstream latency may be unknowable. Central records derived from it MUST carry `upstream_latency_known=false`. The server MUST apply a configured provider age penalty (`provider_age_penalty_ms`, default 1,000 ms, included in `age_ms_at_emit`) and a time-uncertainty floor (`ingest_uncertainty_floor_ms`, default 1,000 ms, applied to `time_uncertainty_ms`) to every group whose timing derives from `TIME_BASIS_INGEST_RECEIPT` or that cannot be attributed to a receiver with known timing (§13.1). These defaults stand until replaced by the measured characterization of Phase 5. Independently of the arithmetic, a group carrying `provider_latency_unknown` MUST NOT be classed `FRESH` (§14.1) and is never precision-eligible (§14.2), so boundary cases cannot promote it.

---

## 11. Canonical Protocol Buffer contract

The repository MUST keep the contract in `proto/retina/adsb/v1/adsb.proto`, generate code in CI, and run binary compatibility checks. The following is the normative v1 shape; exact comments/options may be expanded without changing field numbers.

```protobuf
syntax = "proto3";

package retina.adsb.v1;

enum SourceClass {
  SOURCE_CLASS_UNSPECIFIED = 0;
  SOURCE_CLASS_LOCAL_RECEIVER = 1;
  SOURCE_CLASS_RETINA_CENTRAL = 2;
  SOURCE_CLASS_RETINA_RECEIVER_NETWORK = 3;
  SOURCE_CLASS_ADSBHUB = 4;
  SOURCE_CLASS_MLAT = 5;
  SOURCE_CLASS_TISB = 6;
  SOURCE_CLASS_OTHER = 7;
}

enum TimeBasis {
  TIME_BASIS_UNSPECIFIED = 0;
  TIME_BASIS_RECEIVER_OBSERVATION = 1;
  TIME_BASIS_READSB_SEEN_AGE = 2;
  TIME_BASIS_PROVIDER_TIMESTAMP = 3;
  TIME_BASIS_INGEST_RECEIPT = 4;
  TIME_BASIS_VALUE_CHANGE_ESTIMATE = 5;
}

enum FreshnessClass {
  FRESHNESS_UNSPECIFIED = 0;
  FRESHNESS_FRESH = 1;
  FRESHNESS_USABLE = 2;
  FRESHNESS_STALE = 3;
  FRESHNESS_EXPIRED = 4;
}

enum SelectionReason {
  SELECTION_REASON_UNSPECIFIED = 0;
  SELECTION_REASON_ONLY_VALID_CANDIDATE = 1;
  SELECTION_REASON_MATERIALLY_FRESHER = 2;
  SELECTION_REASON_LOCAL_TIE_BREAK = 3;
  SELECTION_REASON_BETTER_INTEGRITY = 4;
  SELECTION_REASON_CURRENT_SOURCE_HYSTERESIS = 5;
  SELECTION_REASON_OTHER_SOURCE_EXPIRED = 6;
}

message ObservationMeta {
  SourceClass source_class = 1;
  string source_id = 2;
  string receiver_id = 3;
  optional int64 observed_at_unix_ns = 4;
  optional int64 source_received_at_unix_ns = 5;
  optional int64 server_ingested_at_unix_ns = 6;
  optional int64 server_emitted_at_unix_ns = 7;
  uint32 age_ms_at_emit = 8;
  TimeBasis time_basis = 9;
  uint32 time_uncertainty_ms = 10;
  bool upstream_latency_known = 11;
  uint64 source_sequence = 12;
  SelectionReason selection_reason = 13;
  FreshnessClass freshness = 14;
}

message Position {
  double latitude_deg = 1;
  double longitude_deg = 2;
  optional uint32 nic = 3;
  optional uint32 nac_p = 4;
  optional uint32 sil = 5;
  optional double containment_radius_m = 6;
  ObservationMeta meta = 7;
}

message Altitude {
  optional sint32 barometric_ft = 1;
  optional sint32 geometric_ft = 2;
  optional bool on_ground = 3;
  ObservationMeta barometric_meta = 4;
  ObservationMeta geometric_meta = 5;
}

message Velocity {
  optional float ground_speed_kt = 1;
  optional float true_track_deg = 2;
  optional sint32 barometric_vertical_rate_fpm = 3;
  optional sint32 geometric_vertical_rate_fpm = 4;
  ObservationMeta ground_vector_meta = 5;
  ObservationMeta barometric_rate_meta = 6;
  ObservationMeta geometric_rate_meta = 7;
}

message Identity {
  optional string callsign = 1;
  optional string squawk = 2;
  optional string category = 3;
  optional string emergency = 4;
  ObservationMeta callsign_meta = 5;
  ObservationMeta status_meta = 6;
}

message AircraftState {
  string aircraft_key = 1;
  optional Position position = 2;
  optional Altitude altitude = 3;
  optional Velocity velocity = 4;
  optional Identity identity = 5;
  uint64 revision = 6;
  int64 generated_at_unix_ns = 7;
  uint32 position_age_ms = 8;
  FreshnessClass position_freshness = 9;
  bool usable_for_precision_association = 10;
  repeated string quality_flags = 11;
}

message Region {
  double center_latitude_deg = 1;
  double center_longitude_deg = 2;
  uint32 radius_m = 3;
  optional sint32 minimum_altitude_ft = 4;
  optional sint32 maximum_altitude_ft = 5;
}

message Subscribe {
  string node_id = 1;
  Region region = 2;
  uint32 protocol_minor = 3;
  repeated string requested_features = 4;
  optional string resume_server_epoch = 5;
  optional uint64 resume_event_id = 6;
}

message SnapshotBegin {
  string snapshot_id = 1;
  string server_epoch = 2;
  uint64 barrier_event_id = 3;
  uint64 subscription_sequence = 4;
}

message SnapshotChunk {
  string snapshot_id = 1;
  repeated AircraftState aircraft = 2;
  uint64 subscription_sequence = 3;
}

message SnapshotEnd {
  string snapshot_id = 1;
  uint64 barrier_event_id = 2;
  uint64 subscription_sequence = 3;
  uint32 aircraft_count = 4;
  bytes content_sha256 = 5;
}

message AircraftUpsert {
  AircraftState state = 1;
  uint64 server_event_id = 2;
  uint64 subscription_sequence = 3;
}

message AircraftDelete {
  string aircraft_key = 1;
  string reason = 2;
  uint64 server_event_id = 3;
  uint64 subscription_sequence = 4;
}

message Heartbeat {
  string server_epoch = 1;
  int64 server_time_unix_ns = 2;
  uint64 last_server_event_id = 3;
  uint64 subscription_sequence = 4;
}

message ClientMessage {
  oneof payload {
    Subscribe subscribe = 1;
    uint64 acknowledged_subscription_sequence = 2;
  }
}

message ServerMessage {
  oneof payload {
    SnapshotBegin snapshot_begin = 1;
    SnapshotChunk snapshot_chunk = 2;
    SnapshotEnd snapshot_end = 3;
    AircraftUpsert upsert = 4;
    AircraftDelete delete = 5;
    Heartbeat heartbeat = 6;
  }
}

message ProbeRequest { bytes nonce = 1; }
message ProbeResponse {
  bytes nonce = 1;
  int64 server_receive_unix_ns = 2;
  int64 server_send_unix_ns = 3;
}

service DistributionService {
  rpc Stream(stream ClientMessage) returns (stream ServerMessage);
  rpc Probe(ProbeRequest) returns (ProbeResponse);
}

message SnapshotRequest {
  optional Region region = 1;
  bool include_expired = 2;
}

message SnapshotResponse {
  repeated AircraftState aircraft = 1;
  uint64 local_revision = 2;
  int64 generated_at_unix_ns = 3;
}

message WatchRequest {
  optional Region region = 1;
  uint64 after_local_revision = 2;
}

message LocalAircraftDelete {
  string aircraft_key = 1;
  string reason = 2;
}

message LocalAircraftEvent {
  oneof payload {
    AircraftState upsert = 1;
    LocalAircraftDelete delete = 2;
  }
  uint64 local_revision = 3;
}

enum EstimateMethod {
  ESTIMATE_METHOD_UNSPECIFIED = 0;
  ESTIMATE_METHOD_EXACT_SAMPLE = 1;
  ESTIMATE_METHOD_INTERPOLATED = 2;
  ESTIMATE_METHOD_EXTRAPOLATED = 3;
  ESTIMATE_METHOD_NEAREST_SAMPLE = 4;
  ESTIMATE_METHOD_UNAVAILABLE = 5;
}

message AtTimeRequest {
  string aircraft_key = 1;
  int64 target_time_unix_ns = 2;
  bool allow_extrapolation = 3;
  bool allow_nearest_sample = 4;
}

message AtTimeResponse {
  optional AircraftState state = 1;
  EstimateMethod method = 2;
  uint32 temporal_distance_ms = 3;
  optional double estimated_horizontal_uncertainty_m = 4;
  repeated string warnings = 5;
}

message RegionAtTimeRequest {
  optional Region region = 1;
  int64 target_time_unix_ns = 2;
  bool allow_extrapolation = 3;
  bool allow_nearest_sample = 4;
}

message AtTimeResult {
  string aircraft_key = 1;
  optional AircraftState state = 2;
  EstimateMethod method = 3;
  uint32 temporal_distance_ms = 4;
  optional double estimated_horizontal_uncertainty_m = 5;
  repeated string warnings = 6;
}

message RegionAtTimeResponse {
  repeated AtTimeResult results = 1;
  int64 generated_at_unix_ns = 2;
}

service LocalAircraftService {
  rpc GetSnapshot(SnapshotRequest) returns (SnapshotResponse);
  rpc Watch(WatchRequest) returns (stream LocalAircraftEvent);
  rpc GetAtTime(AtTimeRequest) returns (AtTimeResponse);
  rpc GetRegionAtTime(RegionAtTimeRequest) returns (RegionAtTimeResponse);
}
```

### 11.1 Schema evolution rules

- Major package version changes only for wire-incompatible semantic changes.
- Existing field numbers MUST NOT change or be reused.
- Deleted fields and names MUST be marked `reserved`.
- New enum values append new numbers; receivers MUST tolerate unknown values.
- Consumers MUST ignore unknown fields.
- The server supports current and previous minor versions during rolling fleet upgrades.
- CI MUST run Protobuf lint, breaking-change detection, generated-code freshness, and cross-version serialization tests.

These rules follow the official [Protocol Buffers compatibility guidance](https://protobuf.dev/programming-guides/proto3/).

### 11.2 Node contribution transport

- Public endpoint: TCP 7444, TLS 1.3 preferred (TLS 1.2 minimum only where approved), mutual certificate authentication.
- ALPN/application identifier: `retina-adsb-feed/1` when supported by the selected TLS stack.
- Payload after TLS establishment: an unmodified continuous BeastReducePlus byte stream; no custom line framing, application compression, or multiplexing.
- One TLS connection represents one authenticated node/receiver pair. Multiple receivers require separate authorized identities or explicit receiver mappings.
- The gateway requires a valid receiver UUID marker within the first 64 KiB and 5 seconds, then enforces that UUID for the session.
- A certificate/UUID mismatch, unsupported framing, oversized undecodable prefix, or authentication failure closes the connection without forwarding bytes.
- Ordinary Beast heartbeats may keep an active feed session alive but do not replace TLS authentication.
- The forwarder keeps only a short bounded memory queue (default 1 MiB or 2 seconds, whichever is smaller). It MUST discard expired backlog rather than deliver it after reconnect as live traffic.

The exact BeastReducePlus wire behavior is pinned to and tested against the selected `readsb` build. RETINA does not define a competing ADS-B binary format.

### 11.3 Distribution transport

- Public endpoint: gRPC over HTTP/2 on TCP 443 with mutual TLS for production nodes.
- Node certificate identity is authorized before a stream or snapshot is served.
- TLS termination must preserve the authenticated identity to the application using a trusted mechanism; untrusted forwarded identity headers are prohibited.
- Server and client support the current and previous approved certificate chains during rotation.
- gRPC compression is disabled by default for live updates; snapshot-chunk compression MUST be measured during Phase 1 and SHOULD be enabled where measurement shows net benefit.
- Maximum encoded message is 4 MiB; the server targets chunks no larger than 512 KiB.

---

## 12. Validation and normalization

### 12.1 General rules

At every untrusted-input boundary:

- reject NaN and infinity;
- enforce maximum message/line/frame size before allocation;
- bound collection counts and string lengths;
- normalize callsign by trimming right padding but preserve internal characters;
- lowercase aircraft keys and receiver UUIDs;
- validate UTF-8; replace or reject control characters;
- retain an error reason counter without logging full attacker-controlled payloads;
- never terminate the stream because one aircraft record is malformed.

### 12.2 Range checks

| Field | Valid range/default action |
|---|---|
| latitude | `[-90, 90]`; reject outside |
| longitude | `[-180, 180]`; normalize +180 to -180 consistently |
| barometric/geometric altitude | `[-2,000, 100,000] ft`; configurable; reject outside |
| ground speed | `[0, 1,500] kt`; reject outside |
| track/heading | normalize finite value modulo 360; reject extreme parser overflow |
| vertical rate | `[-20,000, 20,000] ft/min`; reject outside |
| callsign | max 16 UTF-8 bytes after trim; v1 SHOULD restrict to expected printable subset |
| squawk | exactly four octal digits |
| NIC/NAC/SIL | values supported by the canonical enum/range; unknown is absent, not zero-by-default |

### 12.3 Plausibility and jump checks

The state engine and agent maintain the last accepted position per source candidate. A new position is hard-rejected from the live table if any applies:

- invalid coordinates;
- age exceeds `position_hard_reject_ms` (default 30,000 ms);
- timestamp is more than `future_tolerance_ms` (default 1,000 ms) ahead;
- implied speed exceeds 1,500 kt for airborne or 250 kt for on-ground, after a 250 m spatial tolerance;
- identical source sequence moves backward beyond the permitted reordering window;
- configured geographic exclusion applies.

Implied speed MUST be computed over `dt_effective = max(observed_dt, jump_check_dt_floor_ms, u_a + u_b)`, where `u_a` and `u_b` are the two samples’ `time_uncertainty_ms` and `jump_check_dt_floor_ms` defaults to 250 ms. Without this, an inter-sample interval smaller than its own timing uncertainty produces false teleport rejections (two samples 500 m apart with an estimated 100 ms gap but 1 s true gap would read as ≈4,900 kt).

A single jump failure increments a counter and is retained only in a bounded diagnostic ring. Two or more mutually consistent subsequent samples MAY re-establish the track. Quality/integrity flags may mark a candidate unusable even when coordinates are in range.

### 12.4 SBS parsing

The ADSBHub adapter MUST support only explicitly tested BaseStation message variants. Unknown message types are counted and ignored until a fixture and mapping are added. CSV parsing MUST correctly handle empty fields and MUST NOT use naive string splitting if quoting is possible in the selected format. Provider timestamps are parsed in a configured timezone (default UTC) and never trusted without sanity checks.

SBS/BaseStation carries no NIC/NACp/SIL integrity fields and does not distinguish direct ADS-B from MLAT- or TIS-B-derived positions mixed upstream. ADSBHub-derived groups therefore carry the `provider_surveillance_type_unknown` quality flag, never populate integrity fields, and never satisfy precision eligibility (§14.2).

### 12.5 `readsb` mapping

The mapping layer MUST have golden fixtures for every used field in the pinned `readsb` version. It MUST explicitly support:

- `hex`, including `~` non-ICAO prefix;
- `now`, `seen`, `seen_pos`;
- `lat`, `lon`;
- numeric altitude and the string `ground`;
- `alt_geom`, `gs`, `track`, `baro_rate`, `geom_rate`;
- `flight`, `squawk`, `emergency`, `category`;
- `nic`, `nac_p`, `sil`, `sil_type`, `rc`;
- `type`, `mlat`, and `tisb` provenance hints;
- omitted fields and added unknown fields.

An upstream upgrade is blocked if golden fixtures or a 30-minute shadow replay show incompatible mapping, parse-error increase, or freshness regression.

---

## 13. Source selection and fusion

### 13.1 Central fusion

`readsb-hub` is responsible for Mode-S decoding, CPR resolution, message deduplication, and construction of central aircraft state. RETINA MUST NOT reimplement those algorithms in v1.

`retina-adsb-server` applies a second, simpler state layer:

1. validate normalized output;
2. reject out-of-order live replacements beyond the allowed reorder tolerance;
3. update only field groups present in the new object;
4. retain each group’s independent observation metadata;
5. expire groups by their TTL;
6. publish an upsert only when solver-visible state or metadata changes materially.

Central source provenance may be coarse because aggregated `readsb` output may not identify the receiver responsible for every selected field. It MUST truthfully report `SOURCE_CLASS_RETINA_CENTRAL`; it MUST NOT invent an ADSBHub or node receiver ID. Where `readsb` provides reliable receiver/type provenance, the server MAY populate the more specific fields.

For every emitted group the server MUST additionally classify timing trust from the best available provenance (`readsb` `type`, receiver id): groups attributable to an authenticated RETINA receiver carry transport-derived uncertainty; groups attributable to the SBS provider, and groups that cannot be attributed at all, carry the §10.5 penalty and floor plus the `provider_latency_unknown` and (where applicable) `provider_surveillance_type_unknown` quality flags. Which attributions the pinned `readsb` build supports MUST be established with the §12.5 fixtures; when attribution is unavailable, the conservative treatment applies to all central groups.

### 13.2 Node candidate tables

For every `(aircraft_key, field_group)`, the agent keeps at most:

- one current LOCAL candidate;
- one current CENTRAL candidate;
- the currently selected candidate;
- a bounded recent sample history for positions.

Candidates are never overwritten by an older observation from the same source, except that an equal observation may update receipt diagnostics without refreshing observation age.

### 13.3 Eligibility

A candidate is eligible if:

- it passed validation and plausibility checks;
- it has all required atomic members for the group;
- its effective age is at or below the group’s selection TTL;
- no hard quality flag excludes it;
- its observation time is not unacceptably in the future.

### 13.4 Deterministic comparison algorithm

For every field group, run:

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

Default values for `POSITION`:

- `local_preference_window_ms = 250`
- `switch_advantage_ms = 100` (config validation MUST require `switch_advantage_ms < local_preference_window_ms`)
- `preferred_integrity_floor_nac_p = 5` when both sides report NACp; absent integrity is not automatically a failure (tie-break floor only; precision eligibility is §14.2)

Consequences:

- Local wins if it is no more than 250 ms older than central — 150 ms while central is currently selected.
- Central wins if it is materially newer, local is invalid/stale, or central has clearly superior reported integrity inside the tie window.
- Hysteresis prevents rapid source flapping but never retains an expired candidate. Tightening the tie window while central is selected (250 ms to switch away from local, 150 ms to switch back) prevents oscillation when local runs persistently near the window edge.
- The choice is recomputed on every candidate update and expiry timer.

### 13.5 Group-specific behavior

- Position selection is independent from callsign, altitude, and velocity.
- Latitude and longitude MUST come from one `POSITION` candidate.
- Ground speed and track SHOULD come from one `GROUND_VECTOR` candidate. If only one is present, the group may be partial but MUST carry that fact.
- Barometric and geometric altitude MUST never overwrite one another.
- `on_ground` is state, not altitude zero. Consumers MUST distinguish it. Absent `on_ground` means unknown; `false` is an observed airborne indication, which is why the field carries explicit presence.
- Identity values use longer TTLs and local tie-break, but a callsign change is accepted only after two matching observations or one high-confidence direct ADS-B update.
- Emergency/status changes bypass ordinary update suppression and publish immediately.

### 13.6 Out-of-order updates and deduplication

- Live state ignores a position older than the current same-source position by more than `same_source_reorder_tolerance_ms` (default 100 ms; MUST be at least the measured source timestamp quantization, §10.4).
- A late position within `history_late_accept_ms` (default 5,000 ms) may be inserted into time-sorted history if valid.
- Exact duplicate canonical observations do not increment state revision.
- A newer receipt of an identical value does not automatically imply a newer observation.
- Stream transport duplicates are removed using `(server_epoch, server_event_id)`.
- Subscription sequence gaps always cause resubscription; they are not inferred from global event IDs.

---

## 14. Freshness, staleness, and retention

### 14.1 Default solver-facing position policy

| Effective position age | Class | Default solver treatment |
|---:|---|---|
| `0–1,000 ms` | `FRESH` | Eligible for precision association |
| `>1,000–2,000 ms` | `USABLE` | Eligible; report age and uncertainty |
| `>2,000–5,000 ms` | `STALE` | Not precision-eligible; cautious extrapolation may be requested |
| `>5,000 ms` | `EXPIRED` | Omit live position; retain only bounded history/identity |

These are RETINA application defaults, not ADS-B standards. They MUST be configurable centrally by signed node configuration, with safe local defaults. The solver MUST use `usable_for_precision_association` and position age rather than assume any returned coordinate is current. Eligibility for precision association is defined in §14.2.

### 14.2 Precision-association eligibility and quality flags

`usable_for_precision_association` MUST be computed by the process emitting the `AircraftState`; the agent’s computation on the selected state is authoritative for the solver. It is true only when all of the following hold for the selected position group:

1. freshness class is `FRESH`;
2. validation and plausibility checks passed and no `track_discontinuity` is active (§12.3);
3. `time_basis` is `TIME_BASIS_RECEIVER_OBSERVATION` or `TIME_BASIS_READSB_SEEN_AGE`, and `time_uncertainty_ms` ≤ `precision_max_time_uncertainty_ms` (default 250);
4. neither `provider_latency_unknown` nor `provider_surveillance_type_unknown` is set;
5. when NACp is reported, NACp ≥ `precision_min_nac_p` (default 8, ≈93 m containment); when no integrity is reported, eligibility additionally requires `precision_allow_unknown_integrity` (default false);
6. `source_class` is not `SOURCE_CLASS_MLAT` or `SOURCE_CLASS_TISB`.

The §13.4 `preferred_integrity_floor_nac_p` (5) is a selection tie-break floor only; it does not confer precision eligibility. With shipped defaults: local receiver positions routinely qualify; central positions attributed to RETINA receivers qualify only while measured transport keeps their time uncertainty within bound; ADSBHub-derived and unattributable central positions never qualify (§10.5, §13.1).

v1 `quality_flags` values:

| Flag | Meaning |
|---|---|
| `provider_latency_unknown` | Upstream transit unknown; §10.5 penalty and uncertainty floor applied |
| `provider_surveillance_type_unknown` | Cannot be confirmed as direct ADS-B (possible MLAT/TIS-B or unattributed aggregate) |
| `integrity_below_precision_floor` | Reported integrity below the §14.2 precision floor |
| `time_uncertainty_high` | Time uncertainty exceeds the precision threshold |
| `track_discontinuity` | Jump detected; continuity re-establishment (§12.3) in progress |

Flags are informational inputs to eligibility, not a substitute for it. Consumers MUST tolerate unknown flags; adding a flag is a documentation update, not a schema change.

### 14.3 Group TTL defaults

| Field group | Fresh | Selection TTL | Cache retention |
|---|---:|---:|---:|
| Position | 1 s | 5 s | 120 s history |
| Barometric/geometric altitude | 2 s | 5 s | 30 s |
| Ground vector | 2 s | 5 s | 30 s |
| Vertical rate | 2 s | 5 s | 30 s |
| Squawk/emergency | 5 s | 15 s | 60 s |
| Callsign/category | 15 s | 60 s | 120 s |
| Integrity fields | atomic with position (§9.3) | with position | with position |
| Aircraft container without position | n/a | n/a | remove after 60 s without any group |

An expired field is removed from live output and produces a state revision. It is not emitted with a magic zero/null value that could be mistaken for an observation.

### 14.4 Stream health thresholds

| Condition | Degraded | Unhealthy |
|---|---:|---:|
| ADSBHub no valid record | 10 s | 30 s |
| Central `readsb` no JSON positions while input is nonzero | 5 s | 15 s |
| Node local sidecar disconnected | immediate degraded | 10 s unhealthy |
| Node central heartbeat missing | 15 s degraded | 30 s disconnected/reconnect |
| Node feed-forwarder no successful central connection | 60 s warning | 10 min critical, local unaffected |

No “no aircraft” condition is itself an error at a quiet receiver. Health checks must distinguish an active empty feed from a dead connection using bytes, heartbeats, or decoder statistics.

---

## 15. Streaming and snapshot semantics

### 15.1 Subscription

The first client message MUST be `Subscribe`. The authenticated certificate identity MUST match `Subscribe.node_id`; mismatch returns `PERMISSION_DENIED`.

`Subscribe.requested_features` is reserved for minor-version negotiation: v1 defines no feature strings, servers MUST ignore unknown entries, and a feature takes effect only when both sides support it.

The region radius MUST be derived per deployment from the node solver’s detection envelope plus margin; distribution cost scales with the square of the radius (§25.2). The shipped default is 150 km. Server policy sets minimum/maximum allowed radius (recommended 25–800 km). The server validates coordinates and may clamp or reject a radius above the node’s authorization.

### 15.2 Snapshot barrier

The server MUST generate a logically consistent snapshot:

1. record `barrier_event_id`;
2. enumerate the regional state as of that barrier;
3. buffer matching subsequent events in a bounded per-subscription queue;
4. send begin/chunks/end with contiguous `subscription_sequence`;
5. drain buffered events in order.

Chunk target: at most 500 aircraft or 512 KiB encoded, whichever comes first. Maximum inbound/outbound gRPC message size MUST be configured consistently and remain below 4 MiB for v1.

The client builds a shadow table and commits only after:

- matching snapshot ID on all chunks;
- contiguous subscription sequence;
- declared count match;
- SHA-256 match over the canonical snapshot manifest defined below; and
- `SnapshotEnd` before `snapshot_timeout_ms` (default 10,000).

The canonical snapshot manifest is formed by sorting states by `aircraft_key` bytewise, then appending for each state: UTF-8 `aircraft_key`, one NUL byte, the unsigned decimal `revision`, and one LF byte. `content_sha256` is the SHA-256 of those bytes. The manifest verifies membership and revisions without relying on language-specific Protobuf serialization order.

### 15.3 Deltas and tombstones

An `AircraftUpsert` contains the complete current state for one aircraft, not a sparse patch. This makes retries and replacement idempotent. An `AircraftDelete` is sent when:

- all live groups expire;
- an aircraft moves outside the subscription region plus hysteresis margin;
- authorization/filter changes exclude it;
- state is removed after a correction.

The node MUST treat an unknown-key delete as idempotent success.

### 15.4 Region boundary hysteresis

To prevent churn near the region edge:

- enter when position is within configured radius;
- remain subscribed until outside `radius + boundary_hysteresis_m` (default 10,000 m);
- delete after two consecutive outside observations or `boundary_exit_hold_ms` (default 2,000 ms), whichever confirms first.

### 15.5 Ordering and gaps

`subscription_sequence` begins at 1 for each stream and increments for every server message, including snapshot frames and heartbeats. Any gap, duplicate with different payload, or regression invalidates the stream. The agent closes it and requests a fresh snapshot.

`server_epoch` is a random 128-bit identifier created at state-service start. `server_event_id` is monotonic only within that epoch. v1 clients SHOULD always resnapshot after a reconnect; bounded resume MAY be enabled later when the server retains an event ring.

### 15.6 Backpressure

Each subscription queue is bounded by both messages and bytes (defaults: 10,000 messages or 16 MiB). On overflow:

1. increment a slow-consumer counter;
2. close the stream with `RESOURCE_EXHAUSTED` and a retry hint;
3. do not block the global state loop;
4. require a new snapshot.

The server MUST coalesce queued upserts for the same aircraft before declaring overflow, preserving deletes, per-aircraft ordering, and snapshot correctness; because upserts carry complete state (§15.3), coalescing to the newest is lossless for convergence. With coalescing, queue occupancy is bounded by regional aircraft count plus deletes, so overflow indicates a subscriber slower than the coalesced update rate — at the uncoalesced capability-bound regional rate (2,000 aircraft × 2–4 updates/s ≈ 4–8 k messages/s, §25.2) the default 10,000-message bound would be exhausted in about 2 seconds.

`ClientMessage.acknowledged_subscription_sequence` is diagnostic in v1: clients SHOULD report their last fully applied sequence at most once per heartbeat interval; servers record it for delivery-lag observability, MUST NOT require it, and MUST NOT use it for flow control before a protocol minor defines that behavior.

### 15.7 Heartbeat and keepalive

Application heartbeat default is 5 seconds when no other message was sent. gRPC health checking SHOULD expose the standard health service. HTTP/2 keepalive settings MUST be coordinated between client, server, and load balancer; aggressive sub-minute pings without active calls are prohibited. See the official gRPC [health checking](https://grpc.io/docs/guides/health-checking/) and [keepalive](https://grpc.io/docs/guides/keepalive/) guidance.

### 15.8 Update decimation

The server MUST support per-subscription decimation of upsert rate: a minimum emit interval per aircraft, optionally tiered by distance from the region center. Shipped default: full rate within `decimation.near_radius` (100 km), minimum interval `decimation.far_min_interval` (1,000 ms) beyond it. Decimation coalesces to the newest complete state, which is lossless for convergence because upserts carry complete state (§15.3). Deletes, emergency/status changes (§13.5), and snapshot frames are never decimated. Decimation parameters are part of node authorization and MUST be visible in the agent’s diagnostic status so downstream age statistics remain interpretable.

---

## 16. Local agent API semantics

### 16.1 Transport and access

Preferred endpoint:

```text
unix:///run/retina/adsb-agent.sock
```

Socket owner/group and mode default to `retina-adsb:retina-solver 0660`. If loopback TCP is used, bind only `127.0.0.1`/`::1`, require OS firewall enforcement, and use a configurable port such as 32100.

### 16.2 `GetSnapshot`

- Returns a complete point-in-time set from the agent’s selected live table.
- Excludes expired positions by default.
- Includes per-group provenance and age.
- Default maximum is 5,000 aircraft; larger results return `RESOURCE_EXHAUSTED` unless pagination is added in a later minor version.
- Deadline recommendation: 500 ms.

### 16.3 `Watch`

- The caller first obtains `GetSnapshot`, records its `local_revision`, and opens `Watch(after_local_revision=local_revision)`.
- The agent retains a replay ring covering at least `watch_replay_window_ms` (default 30,000 ms) of events, and replays matching events newer than `after_local_revision` before live events. This closes the snapshot/watch race. The ring MUST be compacted to stay within its 10,000-event bound: because upserts carry complete state, superseded upserts for an aircraft MAY be dropped provided the newest retained upsert and all deletes are preserved. (Uncompacted, the capability-bound regional rate of 4–8 k events/s (§25.2) would reduce a 10,000-event ring to ≈2 s of cover, forcing a resnapshot on any reconnect slower than that.)
- If the requested revision is older than the retained event ring, the agent returns `OUT_OF_RANGE`; the caller MUST obtain a new snapshot.
- A caller reconnects with the last fully applied `local_revision`. Revision gaps are allowed (region filtering and replay compaction both create them); regression or two different events with the same revision is a protocol error.
- Sends complete selected aircraft state on every material change.
- Publishes deletions as `LocalAircraftEvent.delete`; it MUST NOT encode deletion as an empty aircraft state.
- Uses a bounded per-client queue. A slow local consumer is disconnected and must resnapshot.

### 16.4 `GetAtTime`

Given aircraft `A` and time `T`:

1. return `EXACT_SAMPLE` when a sample lies within ±`exact_sample_window_ms` (default 100 ms; MUST be at least the measured source timestamp quantization, §10.4);
2. otherwise interpolate only if bracketing positions exist from a single source class, each is valid, the gap is ≤2.5 s, the implied motion is plausible, and `T` is between them; cross-source bracketing MAY be used only when no single-source bracket exists, in which case the response adds a warning and inflates `estimated_horizontal_uncertainty_m` by the combined time uncertainty multiplied by ground speed;
3. otherwise extrapolate only when requested, the last valid position is ≤2.0 s before `T`, a valid ground vector exists within 2.0 s of that position, and the aircraft is not in a detected discontinuity;
4. otherwise return `NEAREST_SAMPLE` only when `allow_nearest_sample` is set, or `UNAVAILABLE`.

Interpolation SHOULD operate in Earth-centered Earth-fixed coordinates or on a geodesic, not linearly across longitude near the antimeridian. Extrapolation uses a constant-velocity great-circle model for the short configured horizon. Vertical extrapolation uses the matching altitude reference and rate; it never converts barometric to geometric altitude.

The response MUST identify method, temporal distance, estimated uncertainty, selected source(s), and warnings. The solver decides whether the estimate is acceptable.

`GetRegionAtTime` applies the same per-aircraft rules to every aircraft whose estimated position at `T` lies inside the region, omitting `UNAVAILABLE` results; the response is bounded by the §16.2 aircraft cap. It exists so detection association and label generation can query one radar timestamp with one RPC instead of one call per aircraft per frame.

### 16.5 Material-change suppression

To reduce churn, the agent may suppress a new local API event only if all selected values and sources are unchanged and age-class boundaries have not changed. Position updates MUST NOT be suppressed based solely on small coordinate difference; the new observation time matters to RETINA.

---

## 17. History implementation

Default per-aircraft position history:

- 120 seconds;
- maximum 512 samples;
- time-sorted insertion for accepted late samples up to 5 seconds;
- duplicate collapse by source, observation time, and coordinate;
- cross-source collapse: a sample matching another source’s sample within `cross_source_duplicate_m` (default 10 m) and within their combined time uncertainty SHOULD be stored as a duplicate reference keeping the lower-uncertainty timestamp, not as a distinct kinematic sample — the same physical broadcast otherwise appears twice at observation-time estimates ~250 ms apart and corrupts interpolation;
- memory-only;
- selected position plus optionally both source candidates, controlled by config.

Default node-wide safety limits:

- maximum 5,000 aircraft with any state;
- maximum 500,000 history samples;
- local event replay ring: 30 seconds compacted (§16.3), maximum 10,000 retained events;
- memory target ≤256 MiB for the agent;
- on pressure, evict expired/nonregional aircraft first, then oldest history; never kill the local API process intentionally.

No v1 history is required to survive a restart. Optional diagnostic capture MUST be time-bounded, encrypted where required, and disabled by default.

---

## 18. Configuration

### 18.1 Rules

- Configuration uses YAML with an explicit `config_version`.
- Unknown keys are fatal by default.
- Secrets and private keys MUST be file references or secret-manager references, never inline values.
- Every process supports `--check-config` without opening network sockets.
- Reloadable values are documented; unsafe changes require restart.
- The running process exposes a redacted configuration hash and build version.
- Fleet configuration is signed and staged by cohort.

### 18.2 Server example

```yaml
config_version: 1

adsbhub:
  host: data.adsbhub.org
  port: 5002
  connect_timeout: 10s
  idle_timeout: 30s
  line_max_bytes: 4096
  timestamp_timezone: UTC
  provider_age_penalty: 1s
  ingest_uncertainty_floor: 1s
  reconnect:
    initial: 1s
    maximum: 60s
    jitter_ratio: 0.20

feed_gateway:
  listen: 0.0.0.0:7444
  client_ca_file: /run/secrets/retina-node-ca.pem
  server_cert_file: /run/secrets/feed-server.pem
  server_key_file: /run/secrets/feed-server-key.pem
  max_connections_per_node: 2
  max_bytes_per_second_per_node: 1000000
  idle_timeout: 90s
  readsb_target: 127.0.0.1:31004

readsb:
  json_source: 127.0.0.1:31005
  reconnect_initial: 250ms
  reconnect_maximum: 10s

distribution:
  listen: 0.0.0.0:8443
  default_radius: 150km
  maximum_radius: 800km
  decimation:
    near_radius: 100km
    far_min_interval: 1s
  snapshot_chunk_aircraft: 500
  subscriber_queue_messages: 10000
  subscriber_queue_bytes: 16777216
  heartbeat_interval: 5s

state:
  position_expiry: 5s
  aircraft_expiry: 60s
  history_retention: 120s
  future_tolerance: 1s
  late_history_accept: 5s
  boundary_hysteresis: 10km

telemetry:
  metrics_listen: 127.0.0.1:9090
  log_format: json
  otlp_endpoint: http://otel-collector:4317
```

### 18.3 Suggested server-side `readsb-hub` shape

The deployment MUST generate arguments for the pinned build and verify them in integration tests. The expected topology is:

```text
--net-only
--net
--net-bind-address=127.0.0.1
--net-ingest
--net-receiver-id
--net-bi-port=31004
--net-json-port=31005
--net-json-port-interval=0
--net-connector=127.0.0.1,31003,sbs_in
```

`adsbhub-adapter` listens on loopback port 31003 after it has established and validated the upstream feed. `readsb-hub` reconnects to that relay as an SBS client. `retina-feed-gateway` connects to the loopback Beast input on 31004. `retina-adsb-server` connects as a client to the JSON listener on 31005. If process/container network namespaces differ, use private service addresses and firewall policy rather than publishing these ports.

### 18.4 Node example

```yaml
config_version: 1

node:
  id: retina-node-0042
  latitude: 37.6213
  longitude: -122.3790
  subscription_radius: 150km   # derive from solver detection envelope plus margin (§15.1)

local_source:
  json_address: 127.0.0.1:32005
  reconnect_initial: 100ms
  reconnect_maximum: 5s
  hard_reject_age: 30s

central_source:
  endpoint: adsb.retina.example:443
  ca_file: /etc/retina/pki/ca.pem
  cert_file: /etc/retina/pki/node.pem
  key_file: /etc/retina/pki/node-key.pem
  server_name: adsb.retina.example
  probe_interval: 30s
  unknown_network_penalty: 250ms
  max_network_penalty: 2s
  reconnect:
    initial: 250ms
    maximum: 30s
    jitter_ratio: 0.20

fusion:
  local_preference_window: 250ms
  switch_advantage: 100ms
  position_fresh: 1s
  position_usable: 2s
  position_expiry: 5s
  precision:
    max_time_uncertainty: 250ms
    min_nac_p: 8
    allow_unknown_integrity: false

history:
  retention: 120s
  max_samples_per_aircraft: 512
  max_total_samples: 500000
  interpolation_max_gap: 2500ms
  extrapolation_max_horizon: 2s

local_api:
  unix_socket: /run/retina/adsb-agent.sock
  socket_mode: "0660"
  socket_group: retina-solver

telemetry:
  metrics_listen: 127.0.0.1:9210
  diagnostics_listen: 127.0.0.1:9211
  log_format: json
```

### 18.5 Suggested node-side `readsb-retina` shape

The installer MUST generate arguments appropriate for the pinned build rather than paste this blindly:

```text
--net-only
--net
--net-bind-address=127.0.0.1
--net-connector=127.0.0.1,30005,beast_in
--net-json-port=32005
--net-json-port-interval=0
--net-connector=127.0.0.1,39004,beast_reduce_plus_out,uuid=<receiver-uuid>
```

All selected ports MUST be probed before installation. The exact connector syntax MUST be verified against the pinned `readsb` release.

---

## 19. Network and port plan

| Scope | Port/path | Protocol | Exposure | Purpose |
|---|---|---|---|---|
| Server | 443 | gRPC/HTTP2/TLS | Nodes/approved clients | Distribution API |
| Server | 7444 | BeastReducePlus over mTLS | Authorized nodes | Contributed feed gateway |
| Server | 31003 | SBS/TCP | Loopback/private only | Adapter relay; `readsb` connects with `sbs_in` |
| Server | 31004 | Beast/TCP | Loopback/private only | Gateway → `readsb` |
| Server | 31005 | JSON lines/TCP | Loopback/private only | `readsb` → state server |
| Server | 9090+ | HTTP | Monitoring network only | Metrics/health by component |
| Node | existing 30005 | Beast/TCP | Loopback/LAN per existing policy | Existing decoder output |
| Node | 32005 | JSON lines/TCP | Loopback only | Sidecar → agent |
| Node | 39004 | Beast/TCP | Loopback only | Sidecar → TLS forwarder |
| Node | `/run/retina/adsb-agent.sock` | gRPC | Local OS ACL | Solver API |
| Node | 9210/9211 | HTTP | Loopback only | Metrics/diagnostics |

No deployment automation may assume these ports are free; collision detection is an install precondition.

---

## 20. Deployment architecture

### 20.1 Initial production deployment

One server host or small VM group may run the logical components with containers or hardened system services:

```text
edge proxy
feed-gateway
adsbhub-adapter
readsb-hub
retina-adsb-server
optional tar1090
OpenTelemetry Collector / metrics agent
```

Components MUST have separate users/containers, read-only roots where practical, dropped Linux capabilities, resource limits, restart policies, and private networking. `readsb-hub` and adapters MUST not share the API’s public network namespace unless firewall policy provides equivalent isolation.

### 20.2 Scaling path

When one host is insufficient, split in this order:

1. public node-feed ingress replicas;
2. `readsb` ingest tier and hub tier, following the ADSB.lol ingest → hub → planes pattern;
3. state/distribution replicas fed by an internal bounded pub/sub stream;
4. regional sharding by geospatial cell;
5. optional durable event capture.

NATS or another internal broker MAY be introduced only when multiple state-service replicas require one ordered update fanout. v1 MUST NOT depend on a broker solely for conceptual purity.

### 20.3 Availability and updates

- Nodes use at least two resolved distribution addresses when the service has multiple replicas.
- gRPC load balancing and health checking MUST direct new streams only to ready instances.
- Deployment sets readiness false, sends `GOAWAY`/graceful drain, waits up to 30 seconds, then terminates.
- Nodes reconnect with jitter and continue using local/cached candidates.
- ADSBHub ingestion is active/standby unless the provider explicitly permits multiple simultaneous connections; both instances use the authorized stable egress IP.
- A server restart changes `server_epoch`, forcing a clean snapshot.

### 20.4 Node service ordering

Recommended dependency order:

```text
existing readsb
  └─ readsb-retina
       ├─ retina-adsb-agent (local input; soft dependency)
       └─ retina-feed-forwarder (soft dependency)

network-online
  └─ central gRPC and feed-forwarder connections (never hard startup gates)
```

The agent MUST start and make its local API available in degraded mode even if both sources are initially unavailable.

### 20.5 Node packaging and transactional installation

Node artifacts SHOULD be native signed packages for the supported distribution or immutable containers integrated with the existing node supervisor. A package contains versioned binaries, service definitions, configuration schema, migration tool, health probe, and uninstaller. It MUST NOT bundle a floating upstream `readsb` build.

Where the fleet already operates the RETINA Mender OTA pipeline, ADS-B artifacts MUST be delivered and rolled back through that pipeline (application or rootfs A/B updates) rather than through a parallel package mechanism; the discovery, preflight, shadow-verification, and activation sequence below still applies to the update however delivered. The versioned-symlink layout below applies to hosts without an OTA manager.

Filesystem layout:

```text
/etc/retina/adsb/                 root-owned configuration and public trust material
/etc/retina/adsb/pki/             node certificate/key; key mode 0600
/var/lib/retina/adsb/             stable receiver UUID, migration state, package metadata
/run/retina/                      Unix socket and ephemeral status
/usr/lib/retina-adsb/<version>/   versioned immutable binaries
/usr/lib/retina-adsb/current      atomic symlink to active version
```

Installer sequence:

1. discover OS/architecture, service manager, decoder process, decoder version, Beast address, existing ports, receiver UUID, and available resources;
2. run preflight without mutation and emit a machine-readable report;
3. refuse installation on ambiguous decoder ownership, occupied required ports, insufficient disk/memory, or unsupported architecture;
4. install versioned files and validate configuration offline;
5. enroll/retrieve node certificate through the approved fleet mechanism;
6. start `retina-feed-forwarder`, `readsb-retina`, and `retina-adsb-agent` in a non-solver shadow state;
7. verify local JSON updates, central snapshot, metrics, and that existing tar1090/decoder service remains healthy;
8. atomically mark the version active;
9. enable solver migration only through a separate feature flag.

If any verification fails, the installer stops only the newly installed services, restores its prior symlink/config, and leaves the existing receiver stack untouched. Upgrades use the same staged sequence and retain at least one previous version.

### 20.6 Rollback

Node rollback is stop/disable RETINA sidecar, forwarder, and agent, then restore the prior package version (or the equivalent Mender rollback). It MUST NOT require restoring the existing tar1090/readsb config in default Mode A. Server rollback retains the current and previous schema-compatible build and switches traffic after readiness verification.

---

## 21. Security

### 21.1 Authentication and authorization

- Each node receives a unique client certificate; certificates MUST NOT be shared by a fleet image.
- Node certificate SAN contains the canonical `node_id`.
- Feed gateway authorization maps `node_id` to allowed receiver UUID(s), connection limit, and rate policy.
- Distribution authorization maps `node_id` to allowed radius, features, and environment.
- Server certificates use a publicly trusted or RETINA-managed CA and include the exact configured DNS name.
- Node private keys MUST be generated in, and never leave, the node’s hardware secure element (ATECC608B) on hardware that has one, with TLS client authentication performed against the secure element; certificate enrollment SHOULD reuse the existing RETINA chain-of-custody provisioning flow rather than a parallel PKI process. File-based node keys (mode `0600`, outside container images) are permitted only on hardware without a secure element.
- Server private keys use mode `0600`, remain outside container images, and SHOULD use hardware-backed storage when available.

### 21.2 Certificate lifecycle

- Default node certificate lifetime: 30–90 days.
- Automated renewal begins before 50% of lifetime is consumed.
- Server accepts current and next CA during rotation.
- Revocation propagates within 15 minutes through an authorization denylist or short-lived certificate expiry.
- A node with an expired/revoked feed certificate loses contribution and central access but continues local service.

### 21.3 Input and resource protection

- Maximum frame/message sizes are enforced before parsing.
- Per-node token buckets limit bytes and decoded frames.
- Global connection and memory caps prevent exhaustion.
- Parser fuzzing is a release gate.
- Logs escape user-controlled values and exclude raw certificate keys/tokens.
- Raw network ports remain private.
- Admin and metrics endpoints require network policy; they are not exposed on public ingress.

### 21.4 Supply chain

- Pin source commits/container digests.
- Generate SBOM and vulnerability scan in CI.
- Verify release signatures/checksums where upstream provides them.
- Run as non-root unless SDR/device access makes a narrowly scoped exception necessary.
- Separate build and runtime images.
- Apply security updates through canary/shadow validation, not unreviewed floating tags.

### 21.5 Privacy and data governance

ADS-B data is public RF-derived data but may still create operational or regulatory concerns. RETINA MUST define retention, access, and disclosure policy. v1 keeps only short in-memory operational history by default. Diagnostic captures require an operator reason, duration limit, access audit, and deletion schedule.

### 21.6 Threat cases

| Threat | Required mitigation |
|---|---|
| Forged node feed | Unique mTLS identity, UUID binding, plausibility checks, rate limits |
| Replay | Short certificate lifetime, sequence/timestamp checks, age limits; optionally nonce-bound session metadata later |
| Malformed SBS/Beast | Bounded parsers, fuzzing, per-reason drops, process isolation |
| Slow subscriber | Bounded queue and disconnect |
| Provider poisoning | Treat as untrusted, compare with local, integrity/plausibility gates, source observability |
| Feedback loop | Architectural prohibition on forwarding central data into contribution path |
| Credential extraction | File permissions, secret mounts, no image embedding, rotation/revocation |
| Metrics cardinality attack | No aircraft key, callsign, certificate subject, or node ID as unrestricted metric label |

---

## 22. Observability

Metrics follow Prometheus base-unit and `_total` counter conventions and avoid high-cardinality labels, consistent with [Prometheus naming guidance](https://prometheus.io/docs/practices/naming/). Logs are structured JSON. Traces use OpenTelemetry for control/API requests; per-aircraft hot-path traces are sampled or represented by metrics to avoid overload.

### 22.1 Required server metrics

```text
retina_adsb_provider_connected{provider}
retina_adsb_provider_bytes_total{provider}
retina_adsb_provider_records_total{provider,result}
retina_adsb_provider_last_record_timestamp_seconds{provider}
retina_adsb_provider_reconnects_total{provider,reason}
retina_adsb_provider_record_delay_seconds{provider}        histogram

retina_adsb_feed_connections{state}
retina_adsb_feed_bytes_total{result}
retina_adsb_feed_frames_total{result,reason}
retina_adsb_feed_auth_failures_total{reason}

retina_adsb_readsb_json_records_total{result}
retina_adsb_readsb_last_position_timestamp_seconds
retina_adsb_aircraft_live
retina_adsb_positions_total{source_class,result}
retina_adsb_position_age_seconds{source_class}             histogram
retina_adsb_state_events_total{type}

retina_adsb_subscriptions{state}
retina_adsb_subscription_queue_messages                    histogram/gauge aggregate
retina_adsb_subscription_disconnects_total{reason}
retina_adsb_snapshot_duration_seconds                      histogram
retina_adsb_snapshot_aircraft                              histogram
retina_adsb_grpc_requests_total{service,method,code}
retina_adsb_grpc_request_duration_seconds{service,method}  histogram
retina_adsb_build_info{version,commit,schema_version}
```

Node-specific breakdown belongs in authenticated logs or a bounded top-N diagnostics system, not an unbounded Prometheus `node_id` label if fleet size/cardinality makes that unsafe.

### 22.2 Required node metrics

```text
retina_adsb_agent_source_connected{source}
retina_adsb_agent_source_updates_total{source,result}
retina_adsb_agent_source_last_update_timestamp_seconds{source}
retina_adsb_agent_selected_aircraft{source}
retina_adsb_agent_source_switches_total{from,to,group,reason}
retina_adsb_agent_position_age_seconds{source}              histogram
retina_adsb_agent_position_freshness{class}                 gauge
retina_adsb_agent_stream_gaps_total
retina_adsb_agent_snapshots_total{result}
retina_adsb_agent_central_rtt_seconds                       histogram
retina_adsb_agent_history_samples
retina_adsb_agent_history_evictions_total{reason}
retina_adsb_agent_local_api_requests_total{method,code}
retina_adsb_agent_local_api_duration_seconds{method}        histogram
retina_adsb_forwarder_connected
retina_adsb_forwarder_bytes_total{direction}
retina_adsb_agent_build_info{version,commit,schema_version}
```

Aircraft address and callsign MUST NOT be metric labels.

### 22.3 Structured log fields

Every log record includes:

```text
timestamp, severity, service, version, host/node_id, environment,
event, message, trace_id (when present), connection_id (when relevant),
source_class, error_code, retry_in_ms
```

Aircraft key may appear only in debug logs or targeted diagnostic sampling, never as the default volume path. Private keys, certificate contents, raw tokens, and full untrusted frames MUST NOT be logged.

### 22.4 Dashboards

Required dashboards:

1. end-to-end pipeline: provider/node input → `readsb` → state → subscriptions;
2. freshness: position age percentiles by source and node cohort;
3. node source selection: local/central/none ratios and switch reasons;
4. fleet connectivity: central stream and contribution connection health;
5. parser/security: malformed input, auth failures, rate limiting;
6. resource/capacity: CPU, memory, queues, file descriptors, network;
7. release comparison: canary versus control error/latency/freshness.

### 22.5 Alerts

| Alert | Default condition |
|---|---|
| ADSBHubDown | disconnected or no valid records for 30 s |
| CentralStateStalled | input bytes increase but no state update for 15 s |
| DistributionUnavailable | successful streams below SLO for 5 min |
| FreshnessRegression | p95 central position age increases by >250 ms vs 7-day baseline for 10 min |
| NodeLocalSourceLost | local-capable node has no local updates for 10 s while expected traffic exists |
| StreamGapStorm | >3 resnapshots/node/10 min or cohort anomaly |
| SlowConsumers | resource-exhausted disconnect rate above threshold |
| ClockOffset | warning/critical thresholds in Section 10 |
| FeedAuthAttack | auth/malformed rate over threshold |
| MemoryPressure | >85% limit for 5 min or history eviction surge |

---

## 23. Health and readiness

Every process exposes:

- **liveness:** process event loop/parser is responsive; does not depend on upstream health;
- **readiness:** safe to receive new traffic or serve the declared role;
- **detailed status:** authenticated/redacted dependency state for operators.

Examples:

- `adsbhub-adapter` may be live but not ready to be considered a healthy provider while disconnected.
- `retina-adsb-server` is ready only after connecting to `readsb`, building an initial state (which may be empty), and starting the distribution listener.
- `retina-adsb-agent` is live and local-API ready even with zero sources; detailed status reports `DEGRADED_NO_SOURCES`.
- Empty airspace is not a liveness failure.

The gRPC server SHOULD implement the standard `grpc.health.v1.Health` service with separate service names for distribution and local APIs.

---

## 24. Failure behavior

| Failure | Required behavior | Operator signal |
|---|---|---|
| ADSBHub TCP disconnect | Backoff/reconnect; node contributions continue; no server crash | provider-down alert, reconnect counter |
| ADSBHub format change | Drop unknown/malformed lines; trip degraded threshold; preserve node feeds | parse-error alert, sampled schema diagnostic |
| Server `readsb` crash | Supervisor restarts; state ages/expires; streams heartbeat/degrade or reconnect | readiness false, restart alert |
| State server deployment | Drain streams; agents retain local/cache and resnapshot | deploy event, reconnect rate |
| Central network partition | Local source continues; central candidates age/expire; API remains available | node central disconnected |
| Node SDR/decoder failure | Local candidates age/expire; central becomes selected | local source lost, source-switch count |
| Both sources fail | Agent serves non-position identity/history as permitted, live positions expire, API reports degraded | no-sources alert |
| Local JSON line malformed | Drop one line, count, continue | parse counter |
| Central sequence gap | Abort shadow/current stream, reconnect, full snapshot; local unaffected | stream-gap counter |
| Snapshot interrupted | Discard shadow snapshot, keep previous committed table aging | snapshot failure counter |
| Slow node subscriber | Server disconnects it; agent reconnects/resnapshots | resource-exhausted metric |
| Node contribution rejected | Local operation and central consumption continue; retry only auth-transient cases | feed auth/status log |
| Clock jump | Monotonic ages remain nondecreasing; flag wall-time uncertainty | clock alert |
| Disk full | Operational state continues in memory; logs rate-limit/fallback; no raw unbounded writes | disk alert |
| Memory pressure | Evict history/nonregional expired entries; never unbound queue | eviction/memory alert |
| Invalid certificate | Do not bypass validation; local source remains available | auth failure |
| Duplicate server replicas/epoch change | Agent resnapshots; event IDs not compared across epochs | epoch-change counter |

Retries MUST use exponential backoff with full or equal jitter. Default progression: 250 ms, 500 ms, 1 s, 2 s, 4 s … capped at 30 s for node distribution and 60 s for ADSBHub. A successful stable connection of 60 seconds resets the backoff.

---

## 25. Performance and service objectives

### 25.1 Latency definitions

- `local_decode_to_agent`: `readsb-retina` JSON emit to agent receipt.
- `local_agent_to_solver`: agent receipt to local watcher delivery.
- `server_state_to_node`: server state acceptance to node agent receipt.
- `central_end_to_end`: best known central observation time to node receipt; marked approximate when upstream transit is unknown.

### 25.2 v1 SLOs under reference load

Reference load: 5,000 concurrently tracked central aircraft, 200 node subscriptions, 100 contributing feeds, 5 position updates/s/aircraft burst tolerance, ≤400 aircraft within a default 150 km node region, and normal instance CPU below 70%. The 2,000-aircraft regional figures below are capability bounds, not typical load.

Bandwidth is a first-class budget: a full `AircraftState` upsert with per-group metadata encodes to roughly 0.4–0.7 KB, and an undecimated 2,000-aircraft region at 2–4 state-changing updates/s/aircraft is ≈13–45 Mbps sustained per node — untenable on residential links and as fleet-multiplied server egress. Region radius (§15.1), decimation (§15.8), and meta slimming (§10.1) are the controls; any change that increases encoded upsert size requires a bandwidth review.

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
| Per-node sustained distribution throughput p95 | ≤2 Mbps at reference regional load (≈650 GB/month/node); per-deployment budget documented |
| Fleet server egress | Projected from subscribed regions and decimation policy; documented and approved before Phase 4 |

Provider-to-server latency is reported but is not an enforceable RETINA SLO for ADSBHub.

### 25.3 Capacity test headroom

Release load tests MUST pass at 2× expected aircraft/update volume for 60 minutes without memory growth, process crash, invalid sequence, or p95 latency exceeding 2× its SLO. A 5× burst for 60 seconds must recover within 30 seconds without stale queues being replayed as fresh data.

---

## 26. Testing strategy

### 26.1 Unit tests

Required coverage:

- SBS framing/parsing, empty fields, quoted fields, invalid dates, long lines;
- Beast framing and receiver UUID extraction;
- `readsb` JSON mapping, omitted/unknown fields, `ground` altitude;
- time conversion, negative/future ages, monotonic age behavior;
- every validation/range rule;
- selector truth table including equality, tie window, integrity override, hysteresis (including the tightened tie window at the boundary, §13.4), and expiry;
- precision-eligibility rules (§14.2) as a truth table;
- cross-source history collapse and single-source interpolation preference (§16.4, §17);
- atomic field grouping;
- deduplication and out-of-order handling;
- region/antimeridian/pole geometry;
- snapshot hashing/commit/rollback;
- interpolation and extrapolation, including antimeridian and on-ground cases;
- configuration validation and secret redaction.

Fusion logic SHOULD use property-based tests. Important invariants:

- selected candidate is eligible;
- selected position coordinates come from one candidate;
- effective age never decreases without a newer observation;
- an expired local candidate cannot block an eligible central candidate;
- central failure cannot delete an eligible local candidate;
- a sequence gap cannot produce a partial snapshot commit.

### 26.2 Golden fixtures

The repository MUST contain versioned, license-reviewed fixtures:

- representative ADSBHub SBS lines for each supported transmission subtype;
- captured/synthetic Beast and BeastReducePlus frames;
- `readsb` TCP JSON objects from the pinned version;
- malformed/fuzz-regression cases;
- full snapshot/delta streams for current and previous minor schema versions.

Fixtures containing real data MUST be short, access-controlled where necessary, and documented.

### 26.3 Contract tests

- Generate clients/servers in every production implementation language.
- Verify old client ↔ new server and new client ↔ old server for supported minors.
- Verify unknown Protobuf fields and enum values are tolerated.
- Verify maximum message sizes and error codes.
- Verify mTLS identity/SAN authorization.
- Run Protobuf breaking-change tooling in CI.

### 26.4 Integration tests

A hermetic test stack runs:

```text
synthetic SBS provider
synthetic Beast node feeds
gateway + readsb-hub + state server
node sidecar/agent
fake solver client
```

It verifies:

- local position wins a near-equal returned central copy;
- central wins when more than the configured margin fresher;
- central fills aircraft absent locally;
- local continues while server is stopped/upgraded;
- central takes over within one update after local loss;
- fields expire at exact configured boundaries;
- snapshot plus concurrent delta produces no loss or duplication;
- regional entry/exit tombstones and hysteresis;
- slow client isolation;
- gateway rejects wrong UUID/certificate combinations;
- `GetRegionAtTime` results are consistent with per-aircraft `GetAtTime` for a synthetic scenario;
- decimated subscriptions converge to latest state and never drop deletes.

### 26.5 Fault-injection tests

Automate:

- dropped, duplicated, reordered, truncated, and delayed frames;
- 1%, 10%, and 30% packet loss plus 50–2,000 ms latency;
- half-open TCP and HTTP/2 connections;
- server restart during every snapshot phase;
- DNS failure and certificate expiry;
- node clock jumps ±5 minutes while monotonic clock continues;
- out-of-memory pressure and full filesystem;
- malformed/fuzzed provider and feed input;
- 5× traffic burst and slow solver watcher.

### 26.6 Replay and shadow tests

Before each `readsb`, parser, or fusion change:

1. replay a minimum 30-minute representative capture through old and new builds;
2. compare aircraft count, accepted/dropped updates, coordinates, source choices, and age distributions;
3. manually inspect statistically significant differences;
4. block rollout on unexplained loss, stale-age regression, or implausible positions.

### 26.7 Soak tests

- Server: 72 hours at expected load, then 24 hours at 2×.
- Node agent: 7 days on representative ARM hardware with network fault cycling.
- Pass condition: no unbounded memory/file descriptor growth, deadlock, revision regression, or freshness drift.
- Gate staging (§29): the 72-hour server soak and a 48-hour node soak gate canary production (Gate 1); the 7-day node soak and the 2× extension gate fleet rollout (Gate 2).

### 26.8 Security tests

- Parser fuzzing with continuous corpus retention.
- TLS downgrade/invalid-chain/wrong-name/expired/revoked cert cases.
- Rate-limit and connection-exhaustion tests.
- Secret/log redaction tests.
- Container/user/capability and public-port scan.
- Dependency and container vulnerability scan.

---

## 27. CI/CD quality gates

A change cannot reach production unless:

1. formatting, lint, static analysis, and unit tests pass;
2. race/concurrency tests pass where supported;
3. generated Protobuf code is clean and compatibility gate passes;
4. integration and required fault tests pass;
5. SBOM and vulnerability policy pass;
6. container image is immutable, signed if supported, and tied to a source commit;
7. config schema and example validation pass;
8. replay comparison is attached for parser/decoder/fusion changes;
9. canary dashboards and rollback artifact exist;
10. release notes identify schema/config/operational changes.

---

## 28. Rollout plan

### Phase 0: inventory and measurement

- Inventory node OS/architecture, receiver decoder, ports, tar1090 install mode, UUID, CPU/memory, and service manager.
- Capture existing local latency/availability baseline.
- Confirm stable server egress IP and ADSBHub account/feed entitlement.
- Freeze initial Protobuf/config schemas and create test fixtures.

Exit: ≥95% of target nodes classified into a supported integration mode; no unresolved common port/decoder variant.

### Phase 1: server shadow ingest

- Run ADSBHub adapter, feed gateway, `readsb-hub`, and state server without production node consumption.
- Connect a few test receivers.
- Compare central tar1090/state with independent known-good views and local captures.
- Tune parser and staleness metrics.

Exit: 72-hour soak, no unbounded growth, parse error understood, freshness dashboards operational.

### Phase 2: node agent shadow mode

- Install sidecar/agent on 1–5 lab nodes.
- Solver continues using its old source.
- Agent logs the state it would select and comparison deltas; contribution may be enabled.
- Verify tar1090 and receiver statistics are unchanged within noise.

Exit: zero disruption to existing ADS-B UI/feeds; selection policy matches expected replay truth.

### Phase 3: canary solver integration

- Switch one noncritical node’s solver to local agent API.
- Expand to 5%, then 20% of fleet across hardware/network cohorts.
- Hold each cohort at least 48 hours.
- Exercise planned server deployment and forced local/central outages.

Exit: Gate 1 criteria (§29) pass; rollback performed successfully at least once.

### Phase 4: fleet rollout

- Entry requires Gate 2 criteria (§29).
- Expand 25% → 50% → 100% with automated health gates.
- Maintain previous agent version and server compatibility.
- Do not combine package update with unrelated SDR/tar1090 changes.

### Phase 5: optimize and scale

- Measure whether server role separation, pub/sub, regional shards, or additional providers are necessary.
- Tune freshness penalty using nodes simultaneously visible locally and through ADSBHub.
- Consider durable history and richer receiver provenance only with a demonstrated need.

---

## 29. Acceptance criteria

The system is accepted for production only when every required criterion has recorded evidence. Criteria are staged into two gates so that v1 remains shippable by a small team:

- **Gate 1 — canary production (Phase 3 entry/exit):** required before any production solver consumes the agent API.
- **Gate 2 — fleet rollout (Phase 4 entry):** additionally required before expansion beyond the canary cohort.

Evidence requirements are identical at both gates; only the timing differs.

### 29.1 Functional

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

### 29.2 Resilience and non-disruption

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

### 29.3 Performance

| ID | Criterion | Gate |
|---|---|---|
| AC-P-01 | Local decode-to-selection p95 ≤50 ms at reference node load. | 1 |
| AC-P-02 | Agent-selection-to-solver p95 ≤20 ms. | 1 |
| AC-P-03 | Server-accepted-to-node p95 ≤250 ms and p99 ≤500 ms at reference load. | 1 |
| AC-P-04 | Regional snapshot of 2,000 aircraft commits within 5 s p95 after reconnect. | 1 |
| AC-P-05 | 2× capacity test passes for 60 minutes and 5× burst recovers within 30 seconds. | 2 |
| AC-P-06 | Position ages are nondecreasing between observations and cross class/expiry thresholds on time ±50 ms in deterministic tests (synthetic timestamps; real-source tolerance additionally includes the measured §10.4 quantization). | 1 |
| AC-P-07 | Per-node distribution throughput at reference regional load is within the §25.2 budget. | 1 |

### 29.4 Security

| ID | Criterion | Gate |
|---|---|---|
| AC-S-01 | Public scan exposes only approved TLS endpoints; no raw `readsb`, metrics, admin, or node-local ports. | 1 |
| AC-S-02 | Invalid/expired/wrong-node client certificates are rejected. | 1 |
| AC-S-03 | Receiver UUID mismatch is rejected and audited. | 1 |
| AC-S-04 | Fuzz suites produce no crash, hang, unbounded allocation, or secret disclosure. | 1 |
| AC-S-05 | Images have SBOMs, pass vulnerability policy, run with least privilege, and contain no node private keys. | 1 |
| AC-S-06 | Credential rotation and revocation are demonstrated without loss of local service — before Gate 2 and in any case before the first certificate renewal window. | 2 |

### 29.5 Observability and operations

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

## 30. Work breakdown and ownership

| Workstream | Primary outputs |
|---|---|
| Protocol/API | Protobuf contract, generated clients, compatibility CI, API examples |
| Server ingest | ADSBHub adapter, secure feed gateway, `readsb` packaging/configuration |
| Server state/distribution | Normalizer, state table, spatial index, snapshot/delta service |
| Node platform | Discovery/installer, `readsb-retina`, TLS forwarder, service definitions |
| Node agent | Local/central adapters, fusion, history, localhost API |
| Solver integration | Client library, age/quality handling, migration from old source |
| Infrastructure | DNS, stable egress, certificates, secrets, deployment, firewalls |
| Observability/SRE | Metrics, logs, dashboards, alerts, runbooks, capacity plan |
| QA/security | Fixtures, replay harness, fault injection, fuzzing, threat tests |

Recommended repository layout:

```text
retina-adsb/
  proto/retina/adsb/v1/
  cmd/adsbhub-adapter/
  cmd/retina-feed-gateway/
  cmd/retina-adsb-server/
  cmd/retina-adsb-agent/
  cmd/retina-feed-forwarder/
  internal/canonical/
  internal/fusion/
  internal/history/
  internal/readsbjson/
  internal/sbs/
  deploy/server/
  deploy/node/
  config/schema/
  test/fixtures/
  test/integration/
  test/replay/
  dashboards/
  runbooks/
  docs/adr/
```

Implementation language is a team decision. Go is a reasonable default for small static services, gRPC, concurrency, and cross-compilation; Rust is also appropriate where parser hardening is prioritized. All components need not share a language, but shared canonical/fusion behavior MUST be driven by common fixtures and contract tests.

---

## 31. Required architecture decision records

Before code freeze, the team MUST approve ADRs for:

1. implementation language(s) and shared-library boundaries;
2. exact pinned `readsb` build and packaging method;
3. server container/system-service platform;
4. certificate authority, enrollment, renewal, and revocation;
5. local API transport and solver client language;
6. spatial index implementation (simple grid/geohash/H3/R-tree);
7. whether v1 central history remains memory-only;
8. exact interpretation of ancillary-field timestamps from `readsb` JSON;
9. local `Watch` initial-snapshot and reconnect semantics;
10. provider timestamp trust policy and ADSBHub latency penalty;
11. licensing/SBOM conclusions;
12. SLO reference hardware and traffic model;
13. default region radius and decimation tiers derived from the solver detection envelope (the 150 km/100 km/1 s shipped defaults are placeholders pending this ADR).

These ADRs may select alternatives, but must preserve the goals and acceptance criteria unless this specification is versioned and re-approved.

---

## 32. Operational runbook minimums

Each runbook contains symptoms, dashboards, safe diagnostics, decision tree, remediation, rollback, verification, and escalation.

Required runbooks:

- ADSBHub disconnected/no data;
- ADSBHub parse-error surge after format change;
- `readsb` upgrade rollback;
- central distribution outage;
- node local receiver loss;
- fleet central reconnect storm;
- snapshot/sequence-gap storm;
- contribution authentication/UUID failure;
- certificate rotation failure;
- clock offset/freshness anomaly;
- memory/queue pressure;
- accidental public raw-port exposure;
- emergency disable of contribution while retaining local/central consumption.

Feature flags MUST permit independent disablement of:

- ADSBHub ingest;
- node contribution per node/cohort;
- central consumption per cohort;
- interpolation/extrapolation;
- optional ancillary field groups;
- a newly introduced protocol minor feature.

Disabling contribution or central consumption MUST never disable local agent service.

---

## 33. Implementation notes and cautions

1. `aircraft.json` is acceptable for diagnostics/bootstrap comparison, not the low-latency path; it is normally generated periodically, while `--net-json-port` emits per-position objects.
2. `seen` is age of any message and MUST NOT substitute for `seen_pos`.
3. `readsb` data formats can change between versions; pin and test them.
4. Beast receiver timestamps are not automatically wall-clock UTC. Do not label them as such without a validated mapping.
5. ADSBHub SBS timestamps may reflect upstream receiver clocks and timezone behavior. Keep the raw parsed value as diagnostics but default freshness to conservative receipt-based time until validated.
6. A central aggregate can return a node’s own report. This is expected and harmless when source tables remain separate.
7. Avoid metric labels for aircraft or unrestricted node identity; they create operationally dangerous cardinality.
8. “No aircraft” is plausible. Use connection/byte/heartbeat/decoder health rather than aircraft count alone.
9. Do not equate geometric and barometric altitude.
10. Do not return extrapolated data as observed data; method and uncertainty are mandatory.
11. Do not let reconnect replay a deep backlog as current. Old updates age out or are discarded before live selection.
12. Keep central update logic replaceable: the node contract must survive adding or replacing providers.

---

## 34. Definition of done

The project is done for v1 when:

- all mandatory services, schemas, installers, dashboards, alerts, and runbooks are versioned in the repository;
- every acceptance criterion in Section 29 has linked automated or witnessed evidence (staged per the §29 gates during rollout);
- the solver uses only the node-local API on the production cohort;
- default node installation demonstrably leaves existing tar1090/readsb operation intact;
- local operation survives central loss and central fallback survives local receiver loss;
- freshness/source/timing metadata reaches the solver and is enforced;
- security review, license review, capacity test, soak test, canary, and rollback rehearsal are complete; and
- an on-call operator can diagnose and safely mitigate each documented failure without modifying code.

---

## Appendix A. Selection truth table

Assume the position tie window is 250 ms, integrity is acceptable unless noted, and no candidate is currently selected (`current = none`). While central is currently selected, the local tie-break threshold tightens to `window − switch_advantage` (150 ms default; §13.4).

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

---

## Appendix B. Minimum fusion test vectors

1. Equal timestamps, local and central identical → local.
2. Equal timestamps, coordinates differ within tolerance → local plus discrepancy metric.
3. Equal timestamps, coordinates imply impossible separation → reject suspect candidate per continuity rules.
4. Central 249 ms newer → local.
5. Central 251 ms newer but current local and switch advantage 100 ms → central because total advantage exceeds both rules as configured.
6. Central 300 ms newer, central fails range validation → local.
7. Local expires between input messages → timer-triggered central selection.
8. Central deletes aircraft while local remains fresh → local remains.
9. Local disappears while central copy exists → central selected without empty intermediate state where event ordering permits.
10. Both expire → position removed and deletion/update emitted.
11. Late local sample enters history but does not replace live newer position.
12. Server epoch changes with lower event ID → resnapshot, not regression error against old epoch.
13. Central currently selected, local age deficit oscillating 140–260 ms across updates → selection returns to local only when the deficit is ≤150 ms; no flapping at the 250 ms boundary.
14. Same broadcast arrives via local and central at identical coordinates with observation-time estimates 300 ms apart → history stores one kinematic sample (§17) and interpolation uses a single-source bracket; no near-zero-velocity artifact.
15. ADSBHub-derived position with 400 ms transport age → selectable as fallback after the §10.5 penalty, but `usable_for_precision_association=false` with `provider_latency_unknown` set.

---

## Appendix C. Source references

- [`readsb` repository and network options](https://github.com/wiedehopf/readsb)
- [`readsb` JSON/API field documentation](https://github.com/wiedehopf/readsb/blob/dev/README-json.md)
- [tar1090 repository](https://github.com/wiedehopf/tar1090)
- [combine1090 non-interfering aggregation pattern](https://github.com/wiedehopf/combine1090)
- [adsb-ultrafeeder repository](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder)
- [ADSB.lol infrastructure](https://github.com/adsblol/infra)
- [ADSB.lol API](https://github.com/adsblol/api)
- [ADSBHub aggregate feed access and terms](https://www.adsbhub.org/howtogetdata.php)
- [ADSBHub contribution setup](https://www.adsbhub.org/howtofeed.php)
- [gRPC health checking](https://grpc.io/docs/guides/health-checking/)
- [gRPC keepalive](https://grpc.io/docs/guides/keepalive/)
- [Protocol Buffers language/evolution guidance](https://protobuf.dev/programming-guides/proto3/)
- [Prometheus metric and label naming](https://prometheus.io/docs/practices/naming/)
- [OpenTelemetry signals](https://opentelemetry.io/docs/concepts/signals/)
