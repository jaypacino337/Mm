"""CLI entry point: python -m mmbot --config config.yaml [--live] [--venue perpl]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .core.config import load_config
from .core.engine import Engine
from .venues import create_venue


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="mmbot",
        description="Custom market-making bot: Perpl, Phoenix, Variational, Arcus",
    )
    parser.add_argument("--config", default="config.yaml", help="path to YAML config")
    parser.add_argument("--live", action="store_true", help="trade for real")
    parser.add_argument("--dry-run", action="store_true", help="log quotes only")
    parser.add_argument(
        "--venue",
        action="append",
        help="only run these venues (repeatable), e.g. --venue perpl --venue phoenix",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.live and args.dry_run:
        parser.error("--live and --dry-run are mutually exclusive")

    config = load_config(args.config)
    if args.live:
        config.dry_run = False
    if args.dry_run:
        config.dry_run = True
    if args.venue:
        wanted = {v.lower() for v in args.venue}
        config.venues = [v for v in config.venues if v.name.lower() in wanted]
        missing = wanted - {v.name.lower() for v in config.venues}
        if missing:
            parser.error(f"venues not in config: {', '.join(sorted(missing))}")

    venues = [(create_venue(vc, config.dry_run), vc) for vc in config.venues]
    engine = Engine(config, venues)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, engine.stop)
        await engine.run()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
