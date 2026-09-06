# ADR-0005: Serving-tier auth boundary

## Status

**Accepted** — Q5 decided by the project owner on 2026-09-05: **option 3,
public TLS front with adsb.lol parity**, served at `adsb.retina.fm`.
Supersedes the Proposed version of this ADR, whose two options (mTLS,
private network) remain shipped and selectable.

## Context

AC-S-01 forbids unauthenticated public exposure of readsb-derived endpoints:
"Public scan exposes only approved TLS endpoints; no raw readsb, metrics,
admin, or node-local ports." readsb's re-api has **no** auth and **no** rate
limiting; the interim REST endpoint is not in the AI-06 port enumeration
(443 gRPC / 7444 contribution), so its exposure is itself a recorded
divergence the spec amendment covers. Q5 asked: mTLS (like the eventual §11
contract) or private network + firewall?

The node fleet is adsb.lol-native (ADR-0004): every consumer already polls
`GET /v2/lat/{lat}/lon/{lon}/dist/{radius}` against `https://api.adsb.lol`,
an **open, unauthenticated, rate-limited public HTTPS API**. Cutting nodes
over therefore needs nothing more than a base-URL change *if* the RETINA
endpoint behaves the same way. Both authenticated options add a node-side
enrollment step (client certificates, or VPN membership) that no node has
today.

## Decision

### Option 3 — public TLS front, adsb.lol parity (chosen)

`deploy/nginx/public.conf`, enabled by `deploy/enable-public-front.sh`:

- **TLS only** on 443 for `adsb.retina.fm` (Let's Encrypt, auto-renewed,
  TLS 1.2/1.3, HSTS). Port 80 exists solely for the ACME challenge and a
  301 to HTTPS. Requests for any other hostname get 421.
- **No client authentication.** Anyone can query `/v2/…`, exactly as with
  api.adsb.lol. This is the deliberate, recorded divergence from AC-S-01 and
  from AI-06's "mTLS" qualifier.
- **Only `/v2/` and `/health` are proxied.** `/metrics` is loopback-only
  (403 externally), `/livez` and every other app path are 404 externally.
  The adapter relay/metrics, readsb re-api and the serving-api listener all
  stay bound to 127.0.0.1; the nginx front is the only path in.
- **Two independent per-client rate limits:** nginx `limit_req` (5 r/s,
  burst 20) + `limit_conn` (20) keyed on the client address, in front of the
  serving-api's own token bucket (2 r/s, burst 10 — the contract limit that
  returns 429 + `Retry-After`). The app keys on the address nginx appends to
  `X-Forwarded-For` because `127.0.0.1` is listed in
  `rate_limit.trusted_proxies` (now the shipped default in
  `config/serving.yaml`); client-supplied entries left of it are never used.
- **No access log for `/v2/`:** the URI carries the querying node's own
  position, which is location data under the §21.5 governance policy.
- **Host firewall default-deny**, inbound 22/80/443 only (ufw, applied by
  the enable script). The external port-scan check (A3) is the proof that
  nothing else is reachable; it re-runs after every network change.
- **Cloudflare proxy in front (decided 2026-09-06, owner expects DDoS).**
  The record is orange-cloud: Cloudflare's edge terminates the public TLS
  session, absorbs volumetric attacks and hides the origin address; it
  connects to the origin over TLS (zone SSL mode Full (strict), origin
  presents the Let's Encrypt certificate). Two origin-side consequences,
  both applied by `enable-public-front.sh --proxied`: (a) nginx trusts
  `CF-Connecting-IP` only from Cloudflare's published ranges (`realip`
  module, generated `conf.d/retina-cloudflare-realip.conf`), so both rate
  limiters still key on the real node address and the app receives exactly
  one `X-Forwarded-For` entry, `$remote_addr`; (b) the host firewall admits
  80/443 **only from those ranges**, so learning the origin IP does not let
  an attacker bypass the shield. Ranges are refreshed from cloudflare.com on
  every run (vendored fallback `deploy/nginx/cloudflare-ips.txt`); the script
  is re-run periodically to pick up changes. Dashboard-side requirements:
  Bot Fight Mode / challenges off for this hostname (nodes are non-browser
  clients), a cache-bypass rule, and optionally an edge rate-limiting rule
  so floods are dropped before they reach the origin.

### Options 1 and 2 (still shipped, not enabled)

1. **`mtls.conf` — nginx mTLS.** Listen 443, TLS with client certificate
   required. Aligns with the eventual §11 contract (gRPC + mTLS on 443) and
   the §21.1 per-node identity model. Needs a CA + enrollment (ADR #4).
2. **`private-net.conf` — private network + firewall.** Bind a private
   interface (VPC/WireGuard), source-IP allowlist, default-deny public.

Switching from option 3 to option 1 later is a front-only change: the
serving-api and its rate limiter are identical behind all three fronts.

## AC-S-01 analysis of each option

| Option | Public scan result | AC-S-01 verdict | Costs / residual risk |
|---|---|---|---|
| Default closed (as installed) | nothing | satisfied trivially | no consumer can connect |
| **3. public TLS (chosen)** | one TLS endpoint on 443 (+80 redirect) serving `/v2` and `/health` to anyone | **diverged** — the endpoint is a TLS endpoint but not an *authenticated* one | anyone on the Internet can read the aggregate picture (which is ADSBHub's public data, redistributed under its terms — docs/licenses.md); abuse is bounded by two rate limits and the loopback-only everything-else; no per-node identity or revocation until Phase B mTLS |
| 1. nginx mTLS | one TLS endpoint refusing clients without certs | satisfied | CA + issuance/renewal procedure, cert distribution to nodes |
| 2. private net + firewall | nothing on public interfaces | satisfied only if the network is actually private and the firewall verified | no per-node identity; misconfiguration silently voids AC-S-01 |

## Consequences

- AC-S-01 and AI-06 move from "pending Q5" to **diverged** in the
  conformance ledger, legitimized by this ADR and spec amendment §7. The
  spec owner folds the divergence into the spec in the amendment's
  integration change.
- Node cutover is unblocked from the boundary side; it remains gated on Q7
  latency characterization and the A3 soak.
- The external scan (A3) must show 22 open, 80/443 open **only from
  Cloudflare's ranges** (filtered from anywhere else), `/metrics` 403 and
  `/livez` 404 through the front — `enable-public-front.sh` asserts the
  HTTP part (including that responses carry Cloudflare's `cf-ray`) on every
  run; the from-elsewhere port filtering can only be proven by the external
  scan.
- Phase B's native contract (gRPC + mTLS on 443) still needs the CA/PKI ADR
  (#4); nothing here pre-empts it. The public v2 front sunsets with the
  interim contract (amendment §8).
- tar1090 operator visualization, if ever added, must NOT be exposed through
  this front (spec §6.1 S3: authenticated operator network only).
- ADSBHub's terms permit processing, publishing and commercial use of the
  aggregate feed (spec §4.1; rechecked at launch and annually), so public
  redistribution of the served picture is within the entitlement; the
  contribution-station obligation (AI-04) is unchanged.
