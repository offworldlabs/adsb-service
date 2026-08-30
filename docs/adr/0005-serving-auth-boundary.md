# ADR-0005: Serving-tier auth boundary

## Status

**Proposed** — pending the Q5 stakeholder decision (plan §5).
Decision deadline: **before the first node cutover** (nothing may consume the
endpoint across the network until then; Q7 + soak gate cutover anyway).

## Context

AC-S-01 forbids unauthenticated public exposure of readsb-derived endpoints:
"Public scan exposes only approved TLS endpoints; no raw readsb, metrics,
admin, or node-local ports." readsb's re-api has **no** auth and **no** rate
limiting; the interim REST endpoint is not in the AI-06 port enumeration
(443 gRPC / 7444 contribution), so its exposure is itself a recorded
divergence the spec amendment covers. Q5 asks: mTLS (like the eventual §11
contract) or private network + firewall?

## Decision (interim, holds regardless of Q5 outcome)

**Default closed.** Every listener binds loopback (adapter relay 31003,
adapter metrics 9101, re-api 30152, serving-api 8080). `install.sh` opens no
firewall ports and enables no front. Out of the box the host exposes exactly
zero service ports — AC-S-01 is satisfied trivially until an operator
enables a boundary.

Two boundary options ship as nginx configs (`deploy/nginx/`), NOT enabled by
install:

1. **`mtls.conf` — nginx mTLS (recommended).** Listen 443, TLS with client
   certificate required, proxy to 127.0.0.1:8080. Recommended because it
   aligns with the eventual §11 contract (gRPC + mTLS on 443) and the §21.1
   per-node identity model — nodes acquire client certs once and keep them
   through the Phase B migration.
2. **`private-net.conf` — private network + firewall.** Bind a private
   interface (VPC/WireGuard), source-IP allowlist, host firewall default-deny
   on public interfaces.

## AC-S-01 analysis of each option

| Option | Public scan result | AC-S-01 verdict | Costs / residual risk |
|---|---|---|---|
| Default closed (shipped) | nothing | satisfied trivially | no consumer can connect; fine until cutover |
| nginx mTLS | one TLS endpoint on 443 refusing clients without certs | satisfied — "only approved TLS endpoints" literally | needs a CA + issuance/renewal procedure (pulls §31 ADR #4 CA/PKI scope into Phase A) and cert distribution to nodes; strongest option, per-node identity, revocable |
| private net + firewall | nothing on public interfaces | satisfied **only if** the private network is actually private and the firewall is verified (external scan, A3 runbook check) | no per-node identity or revocation; any host on the private net reaches the endpoint unauthenticated; misconfiguration (interface bind, firewall flush) silently voids AC-S-01 — the accidental-exposure runbook applies |

Either way: the external port-scan verification (AC-S-01 "verify with an
actual scan") is part of A3 and re-run after any network change; per-client
rate limiting stays in serving-api regardless of the front.

## Consequences

- No node cutover until Q5 is decided and the chosen front is enabled and
  scan-verified.
- If mTLS is chosen, a minimal CA/enrollment ADR must follow (ADR #4 scope);
  if private-network is chosen, node onboarding needs network provisioning
  instead, and the Phase B mTLS migration is a bigger step later.
- tar1090 operator visualization sits behind the same boundary (spec §6.1
  S3: authenticated operator network only, never a node contract).
