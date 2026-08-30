# API fixtures — provenance

| File | Provenance |
|---|---|
| `jv2_envelope.json` | **Synthetic (hand-written)** — NOT a recording. The field inventory and value shapes were sourced from a study of the vendored readsb/adsb.lol code (`vendor/readsb/json_out.c`, `vendor/adsblol-api`); values are invented. Used by the unit tests (`test/unit/test_serving_*.py`) as the mocked upstream body for the verbatim-passthrough serving tier. |
| `jv2_envelope_recorded.json` | **Recorded from the live pinned readsb** — the actual response body served end-to-end (synthetic ADSBHub → adapter → pinned readsb → serving-api) by `test/integration/test_e2e.py::test_envelope_shape`, which overwrites this file on every integration run (same regeneration pattern as `docs/readsb-pin.md`). Regenerate with `.venv/bin/pytest -m integration -q`. Timestamps and volatile values differ per run by construction. |

The binding envelope-shape evidence is the live e2e assertion plus the
recorded file, not the synthetic one; the synthetic fixture exists only so
unit tests can run without the readsb binary.
