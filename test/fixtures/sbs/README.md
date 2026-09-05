# SBS fixtures — provenance

| File | Provenance |
|---|---|
| `golden_msg_1.txt` … `golden_msg_8.txt` | **Synthetic (hand-written)** — NOT a recording. One line per SBS transmission type (MSG,1 … MSG,8), shaped from the field inventory in the vendored readsb (`vendor/readsb/net_io.c` `decodeSbsLine`); ICAO addresses, callsigns and positions are invented. Timestamps are fixed at 2026/08/29 12:00:00 UTC, the reference clock the unit tests use. |
| `malformed_corpus.txt` | **Synthetic (hand-written)** — one deliberately malformed line per rejection reason, in the exact order asserted by `CORPUS_EXPECTED` in `test/unit/test_sbs_validator.py`; also seeds the hypothesis corpus in `test/unit/test_sbs_fuzz.py`. Appending a line requires appending its expected result there. |
| `golden_icao_unpadded.txt` | **Recorded from the live ADSBHub feed on 2026-09-05** — three consecutive real lines for Mexican registration 0D0EEC (Volaris, callsign EUS2002), verbatim as the provider sent them: the ICAO field arrives as `D0EEC`, without its leading zero. This is the evidence behind ADR-0007 (`docs/adr/0007-icao-zero-padding.md`); the adapter left-zero-pads the field to `0D0EEC` before relay. Do not "fix" the fixture — its unpadded ICAO is the point. |
