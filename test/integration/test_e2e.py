"""Live end-to-end integration: synthetic ADSBHub -> adsbhub-adapter ->
pinned readsb -> serving-api, all real processes on this host (DESIGN.md §7).

The synthetic upstream replays the golden SBS fixtures with timestamps
regenerated at send time (optional ``lag`` emits them N seconds in the past),
which is what makes the honest-``seen_pos`` and timestamp-guard assertions
meaningful against the real readsb clamp behavior (ADR-0004).

Every wait is a poll with a deadline — no bare sleeps for readiness.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import httpx
import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
READSB = REPO / "vendor" / "readsb" / "readsb"
FIXTURES = REPO / "test" / "fixtures" / "sbs"
PIN_DOC = REPO / "docs" / "readsb-pin.md"
# The RECORDED envelope fixture (see test/fixtures/api/README.md): the actual
# body served by the live stack, rewritten by test_envelope_shape each run —
# same idempotent-regeneration pattern as the readsb-pin.md measurement rows.
RECORDED_ENVELOPE = REPO / "test" / "fixtures" / "api" / "jv2_envelope_recorded.json"

TICK_SECONDS = 0.1  # ~10 lines/s golden replay

# Golden aircraft used as assertion anchors (test/fixtures/sbs/golden_msg_*.txt).
AIRBORNE_HEX = "3c6589"
AIRBORNE_LAT, AIRBORNE_LON = 37.82015, -121.90403
GROUND_HEX = "ab34f0"
GROUND_LAT, GROUND_LON = 37.61890, -122.37505


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sbs_timestamp(unix_seconds: float) -> tuple[str, str]:
    dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    return dt.strftime("%Y/%m/%d"), dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _retimestamp(template: str, generated: float) -> bytes:
    fields = template.split(",")
    date, tod = _sbs_timestamp(generated)
    fields[6], fields[7] = date, tod
    fields[8], fields[9] = date, tod
    return (",".join(fields) + "\r\n").encode("ascii")


def make_msg3(hexid: str, lat: float, lon: float, generated: float) -> bytes:
    date, tod = _sbs_timestamp(generated)
    return (
        f"MSG,3,1,900,{hexid},90001,{date},{tod},{date},{tod},"
        f",36000,,,{lat:.5f},{lon:.5f},,,0,0,0,0\r\n"
    ).encode("ascii")


class SyntheticAdsbhub:
    """Threaded stand-in for data.adsbhub.org:5002.

    Replays the golden fixture lines round-robin at ~10 lines/s with
    generated/logged timestamps rewritten to ``now - lag`` at send time.
    Tests can additionally register per-tick generators (extra aircraft with
    their own lag) and inject raw byte lines verbatim (malformed corpus).
    Restartable on the same port for the upstream-failure/recovery test.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.lag = 0.0
        self._templates = [
            path.read_text().strip()
            for path in sorted(FIXTURES.glob("golden_msg_*.txt"))
        ]
        assert len(self._templates) == 8
        self._generators: dict[str, Callable[[float], list[bytes]]] = {}
        self._inject_queue: list[bytes] = []
        self._lock = threading.Lock()
        self._clients: list[socket.socket] = []
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._stopped = threading.Event()

    def start(self) -> None:
        self._stopped.clear()
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(4)
        self._listener = listener
        self._threads = [
            threading.Thread(target=self._accept_loop, daemon=True),
            threading.Thread(target=self._feed_loop, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stopped.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = []

    def add_generator(self, name: str, fn: Callable[[float], list[bytes]]) -> None:
        with self._lock:
            self._generators[name] = fn

    def remove_generator(self, name: str) -> None:
        with self._lock:
            self._generators.pop(name, None)

    def inject(self, lines: list[bytes]) -> None:
        """Queue raw lines, sent verbatim one per tick (interleaved with golden)."""
        with self._lock:
            self._inject_queue.extend(lines)

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            conn.settimeout(2.0)
            with self._lock:
                self._clients.append(conn)

    def _feed_loop(self) -> None:
        index = 0
        while not self._stopped.is_set():
            base = time.time()
            payload = _retimestamp(self._templates[index % len(self._templates)], base - self.lag)
            index += 1
            with self._lock:
                generators = list(self._generators.values())
                injected = self._inject_queue.pop(0) if self._inject_queue else None
            for fn in generators:
                payload += b"".join(fn(base))
            if injected is not None:
                payload += injected if injected.endswith(b"\n") else injected + b"\r\n"
            with self._lock:
                clients = list(self._clients)
            for client in clients:
                try:
                    client.sendall(payload)
                except OSError:
                    with self._lock:
                        if client in self._clients:
                            self._clients.remove(client)
                    try:
                        client.close()
                    except OSError:
                        pass
            time.sleep(TICK_SECONDS)


def poll(check: Callable[[], object], deadline: float, what: str, interval: float = 0.4):
    """Poll ``check`` until it returns a truthy value; fail after ``deadline`` s."""
    end = time.monotonic() + deadline
    last_error: Exception | None = None
    while time.monotonic() < end:
        try:
            value = check()
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            last_error = exc
            value = None
        if value:
            return value
        time.sleep(interval)
    pytest.fail(f"timed out after {deadline}s waiting for: {what} (last error: {last_error!r})")


def metric_value(text: str, name: str, labels: dict[str, str] | None = None) -> float | None:
    for line in text.splitlines():
        if not line.startswith(name):
            continue
        rest = line[len(name):]
        if rest.startswith("{"):
            label_str, _, value_str = rest[1:].partition("} ")
        elif rest.startswith(" "):
            label_str, value_str = "", rest[1:]
        else:
            continue
        if labels and not all(f'{k}="{v}"' in label_str for k, v in labels.items()):
            continue
        return float(value_str)
    return None


class Stack(SimpleNamespace):
    def get(self, url: str) -> httpx.Response:
        return self.http.get(url, timeout=5.0)

    def adapter_metric(self, name: str, labels: dict[str, str] | None = None) -> float:
        text = self.get(f"{self.metrics_url}/metrics").text
        value = metric_value(text, name, labels)
        return 0.0 if value is None else value

    def records_total(self, result: str) -> float:
        return self.adapter_metric(
            "retina_adsb_provider_records_total",
            {"provider": "adsbhub", "result": result},
        )

    def v2(self, lat: float, lon: float, dist: float) -> httpx.Response:
        return self.get(f"{self.serving_url}/v2/lat/{lat}/lon/{lon}/dist/{dist}")

    def find_aircraft(self, lat: float, lon: float, dist: float, hexid: str) -> dict | None:
        response = self.v2(lat, lon, dist)
        if response.status_code != 200:
            return None  # 429 while a bucket refills is a retry, not a failure
        for aircraft in response.json().get("ac", []):
            if aircraft.get("hex") == hexid:
                return aircraft
        return None


def _spawn(cmd: list[str], stderr_path: Path) -> tuple[subprocess.Popen, object]:
    stderr_file = stderr_path.open("wb")
    proc = subprocess.Popen(cmd, stdout=stderr_file, stderr=subprocess.STDOUT)
    return proc, stderr_file


def _stderr_tail(path: Path, lines: int = 40) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return "<unreadable>"
    return "\n".join(content[-lines:])


@pytest.fixture(scope="module")
def stack(tmp_path_factory, request):
    if not READSB.exists():
        pytest.fail(f"pinned readsb binary missing: {READSB}")
    tmpdir = tmp_path_factory.mktemp("e2e")
    json_dir = tmpdir / "readsb-json"
    json_dir.mkdir()

    synth_port = _free_port()
    relay_port = _free_port()
    metrics_port = _free_port()
    api_port = _free_port()
    serving_port = _free_port()

    adapter_cfg = tmpdir / "adapter.yaml"
    adapter_cfg.write_text(
        f"""\
config_version: 1
log_level: INFO
adsbhub:
  host: 127.0.0.1
  port: {synth_port}
  connect_timeout: 5
  idle_timeout: 30
  future_tolerance: 1
  max_record_age: 60
  # short backoff cap keeps the recovery test bounded (design default is 60s)
  reconnect:
    initial: 0.5
    maximum: 5
    stable_reset: 60
relay:
  host: 127.0.0.1
  port: {relay_port}
  max_clients: 2
  heartbeat_interval: 25
metrics:
  host: 127.0.0.1
  port: {metrics_port}
"""
    )
    serving_cfg = tmpdir / "serving.yaml"
    serving_cfg.write_text(
        f"""\
config_version: 1
bind:
  host: 127.0.0.1
  port: {serving_port}
reapi_endpoint: http://127.0.0.1:{api_port}/re-api/
timeouts:
  total_seconds: 5
  connect_seconds: 1
adapter_health_url: http://127.0.0.1:{metrics_port}/healthz
freshness:
  write_json_every_seconds: 1
  staleness_factor: 5
# rate raised above the shipped 2/s so the suite's own polling never trips it;
# test_rate_limit hammers well above this
rate_limit:
  rate_per_second: 5
  burst: 20
  lru_size: 100
"""
    )

    synth = SyntheticAdsbhub(synth_port)
    synth.start()

    procs: list[tuple[str, subprocess.Popen, Path]] = []
    stack_ns = Stack(
        synth=synth,
        relay_port=relay_port,
        metrics_url=f"http://127.0.0.1:{metrics_port}",
        serving_url=f"http://127.0.0.1:{serving_port}",
        reapi_url=f"http://127.0.0.1:{api_port}/re-api/",
        http=httpx.Client(),
        procs=procs,
    )

    def _launch(name: str, cmd: list[str]) -> subprocess.Popen:
        stderr_path = tmpdir / f"{name}.stderr"
        proc, _ = _spawn(cmd, stderr_path)
        procs.append((name, proc, stderr_path))
        return proc

    try:
        _launch("adapter", [sys.executable, "-m", "adsbhub_adapter", "--config", str(adapter_cfg)])
        poll(
            lambda: stack_ns.get(f"{stack_ns.metrics_url}/healthz").status_code == 200,
            30, "adapter /healthz 200 (upstream connected + valid records)",
        )

        stack_ns.readsb_proc = _launch("readsb", [
            str(READSB), "--net", "--quiet", "--net-bind-address=127.0.0.1",
            f"--net-connector=127.0.0.1,{relay_port},sbs_in", "--net-connector-delay=1",
            "--max-range=99999", f"--write-json={json_dir}", "--write-json-every=1",
            "--json-reliable=1", f"--net-api-port={api_port}",
        ])
        poll(
            lambda: stack_ns.get(f"{stack_ns.reapi_url}?status").status_code == 200,
            30, "readsb re-api ?status 200",
        )
        poll(
            lambda: json.loads(
                stack_ns.get(f"{stack_ns.reapi_url}?circle={AIRBORNE_LAT},{AIRBORNE_LON},50&jv2").content
            )["total"] >= 1,
            30, "readsb serving at least one golden aircraft",
        )

        _launch("serving", [sys.executable, "-m", "serving_api", "--config", str(serving_cfg)])
        poll(
            lambda: stack_ns.get(f"{stack_ns.serving_url}/health").status_code == 200,
            30, "serving-api /health 200",
        )
    except BaseException:
        for name, proc, stderr_path in procs:
            proc.terminate()
            print(f"\n===== {name} stderr (setup failure) =====\n{_stderr_tail(stderr_path)}")
        synth.stop()
        raise

    yield stack_ns

    for name, proc, stderr_path in reversed(procs):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    synth.stop()
    stack_ns.http.close()
    if getattr(request.session, "_integration_failed", False):
        for name, proc, stderr_path in procs:
            print(f"\n===== {name} stderr =====\n{_stderr_tail(stderr_path)}")


def test_envelope_shape(stack):
    aircraft = poll(
        lambda: stack.find_aircraft(AIRBORNE_LAT, AIRBORNE_LON, 50, AIRBORNE_HEX),
        30, f"{AIRBORNE_HEX} in the served /v2 envelope",
    )
    response = stack.v2(AIRBORNE_LAT, AIRBORNE_LON, 50)
    assert response.status_code == 200
    # record the live served body verbatim (audit-M3: the recorded fixture is
    # the provenance-bearing envelope evidence; the hand-synthesized
    # jv2_envelope.json only feeds unit-test upstream mocks)
    RECORDED_ENVELOPE.write_bytes(response.content)
    envelope = response.json()
    assert isinstance(envelope["ac"], list)
    assert envelope["msg"] == "No error"
    assert isinstance(envelope["now"], int)
    assert abs(envelope["now"] - time.time() * 1000.0) < 60_000
    assert envelope["total"] >= 1

    assert aircraft["hex"] == AIRBORNE_HEX
    assert aircraft["type"] == "other"
    assert isinstance(aircraft["lat"], float)
    assert isinstance(aircraft["lon"], float)
    assert isinstance(aircraft["seen_pos"], float)
    # SBS carries no geometric altitude (plan §0 req 3)
    assert "alt_geom" not in aircraft


def test_ground_aircraft(stack):
    aircraft = poll(
        lambda: stack.find_aircraft(GROUND_LAT, GROUND_LON, 50, GROUND_HEX),
        30, f"ground mover {GROUND_HEX} in the served envelope",
    )
    # SBS field-22 ground flag -1 must render as the string "ground"
    # (plan §0 req 4; requires NOT passing --json-separate-ground)
    assert aircraft["alt_baro"] == "ground"


def test_honest_seen_pos(stack):
    """Plan §0 req 1: injected upstream lag must appear in served seen_pos."""
    lag = 3.0
    state = {"lat": 44.0}

    def lagged(base: float) -> list[bytes]:
        # moves ~11 m per tick so readsb's duplicate-position suppression
        # never freezes seen_pos at a stale acceptance
        state["lat"] += 0.0001
        return [make_msg3("0DBE03", state["lat"], -100.0, base - lag)]

    stack.synth.add_generator("lagged", lagged)
    try:
        aircraft = poll(
            lambda: stack.find_aircraft(44.0, -100.0, 100, "0dbe03"),
            30, "lagged aircraft 0dbe03 served",
        )
        observed = aircraft["seen_pos"]
        assert observed >= lag, (
            f"served seen_pos {observed} hides the {lag}s upstream lag"
        )
        # sanity: lag + <=1s api cache staleness + scheduling margin
        assert observed < lag + 5.0
        print(f"\nMEASURED honest seen_pos with {lag}s injected lag: {observed}")
    finally:
        stack.synth.remove_generator("lagged")


def test_quantization_measurement(stack):
    """Spec §10.4: measure source timestamp quantization through the pinned
    build and record it in docs/readsb-pin.md.

    Two aircraft are fed in the same tick with generated timestamps exactly
    7 ms apart (both derived from one base clock, integral-ms offsets). Both
    seen_pos values in one api snapshot are computed against the same cache
    build time, so the served difference must be exactly 0.007 iff readsb
    preserves ms precision end-to-end.
    """
    state = {"lat1": 10.0, "lat2": 10.02}

    def pair(base: float) -> list[bytes]:
        state["lat1"] += 0.0001
        state["lat2"] += 0.0001
        return [
            make_msg3("0DDE01", state["lat1"], 10.0, base - 0.050),
            make_msg3("0DDE02", state["lat2"], 10.0, base - 0.057),
        ]

    samples: list[tuple[float, float, float, float]] = []
    seen_now: set[int] = set()

    def take_sample() -> bool:
        response = stack.v2(10.05, 10.0, 100)
        if response.status_code != 200:
            return False
        envelope = response.json()
        by_hex = {a["hex"]: a for a in envelope["ac"]}
        if "0dde01" not in by_hex or "0dde02" not in by_hex:
            return False
        if envelope["now"] in seen_now:
            return False  # same api cache build — wait for a fresh snapshot
        seen_now.add(envelope["now"])
        a1, a2 = by_hex["0dde01"], by_hex["0dde02"]
        samples.append((a1["seen_pos"], a2["seen_pos"], a1["seen"], a2["seen"]))
        return len(samples) >= 6

    stack.synth.add_generator("quantization", pair)
    try:
        poll(take_sample, 45, "6 distinct-cache seen_pos samples for the 7 ms pair")
    finally:
        stack.synth.remove_generator("quantization")

    deltas_ms = []
    for sp1, sp2, seen1, seen2 in samples:
        # seen_pos renders at 3 decimals: integral in ms
        for value in (sp1, sp2):
            assert abs(value * 1000 - round(value * 1000)) < 1e-6, f"seen_pos {value} not ms-integral"
        # seen renders at 1 decimal: 100 ms resolution
        for value in (seen1, seen2):
            assert abs(value * 10 - round(value * 10)) < 1e-6, f"seen {value} not 0.1s-integral"
        deltas_ms.append(round((sp2 - sp1) * 1000))

    exact = [d for d in deltas_ms if d == 7]
    assert len(exact) >= 3, (
        f"7 ms generated-timestamp offset not preserved: deltas {deltas_ms}"
    )
    # the load-bearing claim: served seen_pos distinguishes values <0.01s apart
    assert any(d % 10 == 7 for d in deltas_ms), f"no sub-centisecond delta seen: {deltas_ms}"
    print(f"\nMEASURED quantization: seen_pos deltas {deltas_ms} ms for a 7 ms "
          f"generated-ts offset -> source_timestamp_quantization_ms = 1")

    _record_measurement(deltas_ms)


def _record_measurement(deltas_ms: list[int]) -> None:
    date = time.strftime("%Y-%m-%d")
    by = f"`test/integration/test_e2e.py::test_quantization_measurement`, {date}"
    evidence = (
        f"injected 7 ms generated-ts offset served as {sorted(set(deltas_ms))} ms "
        f"`seen_pos` deltas within single api snapshots"
    )
    rows = {
        r"^\| `source_timestamp_quantization_ms` \(SBS field-8 path, pinned build\) \|.*$":
            f"| `source_timestamp_quantization_ms` (SBS field-8 path, pinned build) | **1 ms** ({evidence}) | {by} |",
        r"^\| `seen_pos` output resolution observed \|.*$":
            f"| `seen_pos` output resolution observed | 1 ms (all sampled values integral in ms) | {by} |",
        r"^\| `seen` output resolution observed \|.*$":
            f"| `seen` output resolution observed | 100 ms (all sampled values integral in 0.1 s) | {by} |",
    }
    text = PIN_DOC.read_text()
    missing = []
    for pattern, replacement in rows.items():
        new_text, count = re.subn(pattern, replacement.replace("\\", r"\\"), text, count=1, flags=re.M)
        if count:
            text = new_text
        else:
            missing.append(replacement)
    if missing:
        text += (
            "\n## Measured quantization (appended by the integration suite)\n\n"
            + "| Quantity | Measured value | Measured by / date |\n|---|---|---|\n"
            + "\n".join(missing) + "\n"
        )
    PIN_DOC.write_text(text)


def test_stale_guard(stack):
    """ADR-0004: a 120 s-old record is rejected at the adapter and never
    reaches readsb (whose own clamp would have made it look fresh)."""
    baseline = stack.records_total("stale")
    stack.synth.inject([make_msg3("0EEA01", 20.0, 20.0, time.time() - 120.0)])
    poll(
        lambda: stack.records_total("stale") >= baseline + 1,
        15, "records_total{result=stale} increment",
    )
    # two api cache rebuilds' worth of chances for the aircraft to (wrongly) appear
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        assert stack.find_aircraft(20.0, 20.0, 50, "0eea01") is None, (
            "stale record leaked through the adapter into the served state"
        )
        time.sleep(0.5)


def test_malformed_resilience(stack):
    corpus = (FIXTURES / "malformed_corpus.txt").read_bytes().splitlines()
    assert len(corpus) == 20
    base_structure = stack.records_total("invalid_structure")
    base_range = stack.records_total("invalid_range")
    base_ok = stack.records_total("ok")

    stack.synth.inject(corpus)

    # corpus under a live clock: 8 invalid_structure, 8 invalid_range (the
    # track-360 line passes range checks since sc-1 — readsb accepts any
    # finite heading — and falls to the timestamp guard instead), the rest
    # stale/future, 1 unparseable_ts (relayed by design — readsb stamps arrival)
    poll(
        lambda: stack.records_total("invalid_structure") >= base_structure + 8,
        20, "8 invalid_structure increments from the corpus",
    )
    poll(
        lambda: stack.records_total("invalid_range") >= base_range + 8,
        20, "8 invalid_range increments from the corpus",
    )

    # the stream survived: valid records keep flowing...
    poll(
        lambda: stack.records_total("ok") > base_ok + 10,
        15, "ok records still flowing after the malformed corpus",
    )
    # ...readsb is still connected to the relay...
    assert stack.adapter_metric("retina_adsb_relay_connected_clients") >= 1
    # ...and the golden aircraft is still served fresh
    aircraft = poll(
        lambda: stack.find_aircraft(AIRBORNE_LAT, AIRBORNE_LON, 50, AIRBORNE_HEX),
        15, "golden aircraft still served after the malformed corpus",
    )
    assert aircraft["seen_pos"] < 30.0


def test_rate_limit(stack):
    url = f"{stack.serving_url}/v2/lat/{AIRBORNE_LAT}/lon/{AIRBORNE_LON}/dist/50"
    statuses = []
    limited = None
    for _ in range(60):
        response = stack.http.get(url, timeout=5.0)
        statuses.append(response.status_code)
        if response.status_code == 429 and limited is None:
            limited = response
    assert 200 in statuses, "burst should start within the bucket allowance"
    assert limited is not None, f"no 429 across 60 rapid requests: {set(statuses)}"
    assert limited.json() == {"msg": "rate limit exceeded"}
    retry_after = limited.headers.get("Retry-After")
    assert retry_after is not None and int(retry_after) >= 1
    # let the module's bucket refill so later /v2 polls are unaffected
    time.sleep(2.0)


def test_relay_closes_on_upstream_failure(stack):
    poll(
        lambda: stack.get(f"{stack.metrics_url}/healthz").status_code == 200,
        15, "healthy adapter before the upstream kill",
    )
    stack.synth.stop()

    # §14.4 row 1 / §6.1 S1: unhealthy well within idle_timeout(30s) + margin
    poll(
        lambda: stack.get(f"{stack.metrics_url}/healthz").status_code == 503,
        40, "adapter /healthz 503 after upstream kill",
    )

    def relay_refused() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", stack.relay_port), timeout=1):
                return False
        except (ConnectionRefusedError, TimeoutError, OSError):
            return True

    poll(relay_refused, 15, "relay listener closed after upstream failure")

    # no fresh data is manufactured: the served aircraft only ages
    first = poll(
        lambda: stack.find_aircraft(AIRBORNE_LAT, AIRBORNE_LON, 50, AIRBORNE_HEX),
        10, "golden aircraft still present (aging) after upstream kill",
    )
    time.sleep(3.0)
    second = stack.find_aircraft(AIRBORNE_LAT, AIRBORNE_LON, 50, AIRBORNE_HEX)
    assert second is not None, "aircraft evicted sooner than readsb's ~60s window"
    assert second["seen_pos"] > first["seen_pos"] + 1.5, (
        f"seen_pos did not keep growing: {first['seen_pos']} -> {second['seen_pos']}"
    )

    # recovery: upstream returns, adapter reconnects, relay reopens, data flows
    stack.synth.start()
    poll(
        lambda: stack.get(f"{stack.metrics_url}/healthz").status_code == 200,
        45, "adapter healthy again after upstream restart",
    )

    def fresh_again() -> bool:
        aircraft = stack.find_aircraft(AIRBORNE_LAT, AIRBORNE_LON, 50, AIRBORNE_HEX)
        return aircraft is not None and aircraft["seen_pos"] < 6.0

    poll(fresh_again, 45, "fresh data served after recovery")


def test_health_chain(stack):
    """Last test: kills readsb. /health 200 while all up -> 503 when the hub dies."""
    def health_detail(expected_status: int) -> dict | None:
        response = stack.get(f"{stack.serving_url}/health")
        return response.json() if response.status_code == expected_status else None

    detail = poll(
        lambda: health_detail(200),
        15, "serving /health 200 with the full stack up",
        interval=0.5,
    )
    assert detail["status"] == "ok"
    assert detail["adapter"]["healthy"] is True

    stack.readsb_proc.terminate()
    stack.readsb_proc.wait(timeout=10)

    unhealthy = poll(
        lambda: health_detail(503),
        30, "serving /health 503 after readsb stops",
    )
    assert unhealthy["status"] == "unhealthy"
