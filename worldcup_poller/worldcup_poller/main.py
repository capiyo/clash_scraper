"""
Entry point for the World Cup live poller.

Usage:
    python main.py
(MONGO_URI read from .env in this folder via python-dotenv.)

Post-pivot: no identity provider, no curl_cffi IPv4/construction-order
setup — none of that applies anymore since 365Scores doesn't need
browser TLS impersonation. This file is intentionally much shorter than
the Sofascore-era version.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv

from mongo_store import FixtureStore
from poller import WorldCupPoller

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worldcup_poller.main")


async def main() -> None:
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        logger.error("MONGO_URI environment variable is required")
        sys.exit(1)

    store = FixtureStore(mongo_uri)
    poller = WorldCupPoller(store=store)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received")
        poller.stop()
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, AttributeError):
            # Windows dev environment fallback
            pass

    poll_task = asyncio.create_task(poller.run_forever())

    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    finally:
        store.close()
        logger.info("Poller stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
