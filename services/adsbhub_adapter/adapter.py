"""Asyncio orchestration: upstream client, backoff engine, loopback relay.

Behavior per DESIGN.md §4: one long-lived upstream TCP session with a
line-size cap enforced before allocation, exponential backoff with full
jitter, and a loopback SBS relay that listens only while the upstream is
healthy per spec §14.4 row 1 (connected AND a valid record within the 30 s
/healthz threshold) and closes its clients on upstream failure (spec §6.1 S1).
"""

from __future__ import annotations

import asyncio
import logging
import random
import signal
import socket
import time
from typing import Callable

from . import __version__
from .config import AdapterConfig, DiagnosticsConfig, redacted_config_hash
from .metrics import UNHEALTHY_AFTER_SECONDS, AdapterMetrics, HealthState, MetricsServer
from .sbs import ICAO_ZERO_PAD, INVALID_STRUCTURE, OK, validate_line

log = logging.getLogger("adsbhub_adapter")

# spec §6.1 S1: 'close relay client connections immediately' on upstream
# failure/shutdown. asyncio defers connection_lost until the transport flushes,
# so a hung-but-alive peer with unread data would stall wait_closed() forever;
# past this bound the connection is aborted (RST) — readsb reconnects.
CLOSE_TIMEOUT = 1.0


