"""Relay lifecycle tests (DESIGN.md §4, spec §6.1 S1): the relay listens only
while the upstream is healthy, closes clients on upstream failure, heartbeats
idle clients, caps client count, and drops slow clients on queue overflow."""

import asyncio
import socket
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pytest

from adsbhub_adapter.adapter import Adapter, RelayServer
from adsbhub_adapter.config import load_config_dict
from adsbhub_adapter.metrics import AdapterMetrics

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sbs"
GOLDEN = [(FIXTURES / f"golden_msg_{t}.txt").read_bytes().splitlines()[0] for t in range(1, 9)]


def fresh_line(index: int = 2) -> bytes:
    """A golden line re-timestamped to 'now' so the staleness guard accepts it."""
    fields = GOLDEN[index].decode("ascii").split(",")
    stamp = datetime.now(timezone.utc)
    fields[6] = fields[8] = stamp.strftime("%Y/%m/%d")
    fields[7] = fields[9] = stamp.strftime("%H:%M:%S.%f")[:-3]
    return ",".join(fields).encode("ascii")


def unpadded_line(index: int = 2) -> bytes:
    """A fresh golden line whose ICAO field lost its leading zero, exactly as
    ADSBHub emits addresses below 0x100000 (ADR-0007)."""
    fields = fresh_line(index).decode("ascii").split(",")
    fields[4] = "D0EEC"
    return ",".join(fields).encode("ascii")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeUpstream:
    """A fake ADSBHub: accepts connections and lets the test drive the bytes."""

    def __init__(self) -> None:
        self.connections: asyncio.Queue[asyncio.StreamWriter] = asyncio.Queue()
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self.connections.put(writer)

    async def accept(self, timeout: float = 5.0) -> asyncio.StreamWriter:
        return await asyncio.wait_for(self.connections.get(), timeout)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def adapter_config(upstream_port: int, relay_port: int) -> dict:
    return load_config_dict({
        "config_version": 1,
        "adsbhub": {
            "host": "127.0.0.1",
            "port": upstream_port,
            "connect_timeout": "1s",
            "idle_timeout": "5s",
            "reconnect": {"initial": "20ms", "maximum": "40ms"},
        },
        "relay": {"port": relay_port, "heartbeat_interval": "10s"},
        "metrics": {"port": 0},
    })


async def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


async def assert_refused(port: int) -> None:
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)


