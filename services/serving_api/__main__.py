"""Entry point: retina-serving-api [--config PATH] [--check-config]."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from serving_api.app import create_app
from serving_api.config import ConfigError, load_config, redacted_config_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="retina-serving-api",
        description="RETINA interim v2 serving tier (adsb.lol format over readsb re-api).",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="YAML config file (documented defaults apply when omitted)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the config, print its redacted hash, and exit without opening sockets",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        # spec §18.1: --check-config runs without opening network sockets.
        print(f"config ok sha256:{redacted_config_hash(config)}")
        return 0

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = create_app(config)
    # access_log=False: DESIGN §5 forbids client-IP + full-URL request logging
    # (queried paths embed client positions); accounting lives in /metrics.
    uvicorn.run(
        app,
        host=config.bind.host,
        port=config.bind.port,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
