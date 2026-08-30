# Runbook: ADSBHub disconnected / no data

Alerts: **ADSBHubDown** (critical — disconnected or no valid record 30 s, spec §14.4 row 1),
**AdapterHealthEndpointDown** (critical — adapter metrics absent).

## Symptoms

- ADSBHubDown firing; freshness dashboard "Last valid record age" red (>30 s).
- Pipeline dashboard: connection state DOWN, bytes rate 0, reconnects climbing.
- serving-api `/health` returns 503 within ~60 s (aircraft age out of readsb).
- AdapterHealthEndpointDown instead: adapter process/metrics listener dead — start at step 1.

## Dashboards

- `retina-adsb-pipeline`: connection state, bytes rate, reconnects by reason, relay state.
- `retina-adsb-freshness`: last valid record age.

## Safe diagnostics (read-only)

```
systemctl status adsbhub-adapter
curl -sS 127.0.0.1:9101/healthz ; echo
curl -sS 127.0.0.1:9101/metrics | grep -E 'provider_(connected|reconnects|last_record)'
journalctl -u adsbhub-adapter -n 100 --no-pager
ss -tnp 'dport = :5002'                      # upstream session present?
ss -tlnp | grep 31003                        # relay listening? (closed while upstream down — by design)
curl -4 -sS https://icanhazip.com            # current egress IP
```

## Decision tree

1. **Adapter process down?** (`systemctl status` failed/inactive, or AdapterHealthEndpointDown)
   → `journalctl -u adsbhub-adapter -n 200 --no-pager` for the crash reason;
   `systemctl restart adsbhub-adapter`. If it crash-loops on config:
   `/opt/retina-adsb/.venv/bin/adsbhub-adapter --check-config --config /etc/retina-adsb/adapter.yaml`.
2. **Process up, `connected 0`** — read the reconnect reason:
   `journalctl -u adsbhub-adapter -n 50 --no-pager | grep -iE 'connect|refused|timeout|dns'`
   - **DNS failure** (`data.adsbhub.org` unresolvable) → host resolver problem: `resolvectl query data.adsbhub.org`.
   - **Connection refused or timeout** → **whitelist/egress-IP failure mode**. ADSBHub
     opens port 5002 ONLY for IP addresses configured on the account profile page
     (adsbhub.org "How to get data"), so a wrong egress IP is *indistinguishable from
     provider-down* from this side. Check FIRST:
     1. Compare `curl -4 -sS https://icanhazip.com` output with the IP whitelisted on
        the ADSBHub profile page.
     2. Mismatch happens when the droplet was rebuilt/moved (plan.md A0). Fix: update
        the profile whitelist to the current egress IP; propagation is provider-side,
        allow a few minutes. Prevention (plan.md A0): attach a **reserved/floating IP**
        to the droplet so the egress IP never changes across rebuilds.
     3. IPs match and still refused/timeout → provider side: is the contribution
        station (47.46.206.146) actively feeding? Is ADSBHub itself up? → Escalation.
   - **Connected then idle-killed** (`idle_timeout`, 30 s no bytes) repeatedly →
     upstream is accepting but silent: provider-side data problem → Escalation.
3. **`connected 1` but ADSBHubDown still firing** (no *valid* records) → records are
   arriving but all rejected: check
   `curl -sS 127.0.0.1:9101/metrics | grep records_total` — if non-`ok` results dominate,
   switch to `runbooks/parse-error-surge.md`.

## Remediation

- Whitelist fix as above; no service action needed — backoff reconnects automatically
  (exponential, cap 60 s, reset after 60 s stable; spec §24). To skip the current
  backoff wait: `systemctl restart adsbhub-adapter`.
- Do NOT start a second connection/instance to test: ADSBHub ingestion is
  active/standby, one connection only (spec §20.3).

## Verification

```
curl -sS 127.0.0.1:9101/healthz                    # 200, state healthy
curl -sS 127.0.0.1:9101/metrics | grep -E 'provider_connected|records_total{.*result="ok"'
ss -tlnp | grep 31003                              # relay listening again
curl -sS 127.0.0.1:8080/health                     # 200 within ~60 s of first valid line
```

Pipeline dashboard: connection CONNECTED, bytes > 0, records `ok` flowing; ADSBHubDown resolves.

## Escalation

- ADSBHub side (port never opens with correct whitelist; station not feeding; provider
  outage): contact ADSBHub via the account email / adsbhub.org contact form. Reference
  the account owning the whitelist and station 47.46.206.146.
- Entitlement questions (terms, station eligibility): project owner — see plan.md A0.

## Post-incident

- **Confirm no stale data was served.** The relay closes on upstream failure
  (spec §6.1 S1), so readsb aircraft must have aged out (~60 s) rather than being
  served with frozen positions. Check the outage window:
  - pipeline dashboard: `readsb_aircraft_with_position` fell to 0 within ~60 s of disconnect;
  - freshness dashboard: `/health` 503 rate nonzero during the window;
  - serving-api logs: `journalctl -u retina-serving-api --since '<outage start>' --until '<recovery>' --no-pager`.
  If positioned aircraft persisted through the outage, treat as a defect (relay did not
  close) and file it before closing the incident.
- Record: cause, detection time vs alert time, recovery time, whitelist state.
