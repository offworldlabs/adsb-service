# Runbook: SBS parse-error surge

Alerts: **ParseErrorSurge** (warning — reject ratio >5% over 5 m, >100 records; ADR-0006),
**CentralStateStalled** (critical — bytes flow but readsb holds zero positioned aircraft,
spec §14.4 row 2; routed here because format drift is the usual cause).

## Symptoms

- ParseErrorSurge firing; pipeline dashboard "records rate by validation result" shows
  non-`ok` results growing.
- Or CentralStateStalled: adapter passes lines but readsb rejects them
  (`readsb_messages_basestation_invalid` rising on the pipeline dashboard) or readsb
  is broken (then see `runbooks/readsb-upgrade-rollback.md`).
- Feed keeps flowing regardless: malformed lines are dropped and counted, never
  terminate the stream (spec §12.1).

## Likely causes

- **ADSBHub format change** (spec §24 row 2): field order/count, timestamp format,
  new delimiters.
- **New MSG variant**: message type outside the tested {1..8} set, or a subtype with
  field semantics we do not map (spec §12.4: unknown variants are counted and ignored
  until a fixture and mapping are added).
- **Corruption**: truncated lines, control bytes, mid-line reconnect splices.
- **Unpadded ICAO from the provider** is NOT a cause: ADSBHub omits the leading zeros
  of addresses below 0x100000 and the adapter pads them before relay (ADR-0007). Watch
  `retina_adsb_provider_records_normalized_total{normalization="icao_zero_pad"}` — a
  sudden change in its ratio to `records_total` (baseline ≈2.5% of records) is a
  provider-format-change signal in its own right, in either direction: a drop to zero
  means the provider started padding, a jump means a new address block or a new
  field-shape.
- **Clock trouble** (`stale`/`future` results): upstream receiver clock skew, or OUR
  wall clock is wrong — check `timedatectl` before blaming the provider.

## Diagnosis

1. Reject-reason breakdown:
   ```
   curl -sS 127.0.0.1:9101/metrics | grep 'retina_adsb_provider_records_total'
   ```
   | result | meaning | typical cause |
   |---|---|---|
   | `invalid_structure` | not MSG / field count <22 / bad ICAO (0, >6 hex, non-hex) / length / control bytes | format change, corruption, new variant |
   | `invalid_range` | field outside spec §12.2 ranges | unit/format change in a numeric field |
   | `stale` | generated ts older than `max_record_age` (60 s) | provider backlog replay, clock skew |
   | `future` | generated ts > 1 s ahead of wall clock | upstream or local clock skew |
   | `unparseable_ts` | fields 7/8 unparseable (still relayed, counted) | timestamp format change |
2. Cross-check readsb's view (adapter-passed lines readsb still rejects):
   `curl -sS 127.0.0.1:9102/metrics | grep basestation`
   Also read the normalization counter alongside the result breakdown:
   ```
   curl -sS 127.0.0.1:9101/metrics | grep 'retina_adsb_provider_records_normalized_total'
   ```
3. **Bounded diagnostic capture** (spec §21.5: never log payloads by default; a capture
   requires operator reason, duration limit, access audit, deletion schedule).
   The adapter knob (DESIGN.md §4, `diagnostics` block of the adapter config —
   `sample_reason` is mandatory when enabling, the service refuses to start without it):
   ```yaml
   # /etc/retina-adsb/adapter.yaml (deploy/install.sh path)
   diagnostics:
     sample_enabled: true
     sample_reason: "INC-<id>: ParseErrorSurge triage, <operator>"
     sample_duration: 60      # seconds — keep minimal
     sample_max_lines: 100
   ```
   `systemctl restart adsbhub-adapter`, let the window elapse, read the sampled lines
   (logged truncated to 64 bytes each, tagged with reject reason) from
   `journalctl -u adsbhub-adapter | grep 'diagnostic sample'`, then **revert the config
   block and restart again**.
   Append to the capture log (`docs/capture-log.md`, create if absent):
   ```
   | date | operator | reason | duration | max_lines | where stored | who accessed | delete by |
   | 2026-08-29 | jdoe | INC-42 ParseErrorSurge triage | 60s | 100 | journald (unit adsbhub-adapter) | jdoe | 2026-09-12 |
   ```
4. Variant analysis on the sample: which field/position changed? New MSG type? Compare
   against golden fixtures in `test/fixtures/sbs/`.

## Remediation

- **Corruption / provider hiccup**: nothing to change — drops are the correct behavior;
  watch the ratio recover.
- **Clock skew local**: fix NTP (`timedatectl`, `systemctl restart systemd-timesyncd`).
- **Legit new variant / format change** — fixture-first process (spec §12.4/§26.2):
  1. Add a golden fixture line per new subtype to `test/fixtures/sbs/`, plus malformed
     counter-cases.
  2. Extend the validator mapping in `services/adsbhub_adapter/sbs.py`; unit tests for
     accept + reject; fuzz corpus reruns (`.venv/bin/pytest`) — fuzzing is a release
     gate (spec §21.3).
  3. Replay comparison before rollout (spec §26.6 — required for any parser change):
     see the shadow-replay procedure in `runbooks/readsb-upgrade-rollback.md`, replaying
     through old and new **adapter** builds.
  4. Deploy; verify reject ratio drops and `ok` rate is unchanged or higher.
- **Poisoned/garbage feed and triage needs quiet**: disable ADSBHub ingest independently
  (the spec §32 feature flag, implemented as the unit): `systemctl stop adsbhub-adapter`.
  The relay closes, readsb ages out — consumers see honest staleness, not junk.

## Rollback

If a new adapter build makes it worse: reinstall the previous build
(`pip install` of the prior commit in the venv, or restore the previous release
artifact per spec §20.6 retain-current-and-previous), `systemctl restart adsbhub-adapter`.

## Verification

```
curl -sS 127.0.0.1:9101/metrics | grep records_total    # ok dominates, surge results flat
curl -sS 127.0.0.1:9102/metrics | grep basestation_invalid
curl -sS 127.0.0.1:8080/health                          # 200
```
ParseErrorSurge resolves; diagnostics block reverted (`grep sample_enabled /etc/retina-adsb/adapter.yaml` → false/absent);
capture log entry completed with deletion date.

## Escalation

- Confirmed ADSBHub format change with no documented notice: contact ADSBHub
  (see `runbooks/adsbhub-disconnected.md` escalation) and record the change in
  `docs/conformance-ledger.md`.
- Mapping decision needed for a new variant (which fields are trustworthy): project owner.
