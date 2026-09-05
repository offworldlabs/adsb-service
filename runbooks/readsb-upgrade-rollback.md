# Runbook: readsb upgrade / rollback

Covers upgrading the pinned readsb-hub build and rolling it back (spec §12.5, §20.6,
§26.6; ADR-0002).

## Pin discipline

- Current pin (ADR-0002): **wiedehopf/readsb v3.16.16, commit `d9a4c62`**, plain build
  (`make -j$(nproc) OPTIMIZE="-O2"`, all SDR backends off, `WITH_UUIDS=no`).
- Installed as `/usr/local/bin/readsb-retina` by `deploy/build-readsb.sh`, which
  verifies the reported git hash and refuses on mismatch.
- Check anytime: `/usr/local/bin/readsb-retina --version`
  → `readsb version: 3.16.16 wiedehopf git: d9a4c62`.
- Never upgrade by editing the unit or building ad hoc; a new pin means a new
  ADR-0002 revision plus the gate below. An upgrade is **BLOCKED** if golden fixtures
  or the shadow replay show incompatible mapping, parse-error increase, or freshness
  regression (spec §12.5).

## Pre-upgrade: shadow-replay gate (spec §12.5 / §26.6)

Run entirely off-path; production units untouched.

1. Corpus: a recorded representative SBS capture, ≥30 minutes (spec §26.6). Captures
   are governed by spec §21.5 — reason, duration, access, deletion logged
   (see `runbooks/parse-error-surge.md` step 3). Short goldens live in
   `test/fixtures/sbs/`; the 30-min corpus is operator-recorded.
2. Build the candidate without installing. `build-readsb.sh` refuses any
   non-pin commit unless the candidate is named explicitly — `--candidate`
   verifies HEAD is exactly that sha, prints a SHADOW BUILD banner, and only
   works with `--build-only` (a candidate is never installed):
   ```
   cd /root/adsb-service/vendor/readsb && git fetch && git checkout <candidate-sha>
   deploy/build-readsb.sh --build-only --candidate <candidate-sha>
   ```
3. Run candidate on shadow ports (nothing overlapping DESIGN.md §2):
   ```
   mkdir -p /tmp/readsb-shadow
   vendor/readsb/readsb --net --quiet --net-bind-address=127.0.0.1 \
     --net-sbs-in-port=33006 --net-api-port=33152 --max-range=99999 \
     --write-json=/tmp/readsb-shadow --write-json-every=1 --json-reliable=1 \
     --write-prom=/tmp/readsb-shadow/stats.prom
   ```
4. Replay the corpus into BOTH builds (pinned build the same way on e.g. 34006/34152)
   at recorded pacing, e.g.:
   ```
   .venv/bin/python - <<'EOF'
   import socket, time, sys
   s = socket.create_connection(("127.0.0.1", 33006))
   for line in open("corpus.sbs", "rb"):
       s.sendall(line); time.sleep(0.005)   # ~200 lines/s; match capture rate
   EOF
   ```
5. Diff old vs new (spec §26.6 comparison set):
   - `aircraft.json` snapshots and `curl -sS '127.0.0.1:33152/?all_with_pos&jv2'`
     against the recorded goldens in `test/fixtures/api/`;
   - aircraft count, accepted/dropped counts (`stats.prom`:
     `readsb_messages_basestation_valid/invalid`, `readsb_position_count_*`),
     coordinates, `seen`/`seen_pos` age distributions;
   - every §12.5 mapped field still present with the same type — including
     `alt_baro:"ground"` as a string and NO `alt_geom` for SBS aircraft;
   - **ICAO parity (ADR-0007):** `decodeSbsLine` still requires a 6–7 char ICAO
     field and still reads exactly 6 hex digits after an optional `~`
     (`grep -n 'icao must be 6 characters' vendor/readsb/net_io.c`), and
     `json_out.c` still keys `hex` as `%06x`. The adapter's zero-padding exists
     because of those two facts; if either changed, re-verify the padding rule
     (and the post-padding 199-byte length re-check) before upgrading.
6. Run the integration suite against the candidate binary, and manually inspect any
   statistically significant difference (spec §26.6): `.venv/bin/pytest -m integration`.
7. Gate: **unexplained loss, stale-age regression, or implausible positions ⇒ no upgrade.**
   Attach the comparison to the change (CI gate 8, spec §27).

## Upgrade steps

```
cp /usr/local/bin/readsb-retina /usr/local/bin/readsb-retina.prev   # §20.6: retain previous build
deploy/build-readsb.sh            # builds pinned commit per updated ADR-0002 and installs
systemctl restart readsb-hub
```
Only readsb-hub restarts: the adapter keeps its upstream session (readsb re-dials the
relay within `--net-connector-delay` 5 s) and serving-api is stateless per request —
no restart order beyond readsb-hub itself. Expect ≤~60 s of state repopulation.

## Rollback

```
install -m 0755 /usr/local/bin/readsb-retina.prev /usr/local/bin/readsb-retina
systemctl restart readsb-hub
```
No `.prev` retained? Re-checkout the pin and reinstall:
```
cd /root/adsb-service/vendor/readsb && git checkout d9a4c62
deploy/build-readsb.sh
systemctl restart readsb-hub
```
Restart order after rollback: `readsb-hub` only, then verify; restart
`retina-serving-api` only if `/health` stays 503 after readsb is confirmed healthy.

## Verification

```
/usr/local/bin/readsb-retina --version            # expected pin
systemctl status readsb-hub
curl -sS '127.0.0.1:30152/?status'                # 200
curl -sS '127.0.0.1:30152/?all_with_pos&jv2' | head -c 300 ; echo
curl -sS 127.0.0.1:8080/health                    # 200 once repopulated
curl -sS 127.0.0.1:8080/v2/lat/37.62/lon/-122.38/dist/50 | head -c 300 ; echo
```
Dashboards: `readsb_aircraft_with_position` recovers; basestation invalid rate flat;
freshness p95 unchanged vs pre-upgrade. Alerts quiet for 30 min before declaring done.

## Escalation

- Gate failure that looks like an upstream readsb behavior change: record in
  `docs/conformance-ledger.md`, stay on the pin, raise with project owner
  (spec risk: readsb upstream drift).