class Backoff:
    """Exponential backoff: doubling from ``initial`` to ``maximum``, FULL
    jitter (spec §24: delay = uniform(0, base)), reset after a connection
    that stayed up for ``stable_reset`` seconds."""

    def __init__(
        self,
        initial: float = 1.0,
        maximum: float = 60.0,
        stable_reset: float = 60.0,
        *,
        rng: Callable[[], float] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._initial = initial
        self._maximum = maximum
        self._stable_reset = stable_reset
        self._rng = rng if rng is not None else random.random
        self._clock = clock
        self._attempt = 0
        self._connected_at: float | None = None

    @property
    def attempt(self) -> int:
        return self._attempt

    def next_delay(self) -> float:
        base = min(self._maximum, self._initial * (2 ** min(self._attempt, 32)))
        self._attempt += 1
        return self._rng() * base

    def note_connected(self) -> None:
        self._connected_at = self._clock()

    def note_disconnected(self) -> None:
        if self._connected_at is not None and self._clock() - self._connected_at >= self._stable_reset:
            self._attempt = 0
        self._connected_at = None

    def reset(self) -> None:
        self._attempt = 0
        self._connected_at = None


class DiagnosticSampler:
    """Bounded, opt-in sampling of rejected lines (spec §21.5, §6.1 S1)."""

    def __init__(self, cfg: DiagnosticsConfig, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._clock = clock
        self._started: float | None = None
        self._emitted = 0

    def sample(self, reason: str, line: bytes) -> None:
        if not self._cfg.sample_enabled:
            return
        now = self._clock()
        if self._started is None:
            self._started = now
        if now - self._started > self._cfg.sample_duration:
            return
        if self._emitted >= self._cfg.sample_max_lines:
            return
        self._emitted += 1
        # bounded excerpt only, never the full payload (spec §12.1/§21.5)
        log.warning("diagnostic sample [%s] reason=%s line[:64]=%r",
                    self._cfg.sample_reason, reason, bytes(line[:64]))


class _RelayClient:
    __slots__ = ("writer", "queue", "sender_task", "peer")

    def __init__(self, writer: asyncio.StreamWriter, queue_max: int) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_max)
        self.sender_task: asyncio.Task | None = None
        self.peer = writer.get_extra_info("peername")


class RelayServer:
    """Loopback SBS relay: bounded per-client queues, heartbeat on idle,
    hard client cap, and a listen lifecycle driven by upstream health."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 31003,
        *,
        max_clients: int = 2,
        queue_max_lines: int = 20000,
        heartbeat_interval: float = 25.0,
        metrics: AdapterMetrics | None = None,
    ) -> None:
        self._host = host
        self.port = port
        self._max_clients = max_clients
        self._queue_max_lines = queue_max_lines
        self._heartbeat_interval = heartbeat_interval
        self._metrics = metrics
        self._server: asyncio.Server | None = None
        self._clients: set[_RelayClient] = set()

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._on_client, self._host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("relay listening on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        # close clients before wait_closed(): since 3.12 wait_closed() waits
        # for connection handlers, which only finish once their client is gone
        for client in list(self._clients):
            self._detach(client, "relay stopping")
            await self._close(client)
        if server is not None:
            # Py3.12 wait_closed() also waits for connection handlers; a handler
            # stuck on a hung client must not stall shutdown (cc-1).
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=CLOSE_TIMEOUT)
            except TimeoutError:
                log.warning("relay wait_closed timed out; continuing shutdown")
            log.info("relay stopped listening")

    def broadcast(self, line: bytes) -> None:
        for client in list(self._clients):
            try:
                client.queue.put_nowait(line)
            except asyncio.QueueFull:
                # Bounded everything (AI-08): the client is too slow. Every line
                # still queued plus this one will never reach it — count them
                # all and drop the client connection (DESIGN.md §4).
                lost = client.queue.qsize() + 1
                if self._metrics is not None:
                    self._metrics.relay_dropped_lines_total.inc(lost)
                self._detach(client, f"queue overflow, {lost} lines dropped")
                if client.sender_task is not None:
                    # cancel now so the doomed queue is not partially flushed
                    client.sender_task.cancel()
                # force: the client is already too slow to drain; abort() frees
                # its blocked _on_client handler instead of waiting on a flush
                # that a stalled reader may never allow (cc-1 interleaving B)
                asyncio.get_running_loop().create_task(self._close(client, force=True))

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._server is None or len(self._clients) >= self._max_clients:
            log.warning("relay refusing extra client (connected=%d, max=%d)",
                        len(self._clients), self._max_clients)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        client = _RelayClient(writer, self._queue_max_lines)
        self._clients.add(client)
        self._set_gauge()
        log.info("relay client connected: %s", client.peer)
        client.sender_task = asyncio.create_task(self._sender(client))
        try:
            while True:
                # readsb sends '\r\n' keepalives on sbs_in connectors; discard input
                chunk = await reader.read(1024)
                if not chunk:
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            self._detach(client, "client disconnected")
            await self._close(client)

    async def _sender(self, client: _RelayClient) -> None:
        try:
            while True:
                try:
                    line = await asyncio.wait_for(client.queue.get(), timeout=self._heartbeat_interval)
                except TimeoutError:
                    # idle heartbeat so readsb's 120 s connector idle-kill never fires
                    client.writer.write(b"\r\n")
                    await client.writer.drain()
                    continue
                client.writer.write(line)
                await client.writer.drain()
        except (ConnectionError, OSError):
            self._detach(client, "write failed")
            client.writer.close()

    def _detach(self, client: _RelayClient, why: str) -> None:
        if client in self._clients:
            self._clients.discard(client)
            self._set_gauge()
            log.info("relay client %s dropped: %s", client.peer, why)

    async def _close(self, client: _RelayClient, *, force: bool = False) -> None:
        if client.sender_task is not None and client.sender_task is not asyncio.current_task():
            client.sender_task.cancel()
        if force:
            self._abort(client.writer)
            return
        client.writer.close()
        try:
            # wait_closed() only returns once the write buffer flushes; a peer
            # that stopped reading would block it forever (cc-1 interleaving A)
            await asyncio.wait_for(client.writer.wait_closed(), timeout=CLOSE_TIMEOUT)
        except TimeoutError:
            self._abort(client.writer)
        except (ConnectionError, OSError):
            pass

    @staticmethod
    def _abort(writer: asyncio.StreamWriter) -> None:
        transport = writer.transport
        if transport is not None:
            transport.abort()

    def _set_gauge(self) -> None:
        if self._metrics is not None:
            self._metrics.relay_connected_clients.set(len(self._clients))


class Adapter:
    """The adsbhub-adapter process: upstream session loop + relay + metrics."""

    def __init__(
        self,
        cfg: AdapterConfig,
        *,
        metrics: AdapterMetrics | None = None,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self.cfg = cfg
        self.metrics = metrics if metrics is not None else AdapterMetrics()
        self.health = HealthState()
        self._clock = clock
        self._mono = mono
        self._sampler = DiagnosticSampler(cfg.diagnostics, clock=mono)
        self.relay = RelayServer(
            cfg.relay.host,
            cfg.relay.port,
            max_clients=cfg.relay.max_clients,
            queue_max_lines=cfg.relay.client_queue_max_lines,
            heartbeat_interval=cfg.relay.heartbeat_interval,
            metrics=self.metrics,
        )
        reconnect = cfg.adsbhub.reconnect
        self.backoff = Backoff(reconnect.initial, reconnect.maximum, reconnect.stable_reset,
                               rng=rng, clock=mono)
        self.metrics_server = MetricsServer(self.metrics, self.health,
                                            cfg.metrics.host, cfg.metrics.port, clock=mono)
        self.metrics.set_build_info(__version__, redacted_config_hash(cfg))
        self._stop_event: asyncio.Event | None = None

    def request_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def run(self) -> None:
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                pass
        await self.metrics_server.start()
        session_task = asyncio.create_task(self._session_loop())
        stop_task = asyncio.create_task(self._stop_event.wait())
        failure: BaseException | None = None
        try:
            await asyncio.wait({session_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            session_task.cancel()
            stop_task.cancel()
            results = await asyncio.gather(session_task, stop_task, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    log.error("task failed", exc_info=result)
                    failure = result
            await self.relay.stop()
            await self.metrics_server.stop()
            for sig in installed:
                loop.remove_signal_handler(sig)
            log.info("adapter stopped")
        if failure is not None:
            # cc-2: an escaped task exception is a crash, not a clean stop —
            # propagate so main() exits nonzero and the crash-loop is visible
            raise failure

    async def _session_loop(self) -> None:
        while True:
            reason = await self._run_session()
            self.metrics.reconnect(reason)
            # spec §6.1 S1: upstream failure closes relay clients AND stops listening
            await self.relay.stop()
            self.backoff.note_disconnected()
            delay = self.backoff.next_delay()
            log.warning("upstream session ended (%s); reconnecting in %.2fs", reason, delay)
            await asyncio.sleep(delay)

    async def _run_session(self) -> str:
        up = self.cfg.adsbhub
        try:
            reader, writer = await asyncio.wait_for(
                # limit= bounds StreamReader buffering so readuntil enforces the
                # line cap before a full oversized line is ever allocated (§12.1)
                asyncio.open_connection(up.host, up.port, limit=up.line_max_bytes),
                timeout=up.connect_timeout,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning("connect to %s:%d timed out", up.host, up.port)
            return "connect_timeout"
        except OSError as exc:
            log.warning("connect to %s:%d failed: %s", up.host, up.port, exc)
            return "connect_error"
        except Exception:
            # cc-2: e.g. UnicodeError (a ValueError, not OSError) from a >63-char
            # DNS label. §24: a bad endpoint must behave like a connect failure —
            # count, back off, keep running — never crash the session loop.
            log.exception("connect to %s:%d failed unexpectedly", up.host, up.port)
            return "connect_error"

        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
        log.info("connected to upstream %s:%d", up.host, up.port)
        self.metrics.set_connected(True)
        self.health.connected = True
        self.backoff.note_connected()
        try:
            return await self._read_loop(reader)
        except asyncio.CancelledError:
            raise
        except Exception:
            # §12.1/§24: input must never crash the process
            log.exception("unexpected error in upstream read loop")
            return "internal_error"
        finally:
            self.metrics.set_connected(False)
            self.health.connected = False
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def _relay_stale(self) -> bool:
        last = self.health.last_valid_monotonic
        return last is None or self._mono() - last > UNHEALTHY_AFTER_SECONDS

    async def _enforce_relay_freshness(self) -> None:
        """DESIGN.md §4 relay gate: serve only while the upstream is healthy
        per spec §14.4 row 1 — connected AND a valid record within the same
        30 s threshold /healthz uses. A live TCP session delivering only
        invalid lines or keepalive bytes must not keep the relay open; the
        relay resumes on the next valid line."""
        if self.relay.listening and self._relay_stale():
            log.warning("no valid record for >%.0f s; closing relay until the feed recovers",
                        UNHEALTHY_AFTER_SECONDS)
            await self.relay.stop()

    async def _read_loop(self, reader: asyncio.StreamReader) -> str:
        up = self.cfg.adsbhub
        discarding = False
        while True:
            try:
                line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=up.idle_timeout)
            except TimeoutError:
                # DESIGN.md §4 / ADR-0006: 30 s of silence = dead session
                return "idle_timeout"
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    self.metrics.add_bytes(len(exc.partial))
                return "eof"
            except asyncio.LimitOverrunError as exc:
                # Oversized line (> line_max_bytes, spec §18.2/§12.1): discard the
                # buffered bytes and keep skipping until the next newline; the
                # whole oversized line counts once as invalid_structure.
                try:
                    data = await asyncio.wait_for(reader.read(max(1, exc.consumed)),
                                                  timeout=up.idle_timeout)
                except TimeoutError:
                    return "idle_timeout"
                if not data:
                    return "eof"
                self.metrics.add_bytes(len(data))
                await self._enforce_relay_freshness()
                if not discarding:
                    discarding = True
                    self.metrics.record(INVALID_STRUCTURE)
                    self._sampler.sample(INVALID_STRUCTURE, data)
                continue
            except (ConnectionError, OSError) as exc:
                log.warning("upstream read failed: %s", exc)
                return "read_error"

            self.metrics.add_bytes(len(line))
            await self._enforce_relay_freshness()
            if discarding:
                discarding = False  # tail of the oversized line
                continue
            stripped = line.rstrip(b"\r\n")
            if not stripped:
                continue  # bare keepalive, not a record
            result = validate_line(
                stripped,
                now=self._clock(),
                future_tolerance=up.future_tolerance,
                max_record_age=up.max_record_age,
                altitude_min_ft=up.altitude_min_ft,
                altitude_max_ft=up.altitude_max_ft,
            )
            self.metrics.record(result.result)
            if result.normalized:
                # ADR-0007: the one rewrite the relay is allowed to make; the
                # ratio to records_total is the provider-format-change signal
                self.metrics.record_normalized(ICAO_ZERO_PAD)
            if result.result == OK:
                self.health.last_valid_monotonic = self._mono()
                self.metrics.set_last_record(self._clock())
                if result.delay is not None:
                    self.metrics.observe_delay(result.delay)
                if not self.relay.listening:
                    # §6.1 S1 / §14.4 row 1: listen only while the upstream
                    # feed is validated and fresh (see _enforce_relay_freshness)
                    await self.relay.start()
            elif not result.relay:
                self._sampler.sample(result.result, stripped)
            if result.relay and result.relay_line is not None:
                # forward verbatim — the validated bytes, with the ICAO field
                # zero-padded when the provider omitted the leading zeros
                # (ADR-0007, the only rewrite) — plus the original terminator
                self.relay.broadcast(result.relay_line + line[len(stripped):])
