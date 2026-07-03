"""
Live poller for World Cup matches with full state machine and triple-verification.
Handles: upcoming → soon → live → completed → archived
Fetches lineups when matches are in "soon" state (40-60 mins before kickoff)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

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

# Polling intervals (in seconds)
POLL_INTERVAL_LIVE = 15      # Every 15 seconds for live matches
POLL_INTERVAL_SOON = 60      # Every minute for soon matches
POLL_INTERVAL_UPCOMING = 300 # Every 5 minutes for upcoming matches

# Time thresholds (in minutes before kickoff)
SOON_THRESHOLD_MINUTES = 60   # 1 hour before kickoff = "soon"
LINEUP_EARLY_THRESHOLD = 60   # Start checking at 60 mins before
LINEUP_LATE_THRESHOLD = 40    # Stop checking at 40 mins before

# 365Scores status groups
STATUS_GROUP_SCHEDULED = 2
STATUS_GROUP_LIVE = 3
STATUS_GROUP_FINISHED = 4


class MatchStateMachine:
    """
    Manages match state transitions and determines what data to fetch.

    States:
      upcoming: > 60 mins before kickoff - just basic info
      soon: 10-60 mins before kickoff - fetch lineups, start stats
      live: kickoff to final whistle - fetch scores, events, stats
      completed: match ended - fetch final result, move to history
    """

    def __init__(self, store: FixtureStore, forwarder: Forwarder):
        self.store = store
        self.forwarder = forwarder
        self.lineups_fetched = set()
        self.stats_started = set()
        self.completed_notified = set()
        self._status_correction_logged = set()  # Prevent log spam

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

        # ✅ DOUBLE-CHECK: If 365Scores says it's scheduled, force "soon" or "upcoming"
        # This is the critical fix — we verify with the source of truth
        if status == "live":
            game_id = match.get("threesixtyfiveGameId")
            if game_id:
                try:
                    details = threesixtyfive.fetch_game_details(
                        game_id=game_id,
                        away_id=match.get("away_competitor_id"),
                        home_id=match.get("home_competitor_id"),
                        competition_id=match.get("competition_id", 5930),
                    )
                    if details and details.get("game", {}).get("statusGroup") == STATUS_GROUP_SCHEDULED:
                        # 365Scores says it's NOT live
                        if minutes_until_kickoff > 0:
                            logger.warning(
                                f"⚠️ {match.get('matchId')}: 365Scores says scheduled but DB says live — "
                                f"forcing 'soon' ({minutes_until_kickoff:.0f} mins to kickoff)"
                            )
                            return "soon"
                except Exception as e:
                    logger.debug(f"Could not verify live status with 365Scores: {e}")

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

        if match_id in self.lineups_fetched:
            return False

        if state == "completed":
            return False

        # If match is already live and we missed lineups, fetch them
        if state == "live":
            logger.info(f"📋 {match_id}: Live but no lineups - fetching now")
            return True

        # Normal flow: fetch in "soon" state within the window
        if state == "soon" and minutes_to_kickoff is not None:
            should_fetch = LINEUP_LATE_THRESHOLD <= minutes_to_kickoff <= LINEUP_EARLY_THRESHOLD
            if should_fetch:
                logger.info(
                    f"📋 {match_id}: {minutes_to_kickoff:.0f} mins to kickoff - fetching lineups"
                )
            return should_fetch

        return False

    def should_forward_statistics(self, match_id: str, phase: Optional[str]) -> bool:
        """
        Determine if we should store/forward a statistics snapshot.

        Statistics are only fetched/forwarded at three moments in a match:
        halftime, stopped (suspended/interrupted play), and full-time.
        """
        if phase not in ("halftime", "stopped", "fulltime"):
            return False
        return (match_id, phase) not in self.stats_started

    def mark_stats_forwarded(self, match_id: str, phase: str):
        self.stats_started.add((match_id, phase))

    def should_update_status(self, match: Dict[str, Any]) -> Optional[str]:
        """
        Determine if match status should be updated.
        ✅ UPDATED: Now checks the state machine's computed state first.
        """
        current_status = match.get("status", "upcoming")
        minutes_to_kickoff = match.get("minutes_to_kickoff")

        if current_status == "completed":
            return None

        # ✅ FIX: If the match is already "live" but minutes_to_kickoff > 0,
        # we need to correct it
        if current_status == "live" and minutes_to_kickoff is not None and minutes_to_kickoff > 0:
            if minutes_to_kickoff <= SOON_THRESHOLD_MINUTES:
                return "soon"
            else:
                return "upcoming"

        if minutes_to_kickoff is not None and minutes_to_kickoff <= 0 and current_status != "live":
            return "live"

        if (
            minutes_to_kickoff is not None
            and minutes_to_kickoff <= SOON_THRESHOLD_MINUTES
            and current_status == "upcoming"
        ):
            return "soon"

        return None

    def mark_lineups_done(self, match_id: str):
        self.lineups_fetched.add(match_id)

    def mark_completed_notified(self, match_id: str):
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

    def _verify_live_status_with_365scores(self, match: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        ✅ NEW: Triple-verification — check with 365Scores if the match is actually live.
        Returns (is_actually_live, status_text_or_phase)
        """
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")
        away_id = match.get("away_competitor_id")
        home_id = match.get("home_competitor_id")
        competition_id = match.get("competition_id", 5930)

        if not all([game_id, away_id, home_id]):
            logger.debug(f"{match_id}: Missing IDs, cannot verify with 365Scores")
            return False, None

        try:
            details = threesixtyfive.fetch_game_details(
                game_id=game_id,
                away_id=away_id,
                home_id=home_id,
                competition_id=competition_id,
            )
        except Exception as e:
            logger.error(f"❌ Failed to verify live status for {match_id}: {e}")
            return False, None

        if not details or "game" not in details:
            return False, None

        game = details.get("game", {})
        status_group = game.get("statusGroup")
        status_text = game.get("statusText", "")

        # 365Scores status groups:
        # 2 = Scheduled (not started)
        # 3 = Live (in progress)
        # 4 = Finished
        if status_group == STATUS_GROUP_LIVE:
            logger.debug(f"{match_id}: 365Scores confirms LIVE (statusGroup=3, statusText='{status_text}')")
            return True, status_text
        elif status_group == STATUS_GROUP_SCHEDULED:
            logger.debug(f"{match_id}: 365Scores says SCHEDULED (statusGroup=2, statusText='{status_text}')")
            return False, status_text
        elif status_group == STATUS_GROUP_FINISHED:
            logger.debug(f"{match_id}: 365Scores says FINISHED (statusGroup=4)")
            return False, "fulltime"
        else:
            # Unknown status group — check statusText for live markers
            live_markers = ("1st half", "2nd half", "first half", "second half", "halftime", "ht", "live")
            if any(marker in status_text.lower() for marker in live_markers):
                logger.debug(f"{match_id}: 365Scores statusText suggests LIVE: '{status_text}'")
                return True, status_text
            else:
                logger.debug(f"{match_id}: 365Scores statusText unknown: '{status_text}'")
                return False, status_text

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

        # ──────────────────────────────────────────────────────────────────────
        # ✅ STEP 0: TRIPLE-VERIFICATION — Check with 365Scores for truth
        # ──────────────────────────────────────────────────────────────────────
        is_actually_live, status_text = self._verify_live_status_with_365scores(match)

        # If 365Scores says the match is NOT live, correct the status
        if not is_actually_live and match.get("status") == "live":
            if minutes_to_kickoff is not None and minutes_to_kickoff > 0:
                new_status = "soon" if minutes_to_kickoff <= SOON_THRESHOLD_MINUTES else "upcoming"
                logger.warning(
                    f"⚠️ {match_id}: 365Scores says NOT LIVE (statusText='{status_text}') — "
                    f"CORRECTING 'live' → '{new_status}' ({minutes_to_kickoff:.0f} mins to kickoff)"
                )
                self.store.update_status(match_id, new_status)
                self.forwarder.forward_live_update({
                    "fixture_id": match_id,
                    "event_type": "status_change",
                    "status": new_status,
                    "is_live": False,
                    "available_for_voting": new_status in ("upcoming", "soon"),
                    "minutes_to_kickoff": minutes_to_kickoff,
                })
                match["status"] = new_status
                # If we corrected to "soon", still fetch lineups
                if new_status == "soon":
                    self._fetch_and_forward_lineups(match)
                return
            else:
                # minutes_to_kickoff is None or <= 0, but 365Scores says not live
                # Force "soon" as a safety net
                logger.warning(
                    f"⚠️ {match_id}: 365Scores says NOT LIVE but minutes_to_kickoff={minutes_to_kickoff} — "
                    f"FORCING 'soon' as safety net"
                )
                self.store.update_status(match_id, "soon")
                self.forwarder.forward_live_update({
                    "fixture_id": match_id,
                    "event_type": "status_change",
                    "status": "soon",
                    "is_live": False,
                    "available_for_voting": True,
                })
                match["status"] = "soon"
                self._fetch_and_forward_lineups(match)
                return

        # ──────────────────────────────────────────────────────────────────────
        # STEP 1: Determine state (with 365Scores verification baked in)
        # ──────────────────────────────────────────────────────────────────────
        state = self.state_machine.determine_state(match)
        current_status = match.get("status", "upcoming")

        # ──────────────────────────────────────────────────────────────────────
        # STEP 2: UPDATE STATUS IF NEEDED (with safety checks)
        # ──────────────────────────────────────────────────────────────────────
        new_status = self.state_machine.should_update_status(match)

        # ✅ SAFETY: If state machine says "soon" but DB says "live", force correction
        if state == "soon" and current_status == "live":
            if minutes_to_kickoff is not None and minutes_to_kickoff > 0:
                logger.warning(
                    f"⚠️ {match_id}: State machine says 'soon' but DB says 'live' — "
                    f"CORRECTING ({minutes_to_kickoff:.0f} mins to kickoff)"
                )
                self.store.update_status(match_id, "soon")
                self.forwarder.forward_live_update({
                    "fixture_id": match_id,
                    "event_type": "status_change",
                    "status": "soon",
                    "is_live": False,
                    "available_for_voting": True,
                    "minutes_to_kickoff": minutes_to_kickoff,
                })
                match["status"] = "soon"
                current_status = "soon"
                new_status = None  # Prevent further status updates this cycle

        # ✅ SAFETY: If state machine says "upcoming" but DB says "soon" or "live"
        if state == "upcoming" and current_status in ("soon", "live"):
            if minutes_to_kickoff is not None and minutes_to_kickoff > SOON_THRESHOLD_MINUTES:
                logger.warning(
                    f"⚠️ {match_id}: State machine says 'upcoming' but DB says '{current_status}' — "
                    f"CORRECTING ({minutes_to_kickoff:.0f} mins to kickoff)"
                )
                self.store.update_status(match_id, "upcoming")
                self.forwarder.forward_live_update({
                    "fixture_id": match_id,
                    "event_type": "status_change",
                    "status": "upcoming",
                    "is_live": False,
                    "available_for_voting": True,
                    "minutes_to_kickoff": minutes_to_kickoff,
                })
                match["status"] = "upcoming"
                current_status = "upcoming"
                new_status = None

        # Normal status transition
        if new_status and new_status != current_status:
            # ✅ SAFETY: Double-check with 365Scores before transitioning to "live"
            if new_status == "live":
                is_actually_live, _ = self._verify_live_status_with_365scores(match)
                if not is_actually_live:
                    logger.warning(
                        f"⚠️ {match_id}: Refusing to transition to 'live' — 365Scores says NOT LIVE"
                    )
                    new_status = "soon"
                    self.store.update_status(match_id, "soon")
                    self.forwarder.forward_live_update({
                        "fixture_id": match_id,
                        "event_type": "status_change",
                        "status": "soon",
                        "is_live": False,
                        "available_for_voting": True,
                        "minutes_to_kickoff": minutes_to_kickoff,
                    })
                    match["status"] = "soon"
                    current_status = "soon"
                    # Still fetch lineups
                    self._fetch_and_forward_lineups(match)
                    return

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
            match["status"] = new_status
            current_status = new_status

            if new_status == "completed":
                self._finalize_match_result(match)
                return
            if new_status == "live":
                self._notify_match_live(match)

        # ──────────────────────────────────────────────────────────────────────
        # STEP 3: FETCH LINEUPS (if in soon state)
        # ──────────────────────────────────────────────────────────────────────
        if self.state_machine.should_fetch_lineups(match, current_status, minutes_to_kickoff):
            self._fetch_and_forward_lineups(match)
            self.state_machine.mark_lineups_done(match_id)

        # ──────────────────────────────────────────────────────────────────────
        # STEP 4: FETCH LIVE UPDATES (if live — with verification)
        # ──────────────────────────────────────────────────────────────────────
        if current_status == "live":
            # ✅ SAFETY: Verify with 365Scores AGAIN before sending live_update
            is_actually_live, phase = self._verify_live_status_with_365scores(match)
            if not is_actually_live:
                # 365Scores says not live — correct and return
                if minutes_to_kickoff is not None and minutes_to_kickoff > 0:
                    new_status = "soon" if minutes_to_kickoff <= SOON_THRESHOLD_MINUTES else "upcoming"
                    logger.warning(
                        f"⚠️ {match_id}: About to send live_update but 365Scores says NOT LIVE — "
                        f"CORRECTING to '{new_status}'"
                    )
                    self.store.update_status(match_id, new_status)
                    self.forwarder.forward_live_update({
                        "fixture_id": match_id,
                        "event_type": "status_change",
                        "status": new_status,
                        "is_live": False,
                        "available_for_voting": new_status in ("upcoming", "soon"),
                        "minutes_to_kickoff": minutes_to_kickoff,
                    })
                    match["status"] = new_status
                    return
                else:
                    # Unknown situation — skip live_update this cycle
                    logger.warning(
                        f"⚠️ {match_id}: Skipping live_update — 365Scores says NOT LIVE "
                        f"(phase='{phase}', minutes_to_kickoff={minutes_to_kickoff})"
                    )
                    return

            self._fetch_live_updates(match)

        # ──────────────────────────────────────────────────────────────────────
        # STEP 5: FETCH COMMENTARY (live matches only)
        # ──────────────────────────────────────────────────────────────────────
        if current_status == "live":
            self._fetch_commentary(match)

        # ──────────────────────────────────────────────────────────────────────
        # STEP 6: CHECK COMPLETION
        # ──────────────────────────────────────────────────────────────────────
        if self.state_machine.should_finalize_result(match):
            self._finalize_match_result(match)

        self.store.record_last_poll(match_id)

    def _fetch_commentary(self, match: Dict[str, Any]):
        """
        Fetch commentary from 365Scores.
        """
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")
        away_id = match.get("away_competitor_id")
        home_id = match.get("home_competitor_id")
        competition_id = match.get("competition_id", 5930)

        if not all([game_id, away_id, home_id]):
            return

        try:
            commentary = threesixtyfive.fetch_commentary(
                game_id=game_id,
                away_id=away_id,
                home_id=home_id,
                competition_id=competition_id,
            )
        except Exception as e:
            logger.error(f"❌ Failed to fetch commentary for {match_id}: {e}")
            return

        if not commentary:
            logger.debug(f"No commentary available for {match_id}")
            return

        already_forwarded = self.store.get_forwarded_event_signatures(match_id)

        new_entries = []
        new_signatures = []
        for entry in commentary:
            sig = f"commentary:{entry.get('minute', 0)}:{entry.get('text', '')[:80]}"
            if sig in already_forwarded:
                continue
            new_entries.append(entry)
            new_signatures.append(sig)

        if not new_entries:
            return

        new_entries.sort(key=lambda e: e.get("minute", 0))

        logger.info(f"📝 {match_id}: {len(new_entries)} new commentary entries")

        self.forwarder.forward_commentary_bulk(match_id, new_entries)
        self.store.add_commentary_bulk(match_id, new_entries)
        self.store.add_forwarded_event_signatures_bulk(match_id, new_signatures)

    @staticmethod
    def _team_lineup_for_forwarder(team_lineup: Dict[str, Any]) -> Dict[str, Any]:
        """Convert 365Scores lineup format to forwarder format."""
        if not team_lineup:
            return {}
        return {
            "formation": team_lineup.get("formation"),
            "players": team_lineup.get("members", []),
        }

    def _fetch_and_forward_lineups(self, match: Dict[str, Any]):
        """Fetch lineups and forward to Rust API."""
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")
        away_id = match.get("away_competitor_id")
        home_id = match.get("home_competitor_id")
        competition_id = match.get("competition_id", 5930)
        home_team = match.get("homeTeam")
        away_team = match.get("awayTeam")

        if not all([game_id, away_id, home_id]):
            logger.warning(f"Missing competitor IDs for {match_id}, cannot fetch lineups")
            return

        logger.info(f"📋 Fetching lineups for {match_id}...")

        try:
            lineups = threesixtyfive.fetch_lineups(
                game_id=game_id,
                away_id=away_id,
                home_id=home_id,
                competition_id=competition_id,
            )

            if lineups:
                lineups_shaped = {
                    "home": self._team_lineup_for_forwarder(lineups.get("home", {})),
                    "away": self._team_lineup_for_forwarder(lineups.get("away", {})),
                }
                lineups_payload = {
                    "fixture_id": match_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "lineups": lineups_shaped,
                }

                success = self.forwarder.forward_lineups(lineups_payload)
                if success:
                    self.store.mark_lineups_fetched(match_id)
                    logger.info(f"✅ Lineups fetched and forwarded for {match_id}")
                else:
                    logger.warning(f"⚠️ Failed to forward lineups for {match_id}")
            else:
                logger.debug(f"No lineups available yet for {match_id}")

        except Exception as e:
            logger.error(f"❌ Failed to fetch lineups for {match_id}: {e}")

    def _fetch_and_forward_statistics(
        self,
        match: Dict[str, Any],
        game: Optional[Dict[str, Any]] = None,
    ):
        """Store + forward a statistics snapshot."""
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")
        away_id = match.get("away_competitor_id")
        home_id = match.get("home_competitor_id")
        competition_id = match.get("competition_id", 5930)

        if game is not None:
            stats = threesixtyfive.extract_statistics_from_game(game)
        else:
            if not all([game_id, away_id, home_id]):
                return
            stats = threesixtyfive.fetch_statistics(
                game_id=game_id,
                away_id=away_id,
                home_id=home_id,
                competition_id=competition_id,
            )

        if stats:
            minute = int(stats.get("minute", 0) or 0)
            team_stats = {"home": stats.get("home", {}), "away": stats.get("away", {})}

            self.store.add_statistics_snapshot(match_id, team_stats, minute)

            self.forwarder.forward_statistics({
                "fixture_id": match_id,
                "minute": minute,
                "statistics": team_stats,
            })
            logger.debug(f"📊 Statistics forwarded for {match_id} at {minute}'")

    def _fetch_live_updates(self, match: Dict[str, Any]):
        """
        Fetch live updates (scores, events, commentary).
        ✅ UPDATED: Contains its OWN verification before sending live_update.
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
        status_group = game.get("statusGroup")
        status_text = game.get("statusText", "")

        # ✅ SAFETY CHECK: Verify the match is ACTUALLY live
        # 365Scores statusGroup: 2 = scheduled, 3 = live, 4 = finished
        if status_group == STATUS_GROUP_SCHEDULED:
            minutes_remaining = match.get("minutes_to_kickoff")
            if minutes_remaining is not None and minutes_remaining > 0:
                logger.warning(
                    f"⚠️ {match_id}: 365Scores says 'scheduled' (statusGroup=2) but in live_update — "
                    f"CORRECTING to 'soon' ({minutes_remaining:.0f} mins to kickoff)"
                )
                self.store.update_status(match_id, "soon")
                self.forwarder.forward_live_update({
                    "fixture_id": match_id,
                    "event_type": "status_change",
                    "status": "soon",
                    "is_live": False,
                    "available_for_voting": True,
                    "minutes_to_kickoff": minutes_remaining,
                })
                match["status"] = "soon"
                return
            else:
                logger.warning(
                    f"⚠️ {match_id}: 365Scores says 'scheduled' (statusGroup=2) — "
                    f"SKIPPING live_update"
                )
                return

        if status_group == STATUS_GROUP_FINISHED:
            logger.info(f"🏁 {match_id}: 365Scores says FINISHED — marking completed")
            self.store.update_status(match_id, "completed")
            self._finalize_match_result(match)
            return

        # If status_group is None or unknown, check statusText
        if status_group is None:
            live_markers = ("1st half", "2nd half", "first half", "second half", "halftime", "ht", "live")
            if not any(marker in status_text.lower() for marker in live_markers):
                minutes_remaining = match.get("minutes_to_kickoff")
                if minutes_remaining is not None and minutes_remaining > 5:
                    logger.warning(
                        f"⚠️ {match_id}: statusText='{status_text}' doesn't indicate live — "
                        f"SKIPPING live_update"
                    )
                    return

        # ✅ If we made it here, the match is actually live
        logger.info(f"🔴 {match_id}: Verified ACTUALLY live (statusGroup={status_group}, statusText='{status_text}')")

        home_comp = game.get("homeCompetitor", {})
        away_comp = game.get("awayCompetitor", {})

        # Update scores
        home_score = home_comp.get("score")
        away_score = away_comp.get("score")

        has_real_score = (
            home_score is not None
            and away_score is not None
            and home_score >= 0
            and away_score >= 0
        )

        if has_real_score:
            self.store.update_score(match_id, home_score, away_score)
            logger.info(f"📊 {match_id}: Score updated {home_score}-{away_score}")

        # Classify the match phase from statusText
        phase = threesixtyfive.classify_match_phase(status_text)
        if self.state_machine.should_forward_statistics(match_id, phase):
            logger.info(f"📊 {match_id}: phase={phase} ({status_text!r}) - fetching statistics")
            self._fetch_and_forward_statistics(match, game=game)
            self.state_machine.mark_stats_forwarded(match_id, phase)

        # Check if match ended
        if phase == "fulltime" or status_group == STATUS_GROUP_FINISHED:
            logger.info(f"🏁 {match_id}: Match ended — marking completed")
            self.store.update_status(match_id, "completed")
            self._finalize_match_result(match)
            return

        # ✅ Only send live_update if we've verified it's actually live
        live_update = {
            "fixture_id": match_id,
            "event_type": "live_update",
            "home_score": home_score if has_real_score else 0,
            "away_score": away_score if has_real_score else 0,
            "minute": int(game.get("gameTime", 0) or 0),
            "status": "live",
            "is_live": True,
            "available_for_voting": False,
        }
        self.forwarder.forward_live_update(live_update)

    def _finalize_match_result(self, match: Dict[str, Any]):
        """Finalize match result and notify Rust API."""
        match_id = match.get("matchId")
        game = self.store.get_fixture(match_id)

        if not game:
            logger.warning(f"{match_id}: Cannot finalize - match not found")
            return

        home_score = game.get("homeScore", 0)
        away_score = game.get("awayScore", 0)

        if home_score > away_score:
            result = "home"
        elif away_score > home_score:
            result = "away"
        else:
            result = "draw"

        # Use move_to_history instead of deprecated finalize_match
        success = self.forwarder.move_to_history(match_id)
        if success:
            self.state_machine.mark_completed_notified(match_id)
            logger.info(f"🏁 Match {match_id} finalized: {result} ({home_score}-{away_score})")

    def _notify_match_live(self, match: Dict[str, Any]):
        """Send notification that match is now live."""
        match_id = match.get("matchId")
        home_team = match.get("homeTeam", "Home")
        away_team = match.get("awayTeam", "Away")

        notification = {
            "fixture_id": match_id,
            "event_type": "match_live",
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