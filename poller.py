#!/usr/bin/env python3
"""
Live poller for World Cup matches with full state machine.
Handles: upcoming → soon → live → completed → archived
Fetches lineups when matches are in "soon" state (40-60 mins before kickoff)
Uses ONLY 365Scores for all data (scores, statistics, lineups, commentary)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

from dotenv import load_dotenv

from forwarder import Forwarder
from mongo_store import FixtureStore
from sources import threesixtyfive

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worldcup_poller.poller")


def _split_lineup_members(lineup: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a raw 365Scores lineup side
    ({"formation": ..., "members": [...]}) into the shape
    Forwarder.forward_lineups() / the Rust API expects:
    {"formation": ..., "coach": {...}, "players": [...], "bench": [...]}

    365Scores status codes (per member):
      1 = Starting XI, 2 = Substitute, 4 = Management/Coach
    """
    players: list = []
    bench: list = []
    coach: Optional[Dict[str, Any]] = None

    for m in lineup.get("members", []):
        status = m.get("status")

        if status == 4:
            # Coaching staff entry, not a player
            coach = {"name": m.get("name", "Unknown")}
            continue

        position = (m.get("formation") or {}).get("shortName") or \
            (m.get("position") or {}).get("shortName")

        # Extract jersey number - 365Scores uses various field names
        jersey_number = m.get("jerseyNumber") or m.get("jerseyNo") or m.get("number") or m.get("shirtNumber")
        if jersey_number is None:
            jersey_number = 0
        else:
            try:
                jersey_number = int(jersey_number)
            except (ValueError, TypeError):
                jersey_number = 0

        # Check if player is captain - 365Scores may have captain flag
        is_captain = m.get("captain", False)
        if isinstance(is_captain, str):
            is_captain = is_captain.lower() == "true"

        entry = {
            "name": m.get("name", "Unknown"),
            "position": position or "Unknown",
            "jerseyNumber": jersey_number,
            "captain": is_captain,
            "lineup": "starting" if status == 1 else "bench",
            "playerId": str(m["id"]) if m.get("id") is not None else None,
        }

        if status == 1:
            players.append(entry)
        elif status == 2:
            bench.append(entry)

    return {
        "formation": lineup.get("formation", "4-4-2"),
        "coach": coach or {"name": "Unknown"},
        "players": players,
        "bench": bench,
    }