async def send(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(payload)
    await writer.drain()


class TestAdapterRelayLifecycle:
    async def test_full_lifecycle(self):
        upstream = FakeUpstream()
        await upstream.start()
        relay_port = free_port()
        adapter = Adapter(adapter_config(upstream.port, relay_port))
        run_task = asyncio.create_task(adapter.run())
        try:
            conn = await upstream.accept()

            # connected but no valid line yet -> relay must refuse (spec §6.1 S1)
            await wait_until(lambda: adapter.health.connected)
            await assert_refused(relay_port)

            # an invalid line alone must not open the relay
            await send(conn, b"SEL,,496,2286,4CA4E5,27215,not,a,valid,line,at,all,,,,,,,,,,\r\n")
            await asyncio.sleep(0.15)
            await assert_refused(relay_port)

            # first valid line -> relay starts listening
            first_valid = fresh_line() + b"\r\n"
            await send(conn, first_valid)
            await wait_until(lambda: adapter.relay.listening)
            reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)

            # validated lines are forwarded verbatim, invalid ones are not
            await send(conn, b"GARBAGE LINE THAT IS LONG ENOUGH BUT NOT SBS\r\n")
            wire_line = fresh_line(3) + b"\r\n"
            await send(conn, wire_line)
            received = await asyncio.wait_for(reader.readline(), 3.0)
            assert received == wire_line  # bytes preserved, terminator included

            # upstream failure -> relay clients closed AND listener stopped
            conn.close()
            eof = await asyncio.wait_for(reader.read(), 3.0)
            assert eof == b""
            await wait_until(lambda: not adapter.relay.listening)
            await assert_refused(relay_port)
            writer.close()

            # after reconnect the relay stays down until a new valid line arrives
            conn2 = await upstream.accept()
            await wait_until(lambda: adapter.health.connected)
            await assert_refused(relay_port)
            await send(conn2, fresh_line(4) + b"\r\n")
            await wait_until(lambda: adapter.relay.listening)
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", relay_port)
            await send(conn2, fresh_line(5) + b"\r\n")
            assert (await asyncio.wait_for(reader2.readline(), 3.0)).startswith(b"MSG,6,")
            writer2.close()
            conn2.close()
        finally:
            adapter.request_stop()
            await asyncio.wait_for(run_task, 5.0)
            await upstream.stop()

    async def test_unpadded_icao_is_relayed_padded_and_counted(self):
        """ADR-0007: the ICAO field is left-zero-padded before relay — the one
        enumerated exception to verbatim forwarding — the original terminator
        is preserved byte for byte, and the line counts both as a normal ok
        record and on records_normalized_total{normalization="icao_zero_pad"}."""
        upstream = FakeUpstream()
        await upstream.start()
        relay_port = free_port()
        adapter = Adapter(adapter_config(upstream.port, relay_port))

        def count(result: str) -> float:
            return adapter.metrics.records_total.labels("adsbhub", result)._value.get()

        def normalized() -> float:
            return adapter.metrics.records_normalized_total.labels(
                "adsbhub", "icao_zero_pad")._value.get()

        # ops-7 style pre-initialization: the series is 0, not missing
        assert normalized() == 0.0
        run_task = asyncio.create_task(adapter.run())
        try:
            conn = await upstream.accept()
            await send(conn, fresh_line() + b"\r\n")  # first valid line opens the relay
            await wait_until(lambda: adapter.relay.listening)
            reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)

            for terminator in (b"\r\n", b"\n"):
                wire_line = unpadded_line()
                await send(conn, wire_line + terminator)
                received = await asyncio.wait_for(reader.readline(), 3.0)
                assert received.split(b",")[4] == b"0D0EEC"
                # only the zero inserted; same terminator as received upstream
                assert received == wire_line.replace(b"D0EEC", b"0D0EEC", 1) + terminator

            await wait_until(lambda: normalized() == 2.0)
            assert count("ok") == 3.0  # padded lines are ordinary ok records
            assert count("invalid_structure") == 0.0
            writer.close()
            conn.close()
        finally:
            adapter.request_stop()
            await asyncio.wait_for(run_task, 5.0)
            await upstream.stop()

    async def test_oversized_line_counted_and_stream_continues(self):
        upstream = FakeUpstream()
        await upstream.start()
        adapter = Adapter(adapter_config(upstream.port, free_port()))
        run_task = asyncio.create_task(adapter.run())
        try:
            conn = await upstream.accept()
            # > line_max_bytes (4096) without a newline, then a valid line:
            # the oversized line is dropped and counted once; the stream survives
            # (spec §12.1) and the next line still validates.
            await send(conn, b"M" * 9000 + b"\n")
            await send(conn, fresh_line() + b"\r\n")
            await wait_until(lambda: adapter.relay.listening)

            def count(result: str) -> float:
                return adapter.metrics.records_total.labels("adsbhub", result)._value.get()

            assert count("invalid_structure") == 1.0
            assert count("ok") == 1.0
            conn.close()
        finally:
            adapter.request_stop()
            await asyncio.wait_for(run_task, 5.0)
            await upstream.stop()


class TestRelayFreshnessWatchdog:
    """L1 (DESIGN.md §4 / spec §14.4 row 1): the relay serves only while the
    upstream is healthy — connected AND a valid record within the same 30 s
    threshold /healthz uses. A live TCP session feeding only invalid lines or
    keepalives must not keep the relay open past that threshold."""

    async def test_invalid_only_feed_closes_relay_and_valid_line_reopens(self):
        class Mono:
            now = 1000.0

            def __call__(self) -> float:
                return self.now

        mono = Mono()
        upstream = FakeUpstream()
        await upstream.start()
        relay_port = free_port()
        adapter = Adapter(adapter_config(upstream.port, relay_port), mono=mono)
        run_task = asyncio.create_task(adapter.run())
        try:
            conn = await upstream.accept()
            await send(conn, fresh_line() + b"\r\n")
            await wait_until(lambda: adapter.relay.listening)
            reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)

            # within the threshold, invalid lines do not close the relay
            mono.now += 29.0
            await send(conn, b"GARBAGE LINE THAT IS LONG ENOUGH BUT NOT SBS\r\n")
            await asyncio.sleep(0.1)
            assert adapter.relay.listening

            # past the 30 s /healthz threshold with still nothing valid: the
            # relay must stop listening and drop its connected client, even
            # though the TCP session is alive and bytes keep flowing
            mono.now += 2.0
            await send(conn, b"STILL GARBAGE, KEEPING THE SESSION VERY ALIVE\r\n")
            await wait_until(lambda: not adapter.relay.listening)
            assert await asyncio.wait_for(reader.read(), 3.0) == b""  # client closed
            await assert_refused(relay_port)
            writer.close()

            # keepalive-only traffic must not reopen it either
            await send(conn, b"\r\n")
            await asyncio.sleep(0.1)
            await assert_refused(relay_port)

            # the next valid line resumes service
            await send(conn, fresh_line(4) + b"\r\n")
            await wait_until(lambda: adapter.relay.listening)
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", relay_port)
            await send(conn, fresh_line(5) + b"\r\n")
            assert (await asyncio.wait_for(reader2.readline(), 3.0)).startswith(b"MSG,6,")
            writer2.close()
            conn.close()
        finally:
            adapter.request_stop()
            await asyncio.wait_for(run_task, 5.0)
            await upstream.stop()


