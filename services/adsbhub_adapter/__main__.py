"""Entry point for the adsbhub-adapter console script."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import ConfigError, load_config, redacted_config_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adsbhub-adapter",
        description="ADSBHub SBS ingest, validation, and loopback relay (RETINA Phase A).",
    )
    parser.add_argument("--config", required=True, metavar="PATH", help="YAML config file")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the config, print the redacted config hash, and exit "
             "without opening any network socket (spec §18.1)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    digest = redacted_config_hash(cfg)

    if args.check_config:
        print(f"config ok: {args.config}")
        print(f"config_hash: sha256:{digest}")
        return 0

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("adsbhub_adapter").info(
        "starting adsbhub-adapter (config_hash sha256:%s)", digest)

    from .adapter import Adapter

    try:
        asyncio.run(Adapter(cfg).run())
    except KeyboardInterrupt:
        pass
    except Exception:
        # cc-2: exiting 0 on an unexpected crash would mask a crash-loop
        logging.getLogger("adsbhub_adapter").exception("adapter crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
