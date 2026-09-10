# ADR-0008: Account-less Beast contribution port on TCP 7444

## Status

**Accepted** — decided by the project owner on 2026-09-06: "Add a mTLS feed
port at 7444 like adsb.lol's ingest, no certificate required. The spec
should be taken as a loose suggestion." Live on the production host the same
day (`deploy/enable-feed-port.sh --tls-host adsb.retina.fm`).

## Context

Until this ADR the Phase A host was serve-only: the sole ingest path was the
outbound ADSBHub SBS connection, readsb opened no input listener beyond the
loopback relay connector, and the firewall admitted nothing but SSH and the
Cloudflare-fronted 80/443. Spec §11.2 / §19 / AI-06 / AI-07 describe the
eventual contribution transport as TCP 7444, **mutual TLS**, one client
certificate per node mapped to a receiver UUID, BeastReducePlus payload, a
`retina-feed-gateway` enforcing all of it — Phase B work that needs the
CA/enrollment ADR (#4) first.

adsb.lol's ingest, which the rest of this stack mirrors (ADR-0004/0005), is
the opposite model: anyone points a Beast connector at their load balancer,
identity is at most a self-chosen UUID, there is no TLS and no account; the
hub's `readsb --net-ingest` mode handles abuse per source address.

The owner chose the adsb.lol model now, at the spec's port number, and
declared the spec guidance rather than binding (recorded as a status note
in both spec documents, version 1.4).

## Decision

One public port, **TCP 7444**, no account and no client certificate,
implemented as an nginx `stream{}` front (`deploy/nginx/feed.conf`) in front
of a loopback readsb Beast listener:

- **Two wire forms on the same port, auto-detected** (`ssl_preread`):
  plain TCP Beast — exactly what `readsb/dump1090 --net-connector
  <host>,7444,beast_out` sends — is forwarded unchanged; a TLS ClientHello is
  terminated at a loopback server presenting the Let's Encrypt certificate
  (server auth only, `ssl_verify_client` off) and forwarded the same way.
  TLS is thus available to feeders that want privacy in transit (stunnel/
  socat in front of the same connector) without being required. "mTLS with
  no certificate required" is read as: the spec's TLS feed port, minus the
  client certificate.
- **The feeder's real address reaches readsb** in a PROXY protocol v1
  header on every forwarded connection; readsb parses it under
  `--net-ingest` (`net_io.c readProxy`), so receiver ids, rate limits,
  garbage disconnects, `clients.json` and the `disc:` journal lines all key
  on the feeder rather than on 127.0.0.1.
- **readsb-hub gains `--net-bi-port=31004 --net-ingest`** (spec §19's own
  31004, loopback via `--net-bind-address`), generated from a new
  `beast_feed_port` key. The generator emits the two flags **only as a
  pair** and refuses either by hand: a Beast listener without
  `--net-ingest` would let any feeder set the hub's receiver location
  (radarcape position frame → arms the range filter) and would skip readsb's
  per-feeder message/position limits, CPR fast-repetition garbage detection
  and PROXY parsing.
- **`--net-ingest`'s limits are raised** (`--devel=ingestLimitRate,60000
  --devel=ingestLimitPositionRate,20000`, keys
  `ingest_limit_*_per_second`). readsb applies them to *every* input client
  including the ADSBHub SBS relay; its defaults (5,420 msg/s, 300
  positions/s — one physical receiver) flagged the ~2,500 msg/s / ~800
  positions/s aggregate as garbage within seconds of the first restart and
  **every ADSBHub position vanished from the served picture** (observed live,
  fixed within minutes). The raised bounds are unreachable by the aggregate
  and by a single feeder alike (nginx caps a feed connection at 1 MB/s ≈ 45k
  Beast messages/s).
- **`--json-reliable=2`** (adsb.lol's multi-feeder value, key
  `json_reliable`): an aircraft's position is served only after two
  consistent position messages, so one bogus CPR pair from an anonymous
  feeder plants nothing. Cost on the ADSBHub side: a newly seen aircraft's
  first position is held ~1–2 s until its second; tracked aircraft are
  unaffected (live: 7,065 aircraft, 6,794 with served positions, the gap
  being fresh arrivals). DESIGN.md §3's earlier claim that SBS positions are
  "immediately visible either way" was wrong and is corrected.
- **Abuse bounds without identity** (the spec §18 `feed_gateway` numbers,
  minus the per-node keying): `limit_conn` 4 connections per source address
  (spec: 2 per identity; a NAT'd site shares one address),
  `proxy_upload_rate` 1 MB/s per connection, `proxy_timeout` 90 s idle,
  `preread_timeout` 65 s (nginx *closes* a connection that sends nothing in
  that window — verified; a stock feeder with no traffic sends its first
  Beast heartbeat after 60 s, hence 65). readsb adds the per-feeder limits
  above, garbage disconnect after 512 undecodable bytes (`--net-ingest`
  threshold) and `--json-reliable=2`.
- **Firewall:** `ufw allow 7444/tcp` from anywhere; `enable-public-front.sh`
  re-applies it after its ufw reset (state in `/etc/retina-adsb/feed-port.env`).
- **No feeder access log in nginx** (§21.5); feeder addresses live in
  readsb's `clients.json` (10 s cadence, `/run/readsb`, mode 0755) and its
  journal `disc:` lines — operational data, retained per journald policy.

### What is deliberately NOT enforced

- No identity: no certificate, no account, no UUID requirement (the pinned
  build is `WITH_UUIDS=no`; adsb.lol's feeder UUIDs need their readsb-uuid
  build). A feeder is its source address, and only for the lifetime of a
  connection's rate accounting.
- AI-04's "contribution carries only local receiver data" is a **policy the
  feeder is told**, not something the hub can verify. Nothing on the hub
  re-feeds anything upstream, so the server half of AC-F-11 still holds; the
  node half is unenforceable until identity exists.
- Beast carries no upstream age: `seen_pos` for feeder-derived aircraft is
  receipt age on the hub (feeders are local receivers with sub-second
  latency, so the plan §0 req 1 honesty guarantee holds in practice but not
  by construction as it does for SBS).

## Consequences

- Spec rows move in the ledger: `AI-06` (7444 exposed, **unauthenticated**),
  `AI-07` (no gateway, no cert↔node mapping — diverged rather than deferred),
  `contract §11.2` (TLS optional, no mTLS, no ALPN, no UUID marker, Beast
  variants accepted, not only BeastReducePlus), `AC-S-01` (public scan now
  shows 22 and 7444 from anywhere), `AI-04`/`AC-F-11` (node half by policy).
  Divergence index entry #7; spec amendment §7a.
- **The origin address is discoverable again.** Cloudflare's proxy carries
  only HTTP(S); feeders connect to the origin, so a DNS-only feed hostname
  (or the bare IP) points at it. 80/443 stay admitted from Cloudflare's
  ranges only, so the *serving* path keeps its shield, but 7444 itself is
  unshielded: a volumetric attack on 7444 hits the droplet directly (bounded
  per address by `limit_conn`, not in aggregate). Cloudflare Spectrum (paid)
  would shield it; not taken now.
- A feed TLS hostname of its own (recommended: `feed.<zone>`, DNS-only) needs
  HTTP-01 validation from Let's Encrypt's many addresses, so
  `enable-feed-port.sh` opens 80/tcp from anywhere when it has to issue that
  certificate (port 80 serves ACME + a 301 only). Today the front reuses the
  `adsb.retina.fm` certificate, so 80 stays Cloudflare-only; TLS feeders must
  then connect to the origin IP with SNI/verification set to that name.
- **Moving to the spec's mTLS is a front-only change** on the same port:
  `ssl_verify_client on` + the RETINA CA in the loopback TLS server, and the
  plain branch of the `ssl_preread` map removed. readsb, its flags and the
  serving tier are untouched. The cert↔UUID mapping and BeastReducePlus UUID
  enforcement still need the Phase B gateway.
- `install.sh` checks 31004 for collisions; `verify-units.sh` asserts the
  `--net-bi-port`/`--net-ingest` pairing on the frozen unit line;
  `test/unit/test_deploy_args.py` pins the generated vector.
- Operators: `runbooks/soak-checklist.md` §5 now expects `0.0.0.0:7444`
  (nginx) and `127.0.0.1:31004` (readsb) locally, and 22 + 7444 open in the
  external scan.

## Verification (2026-09-06, production host)

- Isolated readsb with the exact flag set: synthetic DF17 even/odd CPR pairs
  behind a hand-written `PROXY TCP4 203.0.113.5 …` header decoded to a served
  position; `clients.json` and the `disc:` line carried 203.0.113.5 with a
  receiver id hashed from it.
- Through the public front (heartbeat frames only — nothing that decodes):
  plain and TLS 1.3 connections both reached readsb with the real client
  address in the PROXY header; a 5th concurrent connection from one address
  was closed by nginx; 2 KiB of junk was closed by readsb's garbage
  detection; TLS against a wrong server name failed verification; a plain
  feeder silent for 6.5 s stayed connected.
- Served picture after the restart: 7,065 aircraft / 6,794 with positions
  versus 7,356 before (fresh-arrival hold of `--json-reliable=2`; steady
  state converges), `/v2` via Cloudflare 200.

## Alternatives considered

- **Spec §11.2 mTLS gateway now** — needs the CA/enrollment ADR and node
  tooling that do not exist; blocks the owner's ask.
- **Plain Beast only** — exactly adsb.lol; rejected only because TLS on the
  same port costs one loopback server block and gives feeders an opt-in.
- **Separate ports for plain and TLS** — the spec names one port; detection
  by first bytes keeps it one.
- **Cloudflare Spectrum for 7444** — paid; revisit if 7444 is attacked.