class TestRelayServerUnit:
    async def test_heartbeat_on_idle(self):
        relay = RelayServer("127.0.0.1", 0, heartbeat_interval=0.1, metrics=AdapterMetrics())
        await relay.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
            beat = await asyncio.wait_for(reader.readexactly(2), 1.0)
            assert beat == b"\r\n"  # keeps readsb's 120 s idle-kill away
            relay.broadcast(b"MSG,3,line\r\n")
            line = b"\r\n"
            while line == b"\r\n":  # tolerate an interleaved heartbeat
                line = await asyncio.wait_for(reader.readline(), 1.0)
            assert line == b"MSG,3,line\r\n"
            writer.close()
        finally:
            await relay.stop()

    async def test_max_clients_enforced(self):
        relay = RelayServer("127.0.0.1", 0, max_clients=2, heartbeat_interval=10.0,
                            metrics=AdapterMetrics())
        await relay.start()
        try:
            c1 = await asyncio.open_connection("127.0.0.1", relay.port)
            c2 = await asyncio.open_connection("127.0.0.1", relay.port)
            await wait_until(lambda: relay.client_count == 2)
            r3, w3 = await asyncio.open_connection("127.0.0.1", relay.port)
            assert await asyncio.wait_for(r3.read(), 2.0) == b""  # closed immediately
            assert relay.client_count == 2
            for _, writer in (c1, c2):
                writer.close()
            w3.close()
        finally:
            await relay.stop()

    async def test_queue_overflow_drops_client_and_counts_lines(self):
        metrics = AdapterMetrics()
        relay = RelayServer("127.0.0.1", 0, queue_max_lines=3, heartbeat_interval=10.0,
                            metrics=metrics)
        await relay.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
            await wait_until(lambda: relay.client_count == 1)
            # burst without yielding: the sender task cannot drain between puts,
            # so the bounded queue overflows -> drop the client (AI-08)
            for n in range(6):
                relay.broadcast(b"MSG,line %d\r\n" % n)
            await wait_until(lambda: relay.client_count == 0)
            assert metrics.relay_dropped_lines_total._value.get() == 4.0  # 3 queued + 1 overflowing
            assert await asyncio.wait_for(reader.read(), 2.0) == b""
            writer.close()
        finally:
            await relay.stop()

    async def test_stop_closes_clients_and_listener(self):
        relay = RelayServer("127.0.0.1", 0, metrics=AdapterMetrics())
        await relay.start()
        port = relay.port
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await wait_until(lambda: relay.client_count == 1)
        await relay.stop()
        assert not relay.listening
        assert await asyncio.wait_for(reader.read(), 2.0) == b""
        await assert_refused(port)
        writer.close()

    async def test_stop_completes_with_stalled_reader_client(self):
        # cc-1 interleaving A: a hung-but-alive peer stops reading; the sender
        # blocks in drain() with unflushed transport data, so an unbounded
        # writer.wait_closed() would never return. stop() must abort instead.
        relay = RelayServer("127.0.0.1", 0, queue_max_lines=100000,
                            heartbeat_interval=30.0, metrics=AdapterMetrics())
        await relay.start()
        sock = socket.socket()
        # tiny receive buffer, set before connect, and never read from
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.connect(("127.0.0.1", relay.port))
        try:
            await wait_until(lambda: relay.client_count == 1)
            chunk = b"M" * 65536 + b"\r\n"
            for _ in range(64):  # ~4 MiB: far beyond loopback socket buffers
                relay.broadcast(chunk)
            await asyncio.sleep(0.2)  # sender now blocked in drain()
            await asyncio.wait_for(relay.stop(), timeout=2.5)
            assert relay.client_count == 0
            assert not relay.listening
        finally:
            sock.close()

    async def test_overflow_drop_of_stalled_client_does_not_hang_stop(self):
        # cc-1 interleaving B: the overflow-dropped client's fire-and-forget
        # close must abort() so its _on_client handler leaves reader.read();
        # otherwise server.wait_closed() (Py3.12 waits for handlers) hangs.
        relay = RelayServer("127.0.0.1", 0, queue_max_lines=2,
                            heartbeat_interval=30.0, metrics=AdapterMetrics())
        await relay.start()
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.connect(("127.0.0.1", relay.port))
        sock.setblocking(False)
        try:
            await wait_until(lambda: relay.client_count == 1)
            chunk = b"M" * 65536 + b"\r\n"
            # yield between broadcasts so the sender fills the socket path and
            # blocks in drain() with unflushed data...
            for _ in range(20):
                relay.broadcast(chunk)
                await asyncio.sleep(0.01)
            # ...then burst without yielding so the bounded queue must overflow
            for _ in range(4):
                relay.broadcast(chunk)
            await wait_until(lambda: relay.client_count == 0)
            # the force-abort must have freed the _on_client handler, so a
            # bounded stop() succeeds even though the peer never read a byte
            await asyncio.wait_for(relay.stop(), timeout=2.5)
            assert not relay.listening
        finally:
            sock.close()

    async def test_client_disconnect_updates_gauge(self):
        metrics = AdapterMetrics()
        relay = RelayServer("127.0.0.1", 0, metrics=metrics)
        await relay.start()
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", relay.port)
            await wait_until(lambda: relay.client_count == 1)
            assert metrics.relay_connected_clients._value.get() == 1.0
            writer.close()
            await wait_until(lambda: relay.client_count == 0)
            assert metrics.relay_connected_clients._value.get() == 0.0
        finally:
            await relay.stop()


