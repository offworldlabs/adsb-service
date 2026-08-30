"""Live in-process smoke test (task: 10-second smoke, no external processes):
a fake ADSBHub server on a random loopback port emits golden lines; the
adapter is configured against it; assert the relay serves the lines verbatim
and the metrics/health endpoints respond, then stop gracefully."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from adsbhub_adapter.adapter import Adapter
from adsbhub_adapter.config import load_config_dict

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sbs"
GOLDEN = [(FIXTURES / f"golden_msg_{t}.txt").read_bytes().splitlines()[0] for t in range(1, 9)]


def fresh(line: bytes) -> bytes:
    fields = line.decode("ascii").split(",")
    stamp = datetime.now(timezone.utc)
    fields[6] = fields[8] = stamp.strftime("%Y/%m/%d")
    fields[7] = fields[9] = stamp.strftime("%H:%M:%S.%f")[:-3]
    return ",".join(fields).encode("ascii") + b"\r\n"


async def test_adapter_smoke():
    sent: list[bytes] = []
    stop_feeding = asyncio.Event()

    async def feed(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while not stop_feeding.is_set():
                for golden in GOLDEN:
                    line = fresh(golden)
                    sent.append(line)
                    writer.write(line)
                await writer.drain()
                await asyncio.sleep(0.1)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    upstream = await asyncio.start_server(feed, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]

    cfg = load_config_dict({
        "config_version": 1,
        "adsbhub": {
            "host": "127.0.0.1",
            "port": upstream_port,
            "connect_timeout": "2s",
            "idle_timeout": "10s",
            "reconnect": {"initial": "50ms", "maximum": "100ms"},
        },
        "relay": {"port": 0},    # adapter.relay.port reports the bound port
        "metrics": {"port": 0},  # adapter.metrics_server.port reports the bound port
    })
    adapter = Adapter(cfg)
    run_task = asyncio.create_task(adapter.run())

    try:
        async with asyncio.timeout(10):
            while not adapter.relay.listening:
                await asyncio.sleep(0.05)

            reader, writer = await asyncio.open_connection("127.0.0.1", adapter.relay.port)
            received = []
            while len(received) < 8:
                line = await reader.readline()
                assert line, "relay closed unexpectedly"
                if line != b"\r\n":
                    received.append(line)
            for line in received:
                assert line.startswith(b"MSG,")
                assert line in sent, "relayed line is not byte-identical to an upstream line"

            base = f"http://127.0.0.1:{adapter.metrics_server.port}"
            async with aiohttp.ClientSession() as session:
                async with session.get(base + "/metrics") as resp:
                    assert resp.status == 200
                    text = await resp.text()
                assert 'retina_adsb_provider_connected{provider="adsbhub"} 1.0' in text
                assert 'retina_adsb_provider_records_total{provider="adsbhub",result="ok"}' in text
                assert "retina_adsb_provider_bytes_total" in text
                assert "retina_adsb_provider_record_delay_seconds_bucket" in text
                assert "retina_adsb_relay_connected_clients 1.0" in text

                async with session.get(base + "/healthz") as resp:
                    assert resp.status == 200
                    body = await resp.json()
                assert body["healthy"] is True
                assert body["connected"] is True
                assert body["last_valid_record_age_seconds"] < 30.0

            writer.close()
    finally:
        stop_feeding.set()
        adapter.request_stop()  # same path as the SIGTERM handler
        await asyncio.wait_for(run_task, 5.0)
        upstream.close()
        await upstream.wait_closed()

    # graceful shutdown: relay and metrics endpoints are gone
    assert not adapter.relay.listening
    assert run_task.done() and run_task.exception() is None
