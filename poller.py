"""
Live poller for World Cup matches with full state machine.
Handles: upcoming → soon → live → completed → archived
Fetches lineups when matches are in "soon" state (40-60 mins before kickoff)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

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

# Polling intervals (in seconds)
POLL_INTERVAL_LIVE = 15       # Every 15 seconds for live matches
POLL_INTERVAL_SOON = 60       # Every minute for soon matches
POLL_INTERVAL_UPCOMING = 300  # Every 5 minutes for upcoming matches

# Time thresholds (in minutes before kickoff)
SOON_THRESHOLD_MINUTES = 60   # 1 hour before kickoff = "soon"
LINEUP_EARLY_THRESHOLD = 60   # Start checking at 60 mins before
LINEUP_LATE_THRESHOLD = 40    # Stop checking at 40 mins before
STATS_THRESHOLD_MINUTES = 10  # 10 minutes before kickoff = start stats


class MatchStateMachine:
    """
    Manages match state transitions and determines what data to fetch.
    
    States:
        upcoming:   > 60 mins before kickoff - just basic info
        soon:       10-60 mins before kickoff - fetch lineups, start stats
        live:       kickoff to final whistle - fetch scores, events, stats
        completed:  match ended - fetch final result, move to history
    """
    
    def __init__(self, store: FixtureStore, forwarder: Forwarder):
        self.store = store
        self.forwarder = forwarder
        self.lineups_fetched = set()  # Track which matches we got lineups for
        self.stats_started = set()    # Track which matches stats are active for
        self.completed_notified = set() # Track which matches we finalized

    def determine_state(self, match: Dict[str, Any]) -> str:
        """
        Determine the current state of a match based on kickoff time and current time.
        """
        kickoff_utc = match.get("kickoff_utc")
        if not kickoff_utc:
            return match.get("status", "upcoming")
        
        # If kickoff_utc is a string, parse it
        if isinstance(kickoff_utc, str):
            try:
                kickoff_utc = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
            except:
                return match.get("status", "upcoming")
        
        # Ensure timezone-aware
        if isinstance(kickoff_utc, datetime) and kickoff_utc.tzinfo is None:
            kickoff_utc = kickoff_utc.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        if isinstance(kickoff_utc, datetime):
            minutes_until_kickoff = (kickoff_utc - now).total_seconds() / 60
        else:
            minutes_until_kickoff = float('inf')
        
        # Check if match is already completed
        status = match.get("status", "")
        if status == "completed":
            return "completed"
        
        # Check if match is live (kicked off but not finished)
        if status == "live":
            return "live"
        
        # Check if match has started (we might have missed the status change)
        if minutes_until_kickoff <= 0:
            return "live"
        
        # Check if match is "soon" (within 1 hour)
        if minutes_until_kickoff <= SOON_THRESHOLD_MINUTES:
            return "soon"
        
        # Default: upcoming
        return "upcoming"

    def get_poll_interval(self, state: str) -> int:
        """Get the appropriate poll interval for a match state."""
        if state == "live":
            return POLL_INTERVAL_LIVE
        elif state == "soon":
            return POLL_INTERVAL_SOON
        else:
            return POLL_INTERVAL_UPCOMING

    def should_fetch_lineups(self, match: Dict[str, Any], state: str, minutes_to_kickoff: Optional[float] = None) -> bool:
        """
        Determine if we should fetch lineups for this match.
        
        CRITICAL: This is the main function that decides when to fetch lineups.
        
        Lineups are fetched when:
        1. NOT already fetched (lineups_fetched == False)
        2. Match is in "soon" state AND within 40-60 minutes of kickoff
        3. OR match is "live" and lineups not fetched (we missed the window)
        """
        match_id = match.get("match_id")
        
        # Don't fetch if already fetched or match is completed
        if match_id in self.lineups_fetched:
            return False
        
        if state == "completed":
            return False
        
        # If match is "live" and lineups not fetched, fetch immediately
        if state == "live":
            logger.info(f"📋 {match_id}: Live but no lineups - fetching now")
            return True
        
        # If match is "soon", check time window
        if state == "soon" and minutes_to_kickoff is not None:
            should_fetch = LINEUP_LATE_THRESHOLD <= minutes_to_kickoff <= LINEUP_EARLY_THRESHOLD
            if should_fetch:
                logger.info(f"📋 {match_id}: {minutes_to_kickoff:.0f} mins to kickoff - fetching lineups")
            else:
                logger.debug(f"{match_id}: {minutes_to_kickoff:.0f} mins to kickoff - outside lineup window")
            return should_fetch
        
        return False

    def should_fetch_statistics(self, match: Dict[str, Any], state: str, minutes_to_kickoff: Optional[float] = None) -> bool:
        """
        Determine if we should fetch statistics for this match.
        Statistics are fetched when:
        1. Match is "live"
        2. OR match is "soon" AND within 10 minutes of kickoff
        """
        status = match.get("status", "")
        
        # Don't fetch if match is not live or soon
        if status not in ["live", "soon"]:
            return False
        
        # If match is live, always fetch stats
        if status == "live":
            return True
        
        # If match is "soon", only fetch stats if within 10 minutes
        if status == "soon" and minutes_to_kickoff is not None:
            return minutes_to_kickoff <= STATS_THRESHOLD_MINUTES
        
        return False

    def should_update_status(self, match: Dict[str, Any]) -> Optional[str]:
        """
        Determine if match status should be updated.
        Returns the new status if it should change, None otherwise.
        """
        current_status = match.get("status", "upcoming")
        kickoff_utc = match.get("kickoff_utc")
        minutes_to_kickoff = match.get("minutes_to_kickoff")
        
        if not kickoff_utc:
            return None
        
        if isinstance(kickoff_utc, str):
            try:
                kickoff_utc = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
            except:
                return None
        
        if isinstance(kickoff_utc, datetime) and kickoff_utc.tzinfo is None:
            kickoff_utc = kickoff_utc.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        if minutes_to_kickoff is None and isinstance(kickoff_utc, datetime):
            minutes_to_kickoff = (kickoff_utc - now).total_seconds() / 60
        
        # Don't change completed matches
        if current_status == "completed":
            return None
        
        # Match should be live if kickoff time has passed
        if minutes_to_kickoff is not None and minutes_to_kickoff <= 0 and current_status != "live":
            return "live"
        
        # Match is "soon" if within 1 hour of kickoff
        if minutes_to_kickoff is not None and minutes_to_kickoff <= SOON_THRESHOLD_MINUTES and current_status == "upcoming":
            return "soon"
        
        return None

    def should_finalize_result(self, match: Dict[str, Any]) -> bool:
        """
        Determine if we should finalize the match result.
        This is when:
        1. Match status is "completed"
        2. We haven't notified completion yet
        """
        match_id = match.get("match_id")
        status = match.get("status", "")
        
        if status != "completed":
            return False
        
        if match_id in self.completed_notified:
            return False
        
        return True

    def mark_lineups_done(self, match_id: str):
        """Mark that lineups have been fetched for a match."""
        self.lineups_fetched.add(match_id)

    def mark_stats_started(self, match_id: str):
        """Mark that statistics have started for a match."""
        self.stats_started.add(match_id)

    def mark_completed_notified(self, match_id: str):
        """Mark that completion has been notified."""
        self.completed_notified.add(match_id)


class Poller:
    def __init__(self, store: FixtureStore, forwarder: Forwarder):
        self.store = store
        self.forwarder = forwarder
        self.state_machine = MatchStateMachine(store, forwarder)
        self.running = False
        self.poll_count = 0

    def start(self):
        """Start polling loop."""
        self.running = True
        logger.info(f"🚀 Poller started. Checking all matches...")
        
        while self.running:
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f"Poll cycle failed: {e}", exc_info=True)
            
            self.poll_count += 1
            time.sleep(5)  # Quick check interval

    def poll_once(self):
        """Perform one poll cycle."""
        all_fixtures = self.store.get_all_fixtures()
        
        if not all_fixtures:
            logger.debug("No fixtures found")
            return

        logger.info(f"📊 Poll cycle #{self.poll_count}: Processing {len(all_fixtures)} fixtures")
        
        for match in all_fixtures:
            self._process_match(match)

    def _process_match(self, match: Dict[str, Any]):
        """Process a single match based on its state."""
        match_id = match.get("match_id")
        game_id = match.get("threesixtyfive_game_id")
        
        if not game_id:
            logger.warning(f"No 365Scores game_id for {match_id}, skipping")
            return

        # --- CRITICAL FIX: Calculate minutes until kickoff with timezone handling ---
        kickoff_utc = match.get("kickoff_utc")
        minutes_to_kickoff = None
        
        if kickoff_utc:
            # Handle string
            if isinstance(kickoff_utc, str):
                try:
                    kickoff_utc = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
                except:
                    pass
            
            # Handle datetime
            if isinstance(kickoff_utc, datetime):
                # CRITICAL FIX: Make timezone-aware if naive
                if kickoff_utc.tzinfo is None:
                    kickoff_utc = kickoff_utc.replace(tzinfo=timezone.utc)
                
                now = datetime.now(timezone.utc)
                minutes_to_kickoff = (kickoff_utc - now).total_seconds() / 60
                match["minutes_to_kickoff"] = minutes_to_kickoff

        # Determine current state
        state = self.state_machine.determine_state(match)
        current_status = match.get("status", "upcoming")
        
        # --- STEP 1: UPDATE STATUS IF NEEDED ---
        new_status = self.state_machine.should_update_status(match)
        if new_status and new_status != current_status:
            logger.info(f"📊 {match_id}: {current_status} → {new_status}")
            self.store.update_status(match_id, new_status)
            
            self.forwarder.forward_live_update({
                "fixture_id": match_id,
                "event_type": "status_change",
                "status": new_status,
                "is_live": new_status == "live",
                "available_for_voting": new_status in ["upcoming", "soon"],
                "minutes_to_kickoff": minutes_to_kickoff,
            })
            
            if new_status == "completed":
                self._finalize_match_result(match)
                return
            
            if new_status == "live":
                self._notify_match_live(match)
            
            match["status"] = new_status
            current_status = new_status

        # --- STEP 2: FETCH LINEUPS ---
        if self.state_machine.should_fetch_lineups(match, current_status, minutes_to_kickoff):
            self._fetch_and_forward_lineups(match)
            self.state_machine.mark_lineups_done(match_id)

        # --- STEP 3: FETCH STATISTICS ---
        if self.state_machine.should_fetch_statistics(match, current_status, minutes_to_kickoff):
            self._fetch_and_forward_statistics(match)

        # --- STEP 4: FETCH LIVE UPDATES ---
        if current_status == "live":
            self._fetch_live_updates(match)

        # --- STEP 5: CHECK COMPLETION ---
        if self.state_machine.should_finalize_result(match):
            self._finalize_match_result(match)

        self.store.record_last_poll(match_id)

    def _fetch_and_forward_lineups(self, match: Dict[str, Any]):
        """Fetch lineups and forward to Rust API."""
        match_id = match.get("match_id")
        game_id = match.get("threesixtyfive_game_id")
        
        logger.info(f"📋 Fetching lineups for {match_id}...")
        
        lineups = threesixtyfive.fetch_lineups(game_id)
        if lineups:
            self.store.store_lineups(match_id, lineups)
            success = self.forwarder.forward_lineups(lineups)
            if success:
                self.store.mark_lineups_fetched(match_id)
                logger.info(f"✅ Lineups fetched and forwarded for {match_id}")
            else:
                logger.warning(f"⚠️ Failed to forward lineups for {match_id}")
        else:
            logger.debug(f"No lineups available yet for {match_id}")

    def _fetch_and_forward_statistics(self, match: Dict[str, Any]):
        """Fetch statistics and forward to Rust API."""
        match_id = match.get("match_id")
        game_id = match.get("threesixtyfive_game_id")
        
        stats = threesixtyfive.fetch_statistics(game_id)
        if stats:
            self.store.add_statistics_snapshot(
                match_id, 
                stats.get("statistics", {}), 
                stats.get("minute", 0)
            )
            self.forwarder.forward_statistics(stats)
            logger.debug(f"📊 Statistics forwarded for {match_id} at {stats.get('minute', 0)}'")

    def _fetch_live_updates(self, match: Dict[str, Any]):
        """Fetch live updates (scores, events, commentary)."""
        match_id = match.get("match_id")
        game_id = match.get("threesixtyfive_game_id")
        
        details = threesixtyfive.fetch_game_details(game_id)
        if not details:
            return
        
        home_score = details.get("homeScore")
        away_score = details.get("awayScore")
        if home_score is not None:
            self.store.update_score(match_id, home_score, away_score)
            logger.info(f"📊 {match_id}: Score updated {home_score}-{away_score}")
        
        status_text = details.get("statusText", "").lower()
        if status_text in ("finished", "ft", "ended", "full-time"):
            self.store.update_status(match_id, "completed")
            self._finalize_match_result(match)
            return
        
        live_update = {
            "fixture_id": match_id,
            "event_type": "live_update",
            "home_score": home_score or 0,
            "away_score": away_score or 0,
            "minute": details.get("timeElapsed", 0),
            "status": "live",
        }
        self.forwarder.forward_live_update(live_update)
        
        events = details.get("events", [])
        if events:
            self._process_events(match, events)
        
        commentary = details.get("commentary", [])
        if commentary:
            self._process_commentary(match, commentary)

    def _process_events(self, match: Dict[str, Any], events: List[Dict]):
        """Process and forward match events."""
        match_id = match.get("match_id")
        forwarded = self.store.get_forwarded_event_signatures(match_id)
        new_events = []
        
        for event in events:
            event_type = event.get("type", "unknown")
            minute = event.get("minute", 0)
            team = event.get("team", "")
            signature = f"{event_type}:{minute}:{team}"
            
            if signature not in forwarded:
                new_events.append(event)
                self.store.add_forwarded_event_signature(match_id, signature)
        
        if new_events:
            bulk_payload = {"fixture_id": match_id, "events": new_events}
            self.forwarder.forward_bulk_events(bulk_payload)
            
            for event in new_events:
                event_payload = {
                    "fixture_id": match_id,
                    "event_type": event.get("type", "unknown"),
                    "minute": event.get("minute", 0),
                    "team": event.get("team", ""),
                    "player": event.get("player", ""),
                    "assist": event.get("assist"),
                    "home_score": match.get("home_score", 0),
                    "away_score": match.get("away_score", 0),
                }
                self.forwarder.forward_event(event_payload)
                logger.info(f"⚽ {match_id}: {event.get('type')} at {event.get('minute')}'")

    def _process_commentary(self, match: Dict[str, Any], commentary: List[Dict]):
        """Process and forward match commentary."""
        match_id = match.get("match_id")
        
        for entry in commentary:
            payload = {
                "match_id": match_id,
                "entry": {
                    "minute": entry.get("minute", 0),
                    "text": entry.get("text", ""),
                    "type": entry.get("type", "general"),
                    "team": entry.get("team"),
                    "player": entry.get("player"),
                }
            }
            self.forwarder.forward_commentary(payload)

    def _finalize_match_result(self, match: Dict[str, Any]):
        """Finalize match result and notify Rust API."""
        match_id = match.get("match_id")
        
        game = self.store.get_fixture(match_id)
        if not game:
            logger.warning(f"{match_id}: Cannot finalize - match not found")
            return
        
        home_score = game.get("home_score", 0)
        away_score = game.get("away_score", 0)
        
        if home_score > away_score:
            result = "home"
            winner = game.get("home_team", "Home")
        elif away_score > home_score:
            result = "away"
            winner = game.get("away_team", "Away")
        else:
            result = "draw"
            winner = "None"
        
        finalize_payload = {
            "fixture_id": match_id,
            "result": result,
            "home_score": home_score,
            "away_score": away_score,
            "winner": winner,
        }
        
        success = self.forwarder.finalize_match(finalize_payload)
        if success:
            self.state_machine.mark_completed_notified(match_id)
            logger.info(f"🏁 Match {match_id} finalized: {result} ({home_score}-{away_score})")

    def _archive_completed_match(self, match_id: str):
        """Archive completed match to history."""
        try:
            self.forwarder.move_to_history(match_id)
        except Exception as e:
            logger.debug(f"{match_id}: Move to history skipped: {e}")

    def _notify_match_live(self, match: Dict[str, Any]):
        """Send notification that match is now live."""
        match_id = match.get("match_id")
        home_team = match.get("home_team", "Home")
        away_team = match.get("away_team", "Away")
        
        notification = {
            "fixture_id": match_id,
            "event_type": "match_live",
            "title": f"⚽ {home_team} vs {away_team} is LIVE!",
            "body": f"Match is now live. Follow the action!",
            "data": {
                "home_team": home_team,
                "away_team": away_team,
                "fixture_id": match_id,
                "type": "match_live"
            }
        }
        self.forwarder.forward_notification(notification)
        logger.info(f"🔴 {match_id}: Match is now LIVE!")


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