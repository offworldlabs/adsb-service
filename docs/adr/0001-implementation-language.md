# ADR-0001: Python for Phase A server components

## Status

Accepted (plan Q4 default; stakeholder confirmation still open — see Revisit trigger).

## Context

Plan §5 Q4 asks whether the new adapter code should be Python (following the
adsb.lol lineage) or a hardened Go/Rust component. Plan A1 sets the default:
"Language: follow the adsb.lol base (Python) unless parser-hardening review
says otherwise." Spec §31 ADR #1 requires this decision recorded before code
freeze. The Phase A server components are `adsbhub-adapter` (spec §6.1 S1) and
the interim `serving-api`; readsb-hub stays C (reused, not reimplemented,
AI-10).

The SBS feed from ADSBHub is untrusted input; spec §21.3 makes parser fuzzing
a release gate regardless of implementation language (plan §4 risk item,
AC-S-04).

## Decision

Both Phase A server components are Python 3.12 (`services/adsbhub_adapter/`,
`services/serving_api/`), single asyncio processes, stdlib logging, deps
fastapi/uvicorn/aiohttp/prometheus-client/pyyaml.

Rationale:

- **adsb.lol lineage reuse.** The serving tier is a derivative of vendored
  adsblol/api (BSD-3): the `reapi.py` client pattern, the v2 route → re-api
  `circle` mapping, the pydantic envelope models, and the aioresponses test
  approach carry over nearly verbatim (see `docs/requirements/code-adsblol-api.md`).
  A language switch forfeits that reuse and re-derives a working, deployed
  design from scratch.
- **asyncio fit.** The workload is purely I/O-bound: one long-lived upstream
  TCP client, a loopback relay listener, timers, and HTTP passthrough at
  ~1 Hz per node. All CPU-heavy work (Mode-S decode, CPR, dedup, geo query)
  lives in readsb per AI-10. Python's asyncio handles this shape with less
  code than Go/Rust and no measurable performance risk at Phase A volumes.
- **Memory safety.** Python removes the memory-corruption class from the
  untrusted-input surface; residual parser risks are logic errors and
  algorithmic DoS, which the mitigations below target.

Mitigations (§21.3 applies regardless of language):

- Line length capped at 4096 B **before** allocation (spec §18.2/§12.1).
- Hypothesis property fuzz over the SBS validator runs in default `pytest`
  (release gate, AC-S-04): arbitrary bytes never raise, never relay non-MSG,
  relayed lines re-validate; corpus seeded from golden + malformed fixtures.
- Bounded everything (AI-08): relay queues, rate-limit tables, reconnect
  backoff — no unbounded allocation paths.

## Revisit trigger

Phase B canonical components (`retina-adsb-server`, `retina-feed-gateway`)
re-open the language decision — they carry the performance-sensitive state
layer and the mTLS contribution surface. If the Q4 stakeholder review demands
a hardened language for the adapter, the validation rules, golden fixtures,
and fuzz corpus transfer as the contract tests for the replacement (spec §30:
shared canonical behavior driven by common fixtures).

## Consequences

- Single `pyproject.toml`, one venv, console scripts `adsbhub-adapter` and
  `retina-serving-api`.
- Fuzzing budget (corpus, harness, CI time) is a standing cost (plan §4).
- GIL/throughput irrelevant at Phase A load; any future bottleneck is a
  Phase B concern, not a reason to pre-harden now.
- Ledger rows: input-resource-protection, AC-S-04, s1-adapter-duties.