def _build_lineups_payload(
    fixture_id: str, home_team: str, away_team: str, lineups: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Build the payload shape forward_lineups() documents:
    {"fixture_id", "home_team", "away_team", "lineups": {"home": ..., "away": ...}}
    threesixtyfive.fetch_lineups() returns {"fixture_id", "home", "away"} with
    raw "members" arrays -- this reshapes that into what the Rust API wants.
    """
    return {
        "fixture_id": fixture_id,
        "home_team": home_team,
        "away_team": away_team,
        "lineups": {
            "home": _split_lineup_members(lineups.get("home", {}) or {}),
            "away": _split_lineup_members(lineups.get("away", {}) or {}),
        },
    }


# Polling intervals (in seconds)
POLL_INTERVAL_LIVE = 15
POLL_INTERVAL_SOON = 60
POLL_INTERVAL_UPCOMING = 300

# Time thresholds (in minutes before kickoff)
SOON_THRESHOLD_MINUTES = 60
LINEUP_EARLY_THRESHOLD = 60
LINEUP_LATE_THRESHOLD = 40
STATS_THRESHOLD_MINUTES = 10

# Archive check interval
ARCHIVE_CHECK_INTERVAL_SECONDS = 3600
ARCHIVE_AFTER_HOURS = 24


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
        self.lineups_fetched = set()
        self.stats_started = set()
        self.completed_notified = set()

    def determine_state(self, match: Dict[str, Any]) -> str:
        """Determine the current state of a match based on kickoff time."""
        kickoff_utc = match.get("kickoffUtc")
        if not kickoff_utc:
            return match.get("status", "upcoming")

        if isinstance(kickoff_utc, str):
            try:
                kickoff_utc = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
            except ValueError:
                return match.get("status", "upcoming")

        if isinstance(kickoff_utc, datetime) and kickoff_utc.tzinfo is None:
            kickoff_utc = kickoff_utc.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        if isinstance(kickoff_utc, datetime):
            minutes_until_kickoff = (kickoff_utc - now).total_seconds() / 60
        else:
            minutes_until_kickoff = float("inf")

        status = match.get("status", "")
        if status == "completed":
            return "completed"
        if status == "live":
            return "live"
        if minutes_until_kickoff <= 0:
            return "live"
        if minutes_until_kickoff <= SOON_THRESHOLD_MINUTES:
            return "soon"
        return "upcoming"

    def get_poll_interval(self, state: str) -> int:
        """Get the appropriate poll interval for a match state."""
        if state == "live":
            return POLL_INTERVAL_LIVE
        elif state == "soon":
            return POLL_INTERVAL_SOON
        else:
            return POLL_INTERVAL_UPCOMING

    def should_fetch_lineups(
        self,
        match: Dict[str, Any],
        state: str,
        minutes_to_kickoff: Optional[float] = None,
    ) -> bool:
        """
        Determine if we should fetch lineups for this match.

        Lineups are fetched when:
        1. NOT already fetched (lineups_fetched == False)
        2. Match is in "soon" state AND within 40-60 minutes of kickoff
        3. OR match is "live" and lineups not fetched (we missed the window)
        """
        match_id = match.get("matchId")

        if match.get("lineupsFetched"):
            self.lineups_fetched.add(match_id)
            return False

        if match_id in self.lineups_fetched:
            return False

        if state == "completed":
            return False

        if state == "live":
            logger.info(f"📋 {match_id}: Live but no lineups - fetching now")
            return True

        if state == "soon" and minutes_to_kickoff is not None:
            should_fetch = LINEUP_LATE_THRESHOLD <= minutes_to_kickoff <= LINEUP_EARLY_THRESHOLD
            if should_fetch:
                logger.info(
                    f"📋 {match_id}: {minutes_to_kickoff:.0f} mins to kickoff - fetching lineups"
                )
            return should_fetch

        return False

    def should_fetch_statistics(
        self,
        match: Dict[str, Any],
        state: str,
        minutes_to_kickoff: Optional[float] = None,
    ) -> bool:
        """Determine if we should fetch statistics."""
        status = match.get("status", "")

        if status not in ["live", "soon"]:
            return False

        if status == "live":
            return True

        if status == "soon" and minutes_to_kickoff is not None:
            return minutes_to_kickoff <= STATS_THRESHOLD_MINUTES

        return False

    def should_update_status(self, match: Dict[str, Any]) -> Optional[str]:
        """Determine if match status should be updated."""
        current_status = match.get("status", "upcoming")
        kickoff_utc = match.get("kickoffUtc")
        minutes_to_kickoff = match.get("minutes_to_kickoff")

        if not kickoff_utc:
            return None

        if isinstance(kickoff_utc, str):
            try:
                kickoff_utc = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
            except ValueError:
                return None

        if isinstance(kickoff_utc, datetime) and kickoff_utc.tzinfo is None:
            kickoff_utc = kickoff_utc.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        if minutes_to_kickoff is None and isinstance(kickoff_utc, datetime):
            minutes_to_kickoff = (kickoff_utc - now).total_seconds() / 60

        if current_status == "completed":
            return None

        if minutes_to_kickoff is not None and minutes_to_kickoff <= 0 and current_status != "live":
            return "live"

        if (
            minutes_to_kickoff is not None
            and minutes_to_kickoff <= SOON_THRESHOLD_MINUTES
            and current_status == "upcoming"
        ):
            return "soon"

        return None

    def should_finalize_result(self, match: Dict[str, Any]) -> bool:
        """Determine if we should finalize the match result."""
        match_id = match.get("matchId")
        status = match.get("status", "")

        if status != "completed":
            return False

        if match_id in self.completed_notified:
            return False

        return True

    def mark_lineups_done(self, match_id: str):
        self.lineups_fetched.add(match_id)

    def mark_stats_started(self, match_id: str):
        self.stats_started.add(match_id)

    def mark_completed_notified(self, match_id: str):
        self.completed_notified.add(match_id)


class Poller:
    def __init__(self, store: FixtureStore, forwarder: Forwarder):
        self.store = store
        self.forwarder = forwarder
        self.state_machine = MatchStateMachine(store, forwarder)
        self.running = False
        self.poll_count = 0

        self._last_archive_check: float = 0.0

    def _format_timestamp(self, ts) -> str:
        """Format timestamp for Rust DateTime<Utc> - MUST include timezone and milliseconds"""
        if ts is None:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if isinstance(ts, datetime):
            return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if isinstance(ts, str):
            if '.' not in ts:
                ts = ts.replace('Z', '').replace('+00:00', '')
                ts = ts + ".000Z"
            if not ts.endswith('Z') and '+' not in ts:
                ts = ts + "Z"
            return ts
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def start(self):
        """Start polling loop."""
        self.running = True
        logger.info("🚀 Poller started. Checking all matches...")

        while self.running:
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f"Poll cycle failed: {e}", exc_info=True)

            self.poll_count += 1
            time.sleep(5)

    def poll_once(self):
        """Perform one poll cycle."""
        all_fixtures = self.store.get_all_fixtures()

        if not all_fixtures:
            logger.debug("No fixtures found")
            return

        logger.info(f"📊 Poll cycle #{self.poll_count}: Processing {len(all_fixtures)} fixtures")

        for match in all_fixtures:
            self._process_match(match)

        self._maybe_archive_completed()

    def _maybe_archive_completed(self):
        """Flip movedToHistory=True on completed fixtures older than ARCHIVE_AFTER_HOURS."""
        now = time.time()
        if now - self._last_archive_check < ARCHIVE_CHECK_INTERVAL_SECONDS:
            return

        self._last_archive_check = now
        try:
            archived = self.store.archive_completed_fixtures(hours=ARCHIVE_AFTER_HOURS)
            if archived:
                logger.info(f"🗄️ Archived {archived} completed fixture(s) to history")
        except Exception as e:
            logger.error(f"Failed to archive completed fixtures: {e}")

    def _process_match(self, match: Dict[str, Any]):
        """Process a single match based on its state."""
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")

        if not game_id:
            logger.warning(f"No 365Scores game_id for {match_id}, skipping")
            return

        # Calculate minutes until kickoff
        kickoff_utc = match.get("kickoffUtc")
        minutes_to_kickoff = None

        if kickoff_utc:
            if isinstance(kickoff_utc, str):
                try:
                    kickoff_utc = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
                except ValueError:
                    pass

            if isinstance(kickoff_utc, datetime):
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

            # Get current scores
            home_score = int(match.get("homeScore") or match.get("home_score") or 0)
            away_score = int(match.get("awayScore") or match.get("away_score") or 0)

            self.forwarder.forward_live_update(
                {
                    "fixture_id": match_id,
                    "event_type": "status_change",
                    "home_score": home_score,
                    "away_score": away_score,
                    "minute": 0,
                    "status": new_status,
                    "is_live": new_status == "live",
                    "available_for_voting": new_status in ["upcoming", "soon"],
                    "timestamp": self._format_timestamp(None),
                }
            )

            if new_status == "completed":
                self._finalize_match_result(match)
                return

            if new_status == "live":
                self._notify_match_live(match)

            match["status"] = new_status
            current_status = new_status

        # --- STEP 2: FETCH LINEUPS (if in soon state) ---
        if self.state_machine.should_fetch_lineups(match, current_status, minutes_to_kickoff):
            fetched = self._fetch_and_forward_lineups(match)
            if fetched:
                self.state_machine.mark_lineups_done(match_id)

        # --- STEP 3: FETCH STATISTICS (if live or soon near kickoff) ---
        if self.state_machine.should_fetch_statistics(match, current_status, minutes_to_kickoff):
            self._fetch_and_forward_statistics(match)

        # --- STEP 4: FETCH LIVE UPDATES AND COMMENTARY (if live) ---
        if current_status == "live":
            # Fetch game details once - use for both live updates AND commentary
            game_data = self._fetch_full_game_data(match)
            
            if game_data:
                # Process live updates (scores, status, completion)
                self._process_live_updates(match, game_data)
                
                # Process commentary from 365Scores (NOT Flashscore)
                self._process_commentary(match, game_data)
            else:
                # Fallback: fetch live updates separately if full data fails
                self._fetch_live_updates(match)

        # --- STEP 5: CHECK COMPLETION ---
        if self.state_machine.should_finalize_result(match):
            self._finalize_match_result(match)

        self.store.record_last_poll(match_id)

    def _fetch_full_game_data(self, match: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Fetch full game data from 365Scores once per poll cycle.
        Returns the game object containing all data (scores, commentary, stats).
        """
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")
        away_id = match.get("away_competitor_id")
        home_id = match.get("home_competitor_id")
        competition_id = match.get("competition_id", 5930)

        if not all([game_id, away_id, home_id]):
            return None

        details = threesixtyfive.fetch_game_details(
            game_id=game_id,
            away_id=away_id,
            home_id=home_id,
            competition_id=competition_id,
        )

        if not details or "game" not in details:
            return None

        return details.get("game", {})

    def _process_live_updates(self, match: Dict[str, Any], game: Dict[str, Any]):
        """
        Process live updates from 365Scores game data.
        Updates scores, checks completion, sends live updates.
        """
        match_id = match.get("matchId")
        
        home_comp = game.get("homeCompetitor", {})
        away_comp = game.get("awayCompetitor", {})

        # ENSURE ALL ARE INT
        raw_home_score = home_comp.get("score")
        raw_away_score = away_comp.get("score")
        home_score = int(raw_home_score) if raw_home_score is not None else 0
        away_score = int(raw_away_score) if raw_away_score is not None else 0

        if home_comp.get("score") is not None:
            self.store.update_score(match_id, home_score, away_score)
            logger.info(f"📊 {match_id}: Score updated {home_score}-{away_score}")

        # Check if game is finished using multiple methods
        is_finished = threesixtyfive.is_game_finished(game)
        
        # Fallback: time-based completion check
        if not is_finished:
            time_elapsed = game.get("gameTime", 0)
            status_text = game.get("statusText", "")
            
            if time_elapsed >= 90:
                if "Extra" not in status_text and "Half" not in status_text:
                    logger.info(f"⏰ {match_id}: timeElapsed {time_elapsed} >= 90, marking as finished")
                    is_finished = True

        if is_finished:
            logger.info(f"🏁 {match_id}: Game finished! Final score: {home_score}-{away_score}")
            self.store.update_status(match_id, "completed")
            
            # Send final live update
            live_update = {
                "fixture_id": match_id,
                "event_type": "match_end",
                "home_score": home_score,
                "away_score": away_score,
                "minute": game.get("gameTime", 90),
                "minute_display": "FT",
                "status": "completed",
                "is_live": False,
                "available_for_voting": False,
                "timestamp": self._format_timestamp(None),
            }
            self.forwarder.forward_live_update(live_update)
            
            self._finalize_match_result(match)
            return

        minute = int(game.get("gameTime", 0))

        # Send live update
        live_update = {
            "fixture_id": match_id,
            "event_type": "live_update",
            "home_score": home_score,
            "away_score": away_score,
            "minute": minute,
            "minute_display": f"{minute}'" if minute > 0 else "0'",
            "status": "live",
            "is_live": True,
            "available_for_voting": False,
            "timestamp": self._format_timestamp(None),
        }
        self.forwarder.forward_live_update(live_update)

    def _process_commentary(self, match: Dict[str, Any], game: Dict[str, Any]):
        """
        Process commentary from 365Scores game data.
        Formats entries to match Rust CommentaryEntry and forwards them.
        """
        match_id = match.get("matchId")
        
        # Get commentary from 365Scores game data
        raw_commentary = game.get("commentary", [])
        
        if not raw_commentary:
            logger.debug(f"No 365Scores commentary available for {match_id}")
            return

        logger.info(f"📝 Got {len(raw_commentary)} 365Scores commentary entries for {match_id}")

        for entry in raw_commentary:
            # Extract minute
            minute = entry.get("minute", 0)
            if isinstance(minute, str):
                try:
                    minute = int(minute)
                except (ValueError, TypeError):
                    minute = 0
            
            # Extract team and player names
            team = None
            if entry.get("team") and isinstance(entry.get("team"), dict):
                team = entry.get("team", {}).get("name")
            
            player = None
            if entry.get("player") and isinstance(entry.get("player"), dict):
                player = entry.get("player", {}).get("name")
            
            # Get event type
            event_type = entry.get("type", "commentary")
            
            # Format to match Rust CommentaryEntry EXACTLY
            formatted_entry = {
                "minute": int(minute),
                "text": str(entry.get("text", "")),
                "type": str(event_type),
                "team": team,
                "player": player,
                "createdAt": self._format_timestamp(None),
            }
            
            self.forwarder.forward_commentary(
                {
                    "match_id": match_id,
                    "entry": formatted_entry,
                }
            )

    def _fetch_live_updates(self, match: Dict[str, Any]):
        """
        Fallback: Fetch live updates separately if full data fetch fails.
        """
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")
        away_id = match.get("away_competitor_id")
        home_id = match.get("home_competitor_id")
        competition_id = match.get("competition_id", 5930)

        if not all([game_id, away_id, home_id]):
            return

        details = threesixtyfive.fetch_game_details(
            game_id=game_id,
            away_id=away_id,
            home_id=home_id,
            competition_id=competition_id,
        )

        if not details or "game" not in details:
            return

        game = details.get("game", {})
        
        # Process using the main method
        self._process_live_updates(match, game)
        self._process_commentary(match, game)

    def _fetch_and_forward_lineups(self, match: Dict[str, Any]) -> bool:
        """Fetch lineups and forward to Rust API."""
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")

        away_id = match.get("away_competitor_id")
        home_id = match.get("home_competitor_id")
        competition_id = match.get("competition_id", 5930)

        if not all([game_id, away_id, home_id]):
            logger.warning(f"Missing competitor IDs for {match_id}, cannot fetch lineups")
            return False

        logger.info(f"📋 Fetching lineups for {match_id}...")

        try:
            lineups = threesixtyfive.fetch_lineups(
                game_id=game_id,
                away_id=away_id,
                home_id=home_id,
                competition_id=competition_id,
            )

            if lineups:
                home_team = match.get("homeTeam", "Home")
                away_team = match.get("awayTeam", "Away")
                payload = _build_lineups_payload(match_id, home_team, away_team, lineups)

                success = self.forwarder.forward_lineups(payload)
                if success:
                    self.store.mark_lineups_fetched(match_id)
                    logger.info(f"✅ Lineups fetched and forwarded for {match_id}")
                    return True
                logger.warning(f"⚠️ Failed to forward lineups for {match_id}")
                return False
            logger.debug(f"No lineups available yet for {match_id}")
            return False
        except Exception as e:
            logger.error(f"❌ Failed to fetch lineups for {match_id}: {e}")
            return False

    def _fetch_and_forward_statistics(self, match: Dict[str, Any]):
        """Fetch statistics and forward to Rust API."""
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")
        away_id = match.get("away_competitor_id")
        home_id = match.get("home_competitor_id")
        competition_id = match.get("competition_id", 5930)

        if not all([game_id, away_id, home_id]):
            return

        stats = threesixtyfive.fetch_statistics(
            game_id=game_id,
            away_id=away_id,
            home_id=home_id,
            competition_id=competition_id,
        )

        if stats:
            minute = int(stats.get("minute", 0))
            stats_data = {
                "home": stats.get("home", {}),
                "away": stats.get("away", {}),
            }

            payload = {
                "fixture_id": match_id,
                "minute": minute,
                "statistics": stats_data,
            }
            self.forwarder.forward_statistics(payload)
            logger.debug(f"📊 Statistics forwarded for {match_id} at {minute}'")

    def _finalize_match_result(self, match: Dict[str, Any]):
        """
        Finalize match result and notify Rust API.
        Rust expects only fixture_id and result.
        """
        match_id = match.get("matchId")

        game = self.store.get_fixture(match_id)
        if not game:
            logger.warning(f"{match_id}: Cannot finalize - match not found")
            return

        home_score = game.get("homeScore") or game.get("home_score") or 0
        away_score = game.get("awayScore") or game.get("away_score") or 0

        if home_score > away_score:
            result = "home"
        elif away_score > home_score:
            result = "away"
        else:
            result = "draw"

        finalize_payload = {
            "fixture_id": match_id,
            "result": result,
        }

        success = self.forwarder.finalize_match(finalize_payload)
        if success:
            self.state_machine.mark_completed_notified(match_id)
            logger.info(f"🏁 Match {match_id} finalized: {result} ({home_score}-{away_score})")

    def _notify_match_live(self, match: Dict[str, Any]):
        """Send notification that match is now live."""
        match_id = match.get("matchId")
        home_team = match.get("homeTeam", "Home")
        away_team = match.get("awayTeam", "Away")

        notification = {
            "fixtureId": match_id,
            "eventType": "match_live",
            "title": f"⚽ {home_team} vs {away_team} is LIVE!",
            "body": "Match is now live. Follow the action!",
            "data": {
                "home_team": home_team,
                "away_team": away_team,
                "fixture_id": match_id,
                "type": "match_live",
            },
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