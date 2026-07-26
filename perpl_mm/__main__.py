"""CLI entry point: python -m perpl_mm --config config.yaml [--live]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .auth import ApiCredentials
from .bot import MarketMakerBot
from .config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="perpl-mm", description="Custom market-making bot for Perpl (perpl.xyz)"
    )
    parser.add_argument("--config", default="config.yaml", help="path to YAML config")
    parser.add_argument(
        "--live",
        action="store_true",
        help="override dry_run in config and trade for real",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="override config and only log quotes (no orders sent)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    if args.live and args.dry_run:
        parser.error("--live and --dry-run are mutually exclusive")
    if args.live:
        config.dry_run = False
    if args.dry_run:
        config.dry_run = True

    credentials = None
    try:
        credentials = ApiCredentials.from_env()
    except RuntimeError as exc:
        if not config.dry_run:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        logging.getLogger(__name__).info("no API credentials; running unauthenticated dry-run")

    bot = MarketMakerBot(config, credentials)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.stop)
        await bot.run()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
