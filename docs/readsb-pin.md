# readsb pin record

> Pin authority: ADR-0002. Upgrade gate: spec §12.5 fixtures + ≥30-min shadow
> replay, procedure in `runbooks/readsb-upgrade-rollback.md`.

## Pinned build

| Item | Value |
|---|---|
| Upstream | wiedehopf/readsb (vendored at `vendor/readsb/`) |
| Version | 3.16.16 |
| Commit | `d9a4c62655490e70d07704e207738bb9c6cffde1` (d9a4c62), committed 2026-08-17 |
| Self-report | `readsb version: 3.16.16 wiedehopf git: d9a4c62` |
| Build command | `make -j"$(nproc)" OPTIMIZE="-O2"` (via `deploy/build-readsb.sh`) |
| Build flags | all SDR backends off (Makefile defaults); `WITH_UUIDS` unset (verified & declined, ADR-0002); no LTO; base CFLAGS already `-O2`, `OPTIMIZE` appended to compile+link |
| Build deps | build-essential, pkg-config, libncurses-dev, zlib1g-dev, libzstd-dev |
| License | GPL-3.0 (`vendor/readsb/COPYING`; boundary analysis in `docs/licenses.md`) |

ldd of the built binary (Ubuntu 24.04, gcc 13.3.0):

```
linux-vdso.so.1
libm.so.6
libzstd.so.1
libz.so.1
libncurses.so.6
libtinfo.so.6
libc.so.6
/lib64/ld-linux-x86-64.so.2
```

No librtlsdr/libusb — network-only hub binary as intended.

## Timestamp handling (SBS input) — pinned facts

Source: `docs/requirements/code-readsb.md` (file:line refs verified against
the vendored tree); load-bearing for ADR-0004 honest age.

- Fields 7/8 ("date/time message generated") are parsed with strptime
  `%Y/%m/%d %H:%M:%S` plus separately-parsed milliseconds, interpreted as
  **UTC** via `timegm` → `mm->sysTimestamp` in ms (net_io.c:3135-3153).
  Fields 9/10 ("logged") are ignored.
- Clamp windows (net_io.c:3154-3161): parsed timestamp > arrival + **1 s** or
  < arrival − **20 min** → silently rewritten to arrival wall clock;
  unparseable date → arrival wall clock.
- All aircraft clocks (`a->seen`, `a->seen_pos`, validity `updated`) derive
  from `mm->sysTimestamp`; JSON/api writers subtract from wall clock at write
  time — so served ages include upstream latency (verified live: 3.0 s
  injected lag → `seen_pos` 3.414).
- The adapter's timestamp guard (future > 1 s, stale > 60 s → reject+count;
  unparseable → relay+count) bounds what reaches these clamps (ADR-0004,
  ADR-0006 §5).

## Quantization (spec §10.4 `source_timestamp_quantization_ms`)

- **Input precision:** SBS field 8 carries milliseconds, preserved into
  `sysTimestamp` — source timestamps on this path are ms-precision, not the
  "commonly 100 ms" §10.4 cites for Beast paths.
- **Output resolution:** `seen` rendered at 0.1 s (`%.1f`,
  json_out.c:820-821); `seen_pos` at 1 ms (`%.3f`, json_out.c:730-732).
  readsb therefore bounds `seen_pos` quantization below at **1 ms**; the
  effective value is whatever ADSBHub actually emits in field 8.
- **Measured value** (integration test feeds known-timestamp lines through
  the pinned build and records observed quantization):

| Quantity | Measured value | Measured by / date |
|---|---|---|
| `source_timestamp_quantization_ms` (SBS field-8 path, pinned build) | **1 ms** (injected 7 ms generated-ts offset served as [7] ms `seen_pos` deltas within single api snapshots) | `test/integration/test_e2e.py::test_quantization_measurement`, 2026-09-05 |
| `seen_pos` output resolution observed | 1 ms (all sampled values integral in ms) | `test/integration/test_e2e.py::test_quantization_measurement`, 2026-09-05 |
| `seen` output resolution observed | 100 ms (all sampled values integral in 0.1 s) | `test/integration/test_e2e.py::test_quantization_measurement`, 2026-09-05 |

> NOTE for the integration agent: record the measured value here AND keep the
> measuring test in `test/integration/` so the number regenerates on any
> readsb upgrade (the §10.4 CI validation of dependent constants is Phase B,
> but the measurement itself is a Phase A A0 deliverable). Verify ADSBHub's
> real field-7/8 population, UTC-ness, and ms quantization against a live
> capture when the entitled feed is available (code-readsb.md hazard note).

## ICAO field handling (SBS input) — pinned facts

- `decodeSbsLine` requires `6 <= strlen(field 5) <= 7` (net_io.c:3112) and then
  reads **exactly 6 hex digits** after an optional `~` (net_io.c:3113-3125);
  json/api writers re-key the aircraft as `%06x` (json_out.c:626/845).
- ADSBHub sends addresses below 0x100000 unpadded (`D0EEC`), which that rule
  rejects outright, so the adapter left-zero-pads the field before relay
  (ADR-0007). Both facts are re-checked at every pin bump — see the ICAO
  parity item in `runbooks/readsb-upgrade-rollback.md`.

## Golden fixtures

- `test/fixtures/sbs/` — one golden line per MSG subtype 1–8 + malformed
  corpus (ADR-0006 §4) + `golden_icao_unpadded.txt`, a real 2026-09-05 ADSBHub
  capture of the unpadded-ICAO form (ADR-0007); provenance in
  `test/fixtures/sbs/README.md`.
- `test/fixtures/api/` — recorded jv2 envelope from the pinned build
  (plan §0 req 4 verification surface).
- Fixtures are pinned to actual d9a4c62 behavior including upstream quirks
  (field-20 emergency else-branch reads t[21], net_io.c:3280-3286; SBS yields
  `nic:0`, `rc:0`, placeholder `rssi:-49.5`, never `alt_geom`).

## Invocation contract

Generated by `deploy/gen-readsb-args.py` from config (spec §18.3 "generate
and verify in integration tests"); the canonical arg set and the MUST-NOT
list (`--lat/--lon`, `--json-separate-ground`, second api port) are in
DESIGN.md §3.
