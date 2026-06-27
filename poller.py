"""
Live poller for World Cup matches.
Polls 365Scores for live updates and forwards to Rust backend.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from dotenv import load_dotenv
from mongo_store import FixtureStore
from forwarder import Forwarder
from sources import threesixtyfive
import config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worldcup_poller.poller")

POLL_INTERVAL_SECONDS = 30  # Poll every 30 seconds for live matches


class Poller:
    def __init__(self, store: FixtureStore, forwarder: Forwarder):
        self.store = store
        self.forwarder = forwarder
        self.running = False

    def start(self):
        """Start polling loop."""
        self.running = True
        logger.info("Poller started. Polling every %d seconds", POLL_INTERVAL_SECONDS)
        
        while self.running:
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f"Poll cycle failed: {e}")
            
            time.sleep(POLL_INTERVAL_SECONDS)

    def poll_once(self):
        """Perform one poll cycle."""
        # Get live fixtures
        live_fixtures = self.store.get_in_progress_fixtures()
        
        if not live_fixtures:
            logger.debug("No live fixtures to poll")
            return

        logger.info(f"Polling {len(live_fixtures)} live fixtures")

        for fixture in live_fixtures:
            match_id = fixture.get("match_id")
            game_id = fixture.get("threesixtyfive_game_id")
            
            if not game_id:
                logger.warning(f"No 365Scores game_id for {match_id}, skipping")
                continue

            self._poll_fixture(match_id, game_id, fixture)

    def _poll_fixture(self, match_id: str, game_id: str, fixture: Dict[str, Any]):
        """Poll a single fixture for updates."""
        try:
            # Fetch game details from 365Scores
            game_data = threesixtyfive.fetch_game_details(game_id)
            if not game_data:
                logger.warning(f"No data for {game_id}")
                return

            # Extract live data
            home_score = game_data.get("homeScore")
            away_score = game_data.get("awayScore")
            status_text = game_data.get("statusText", "")
            status = self._map_status(status_text)
            time_elapsed = game_data.get("timeElapsed")

            # Get events
            events = game_data.get("events", [])
            
            # Check for new events (not yet forwarded)
            forwarded = self.store.get_forwarded_event_signatures(match_id)
            new_events = self._get_new_events(events, forwarded, fixture)

            # Update database
            if home_score is not None:
                self.store.update_score(match_id, home_score, away_score)
            
            if status != fixture.get("status"):
                self.store.update_status(match_id, status)

            # Forward updates to Rust API
            if new_events:
                self._forward_events(match_id, new_events, fixture)
            
            # Forward commentary
            commentary = game_data.get("commentary", [])
            if commentary:
                self._forward_commentary(match_id, commentary, fixture)

            self.store.record_last_poll(match_id)

        except Exception as e:
            logger.error(f"Error polling {match_id}: {e}")

    def _map_status(self, status_text: str) -> str:
        """Map 365Scores status to internal status."""
        text = (status_text or "").strip().lower()
        if text in ("finished", "ft", "ended", "full-time"):
            return "completed"
        if text in ("", "scheduled", "not started"):
            return "upcoming"
        return "live"

    def _get_new_events(self, events: list, forwarded: set, fixture: Dict) -> list:
        """Get events not yet forwarded."""
        new_events = []
        for event in events:
            # Build signature
            signature = f"{event.get('type')}:{event.get('minute')}:{event.get('team')}"
            if signature not in forwarded:
                new_events.append(event)
                self.store.add_forwarded_event_signature(
                    fixture.get("match_id"), signature
                )
        return new_events

    def _forward_events(self, match_id: str, events: list, fixture: Dict):
        """Forward events to Rust API."""
        for event in events:
            event_type = event.get("type", "unknown")
            minute = event.get("minute", 0)
            team = event.get("team", "")
            player = event.get("player", "")
            
            # Build event payload for Rust API
            payload = {
                "fixture_id": match_id,
                "event_type": event_type,
                "minute": minute,
                "team": team,
                "player": player,
                "home_score": fixture.get("home_score"),
                "away_score": fixture.get("away_score"),
            }
            
            self.forwarder.forward_event(payload)

    def _forward_commentary(self, match_id: str, commentary: list, fixture: Dict):
        """Forward commentary to Rust API."""
        for entry in commentary:
            payload = {
                "match_id": match_id,
                "entry": {
                    "minute": entry.get("minute", 0),
                    "text": entry.get("text", ""),
                    "type": entry.get("type", "general"),
                }
            }
            self.forwarder.forward_commentary(payload)


def main():
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        logger.error("MONGO_URI environment variable is required")
        sys.exit(1)

    api_url = os.environ.get("FANCLASH_API", "https://clash-api-m5mr.onrender.com/api")
    
    store = FixtureStore(mongo_uri)
    forwarder = Forwarder(api_url)
    poller = Poller(store, forwarder)

    try:
        poller.start()
    except KeyboardInterrupt:
        logger.info("Stopping poller...")
        poller.running = False
    finally:
        store.close()


if __name__ == "__main__":
    main()