class TestSessionCrashContainment:
    """cc-2: a non-OSError from the connect phase (e.g. UnicodeError on a bad
    hostname) must behave like a connect failure, and a genuinely escaped task
    exception must propagate as a nonzero exit instead of a silent 0."""

    async def test_connect_valueerror_backs_off_and_stays_alive(self, monkeypatch):
        async def bad_open_connection(*args, **kwargs):
            # UnicodeError (a ValueError, not OSError): >63-char DNS label
            raise ValueError("hostname label too long")

        monkeypatch.setattr(asyncio, "open_connection", bad_open_connection)
        adapter = Adapter(adapter_config(1, free_port()))
        run_task = asyncio.create_task(adapter.run())
        try:
            def connect_errors() -> float:
                return adapter.metrics.reconnects_total.labels(
                    "adsbhub", "connect_error")._value.get()

            # >= 2 proves the session loop survived the first failure and
            # retried after backoff instead of crashing out
            await wait_until(lambda: connect_errors() >= 2)
            async with aiohttp.ClientSession() as session:
                url = f"http://127.0.0.1:{adapter.metrics_server.port}/healthz"
                async with session.get(url) as resp:
                    assert resp.status == 503
        finally:
            adapter.request_stop()
            await asyncio.wait_for(run_task, 5.0)
        assert run_task.exception() is None

    async def test_session_loop_crash_propagates(self, monkeypatch):
        adapter = Adapter(adapter_config(1, free_port()))

        async def crash():
            raise RuntimeError("boom")

        monkeypatch.setattr(adapter, "_session_loop", crash)
        with pytest.raises(RuntimeError, match="boom"):
            await adapter.run()

    def test_main_exits_nonzero_on_crash(self, tmp_path, monkeypatch):
        from adsbhub_adapter import __main__ as adapter_main

        async def crash(self):
            raise RuntimeError("boom")

        monkeypatch.setattr(Adapter, "run", crash)
        cfg = tmp_path / "adapter.yaml"
        cfg.write_text("config_version: 1\n")
        assert adapter_main.main(["--config", str(cfg)]) == 1
