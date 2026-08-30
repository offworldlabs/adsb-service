# Integration suite — live end-to-end against the real pinned readsb

## How to run

```sh
cd /root/adsb-service
.venv/bin/pytest -m integration -q
```

The default `pytest` run excludes these tests (`addopts = -m 'not integration'`
in `pyproject.toml`); the explicit `-m integration` selects them. Add `-s` to
see the measured honest-`seen_pos` and quantization values printed as they are
observed.

**Expected duration: ~60–90 seconds** for the whole module (one shared stack,
module-scoped fixture; the upstream-failure/recovery test dominates).

## What it drives

Real processes on this host, wired exactly like production (DESIGN.md §1),
with all ports allocated fresh per run:

1. **Synthetic ADSBHub** — in-process threaded TCP server replaying the golden
   SBS fixtures (`test/fixtures/sbs/golden_msg_*.txt`) at ~10 lines/s with
   generated timestamps regenerated at send time (a `lag` control emits them
   N seconds in the past); supports verbatim injection of raw lines.
2. **adsbhub-adapter** — subprocess (`python -m adsbhub_adapter`) with a temp
   strict-YAML config (defaults except a 5 s backoff cap to bound the recovery
   test).
3. **readsb** — the pinned binary `vendor/readsb/readsb` (v3.16.16 d9a4c62),
   dialing the adapter relay via `--net-connector=...,sbs_in`, re-api on a
   test port.
4. **serving-api** — subprocess (`python -m serving_api`), `/v2` mapped onto
   the readsb re-api `&jv2` envelope.

Teardown terminates all processes; on any test failure each process's stderr
tail is dumped.

## What it proves (DESIGN.md §7 integration gates)

| Test | DESIGN.md §7 claim proven |
|---|---|
| `test_envelope_shape` | v2 envelope shape: `ac` array, integer ms `now`, `msg`/`total`, per-aircraft `hex`/`lat`/`lon`/float `seen_pos`, `type:"other"`, **no `alt_geom`** (plan §0 req 3/4) |
| `test_ground_aircraft` | SBS ground flag −1 serves as the `alt_baro:"ground"` **string** (node-proxy expectation, plan §0 req 4) |
| `test_honest_seen_pos` | **plan §0 req 1**: 3.0 s of injected upstream lag appears in served `seen_pos` (≥ 3.0) — honest age including upstream+ingest latency |
| `test_quantization_measurement` | spec §10.4: a 7 ms generated-timestamp offset is preserved end-to-end (`seen_pos` deltas exactly 7 ms) ⇒ `source_timestamp_quantization_ms` = 1 ms; result recorded into `docs/readsb-pin.md` so the number regenerates on any readsb upgrade |
| `test_stale_guard` | ADR-0004: a 120 s-old record is rejected (`records_total{result="stale"}`) and never appears in the served state — readsb's silent stale-looks-fresh clamp cannot be reached |
| `test_malformed_resilience` | spec §12.1: the 20-line malformed corpus is dropped + counted by reason while the stream, the relay connection, and serving continue uninterrupted |
| `test_rate_limit` | plan §0 req 5: hammering `/v2` above the configured rate yields 429 + `Retry-After` |
| `test_relay_closes_on_upstream_failure` | spec §6.1 S1: upstream kill ⇒ adapter `/healthz` 503, relay listener refuses connections, served `seen_pos` only grows (no fresh data manufactured); upstream restart ⇒ full recovery, fresh data flows |
| `test_health_chain` | `/health` composition: 200 with the full chain up, 503 once readsb stops |

## Notes / gotchas

- readsb suppresses duplicate consecutive positions for ~2 s, so aircraft fed
  a **fixed** position show a `seen_pos` sawtooth up to ~3–4 s even on a fresh
  feed. Tests that need tight `seen_pos` bounds move their aircraft ~11 m per
  tick to defeat this; freshness thresholds on fixed golden aircraft are
  deliberately loose (< 6 s).
- The quantization pair derives both timestamps from a single base clock with
  integral-millisecond offsets, so the served delta is exact regardless of the
  sub-ms phase of the send time.
- Tests share one process stack, so they are order-dependent by design: the
  destructive tests (upstream kill/recovery, readsb kill) run last.
- The serving temp config raises the rate limit to 5 req/s (burst 20) so the
  suite's own polling never trips it; `test_rate_limit` hammers far above that
  and then pauses to let the bucket refill.
