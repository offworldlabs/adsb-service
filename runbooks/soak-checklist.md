# Runbook: A3 72-hour shadow-soak checklist

Phase A exit procedure (plan.md A3; spec §28 Phase 1 exit, verbatim criteria:
"72-hour soak, no unbounded growth, parse error understood, freshness dashboards
operational"; pass condition per §26.7: no unbounded memory/FD growth, deadlock,
revision regression, or freshness drift). Shadow = no production node consumes
the serving tier during the soak.

## Setup (t0)

```
systemctl start adsbhub-adapter readsb-hub retina-serving-api readsb-file-exporter
curl -sS 127.0.0.1:9101/healthz && curl -sS 127.0.0.1:8080/health   # both 200
```
Prometheus loaded with `observability/prometheus/prometheus.yml` + `alerts.yml`;
dashboards `retina-adsb-pipeline` and `retina-adsb-freshness` imported and rendering
(this itself is the "freshness dashboards operational" exit criterion). Record t0.

## 1. No unbounded growth (check at t0, +24 h, +48 h, +72 h)

Per unit (`adsbhub-adapter`, `readsb-hub`, `retina-serving-api`):
```
for u in adsbhub-adapter readsb-hub retina-serving-api; do
  pid=$(systemctl show -p MainPID --value $u)
  echo "$u rss=$(systemctl show -p MemoryCurrent --value $u) fds=$(ls /proc/$pid/fd | wc -l)"
done
```
Record all four samples in the sign-off table. Pass: RSS plateaus well under
MemoryMax (adapter 256M, readsb 1G, api 256M — DESIGN.md §8); fd count flat after
warmup (relay clients bounded at 2; API uses one shared session). Any monotonic
climb across three samples = fail; capture `/proc/<pid>/status` + smaps and stop the soak.
Also: zero unexpected restarts — `systemctl show -p NRestarts --value <unit>` and
`journalctl -u <unit> --since '<t0>' | grep -ci oom` both stable/0.

## 2. Parse errors understood

```
curl -sS 127.0.0.1:9101/metrics | grep retina_adsb_provider_records_total
curl -sS 127.0.0.1:9102/metrics | grep basestation
```
Every non-`ok` result with a nonzero count gets a one-line explanation in the sign-off
table (use the reject-reason table in `runbooks/parse-error-surge.md`). "Understood"
means: cause identified, and either fixed, fixture-tracked, or accepted with rationale.
Unexplained reject class or any ParseErrorSurge firing during the soak = fail.

## 3. Coverage comparison vs an independent view

Spec §28 wants comparison against contributed test receivers — deferred to Phase B/C,
so Phase A uses an independent public view instead (**recorded divergence**, see
DESIGN.md §10 / conformance ledger). Procedure (sample ≥3× spread across the 72 h):
```
# same region, same instant; pick a region the contribution station covers
curl -sS '127.0.0.1:8080/v2/lat/<lat>/lon/<lon>/dist/100' > ours.json
curl -sS 'https://api.adsb.lol/v2/lat/<lat>/lon/<lon>/dist/100' > lol.json
.venv/bin/python -c "
import json
a={x['hex'] for x in json.load(open('ours.json'))['ac']}
b={x['hex'] for x in json.load(open('lol.json'))['ac']}
print(f'ours={len(a)} lol={len(b)} overlap={len(a&b)} only_ours={len(a-b)} only_lol={len(b-a)}')"
```
Expect ours ⊂ roughly lol for the region (ADSBHub sees fewer receivers); large
`only_ours` counts are implausible-position red flags. Record counts + overlap per
sample; explain outliers.

## 4. Latency characterization (Q7 — gates node cutover)

Read the freshness dashboard panel **"ADSBHub upstream delay vs node 5s budget"**
over the full 72 h, or query directly:
```
histogram_quantile(0.5,  sum by (le) (rate(retina_adsb_provider_record_delay_seconds_bucket[72h])))
histogram_quantile(0.95, sum by (le) (rate(retina_adsb_provider_record_delay_seconds_bucket[72h])))
histogram_quantile(0.99, sum by (le) (rate(retina_adsb_provider_record_delay_seconds_bucket[72h])))
```
Record p50/p95/p99 (and the worst 1 h window) in `docs/q7-latency-characterization.md`,
against the node's hard 5 s extrapolation budget (plan.md §0 req 2). p95 near/above
5 s ⇒ flag to stakeholders: node cutover blocked pending Q7 decision. Also verify no
freshness drift: p95 in hour 72 comparable to hour 1 (§26.7).

## 5. External port-scan check (AC-S-01)

On the host — the ONLY listeners must be loopback:
```
ss -tlnp
```
Expected: `127.0.0.1:9101` (adapter), `127.0.0.1:8080` (api), `127.0.0.1:9102`
(file exporter), `127.0.0.1:30152` (re-api),
`127.0.0.1:31003` (relay — present only while upstream healthy), plus sshd and
Prometheus if local. `127.0.0.1:32006` (readsb debug SBS inject) must NOT be
listening — `sbs_debug_port` ships disabled; seeing it means a test/debug
overlay leaked into production config. Anything bound `0.0.0.0`/`[::]` other
than sshd = fail.

From ANOTHER host:
```
nmap -Pn -p- <droplet-public-ip>
```
Expected open: ssh (and nothing else — the nginx front stays disabled until Q5 is
resolved). Any RETINA port visible = fail, fix firewall/binds before sign-off.

## 6. Alert hygiene during the soak

No ADSBHubDown/CentralStateStalled/ParseErrorSurge/AdapterHealthEndpointDown firing
except during explained provider outages; every firing gets a line in the sign-off
notes (when, why, runbook followed).

## Sign-off

| # | Criterion | Evidence (link/values) | Pass | Operator | Date |
|---|---|---|---|---|---|
| 1 | 72 h continuous run, no unexpected restarts | | | | |
| 2 | No unbounded RSS/fd growth (4 samples × 3 units) | | | | |
| 3 | Parse errors understood (all reject classes explained) | | | | |
| 4 | Coverage vs adsb.lol sampled region (≥3 samples) | | | | |
| 5 | Q7 latency recorded in docs/ (p50/p95/p99 vs 5 s) | | | | |
| 6 | Freshness dashboards operational, no freshness drift | | | | |
| 7 | Port scan clean (local ss + external nmap) | | | | |
| 8 | Alert log reviewed, all firings explained | | | | |

All rows pass ⇒ Phase A exit evidence complete; file this table + Q7 doc with the
conformance ledger. Any fail ⇒ fix, then restart the 72 h clock for criteria 1–2.
