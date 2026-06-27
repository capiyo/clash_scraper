"""
Main entry point for World Cup poller.
Can run as scraper (once) or poller (continuous).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worldcup_poller.main")


def main():
    parser = argparse.ArgumentParser(description="World Cup Poller")
    parser.add_argument(
        "--mode",
        choices=["scrape", "poll", "both"],
        default="scrape",
        help="Run mode: scrape (fetch fixtures once), poll (continuous live updates), both"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once then exit (for cron jobs)"
    )
    args = parser.parse_args()

    # Validate environment
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        logger.error("MONGO_URI environment variable is required")
        sys.exit(1)

    if args.mode == "scrape":
        from scraper import main as scrape_main
        scrape_main()
    elif args.mode == "poll":
        from poller import main as poll_main
        poll_main()
    else:
        # Run both: scrape then poll
        from scraper import main as scrape_main
        from poller import main as poll_main
        
        logger.info("Running scrape first...")
        scrape_main()
        logger.info("Scrape complete. Starting poller...")
        poll_main()


if __name__ == "__main__":
    main()