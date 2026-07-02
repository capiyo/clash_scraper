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
from datetime import datetime, timezone
from typing import Any, Dict, Optional

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
POLL_INTERVAL_LIVE = 15  # Every 15 seconds for live matches
POLL_INTERVAL_SOON = 60  # Every minute for soon matches
POLL_INTERVAL_UPCOMING = 300  # Every 5 minutes for upcoming matches

# Time thresholds (in minutes before kickoff)
SOON_THRESHOLD_MINUTES = 60  # 1 hour before kickoff = "soon"
LINEUP_EARLY_THRESHOLD = 60  # Start checking at 60 mins before
LINEUP_LATE_THRESHOLD = 40  # Stop checking at 40 mins before


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

    def should_forward_statistics(self, match_id: str, phase: Optional[str]) -> bool:
        """
        Determine if we should store/forward a statistics snapshot.

        Statistics are only fetched/forwarded at three moments in a match:
        halftime, stopped (suspended/interrupted play), and full-time.
        `phase` is the result of threesixtyfive.classify_match_phase() on
        the current statusText, computed from data we already fetched this
        cycle (no extra network call). We forward at most once per
        (match, phase) pair so a match sitting in "Half Time" for several
        poll cycles doesn't spam duplicate snapshots.
        """
        if phase not in ("halftime", "stopped", "fulltime"):
            return False
        return (match_id, phase) not in self.stats_started

    def mark_stats_forwarded(self, match_id: str, phase: str):
        self.stats_started.add((match_id, phase))

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
            self.forwarder.forward_live_update(
                {
                    "fixture_id": match_id,
                    "event_type": "status_change",
                    "status": new_status,
                    "is_live": new_status == "live",
                    "available_for_voting": new_status in ["upcoming", "soon"],
                    "minutes_to_kickoff": minutes_to_kickoff,
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
            self._fetch_and_forward_lineups(match)
            self.state_machine.mark_lineups_done(match_id)

        # --- STEP 3: FETCH LIVE UPDATES (if live) ---
        # Statistics are fetched/forwarded from inside _fetch_live_updates,
        # but ONLY at halftime, stopped/suspended play, and full-time --
        # not on every 15s live tick. See should_forward_statistics().
        if current_status == "live":
            self._fetch_live_updates(match)

        # --- STEP 5: FETCH COMMENTARY (365Scores, same source as
        # stats/lineups/live-updates -- live matches only) ---
        if current_status == "live":
            self._fetch_commentary(match)

        # --- STEP 6: CHECK COMPLETION ---
        if self.state_machine.should_finalize_result(match):
            self._finalize_match_result(match)

        self.store.record_last_poll(match_id)

    def _fetch_commentary(self, match: Dict[str, Any]):
        """
        Fetch commentary from 365Scores (same source/session as stats,
        lineups, and live updates -- no Flashscore ID resolution needed).

        365Scores' /web/game/ endpoint returns the FULL commentary
        history on every call, not just new entries, so we de-dup using
        the same forwardedEventSignatures set already used for goal/
        card/sub events. Signature-based dedup is ordering-agnostic,
        unlike slicing by a stored count, in case 365Scores ever changes
        whether the list is oldest-first or newest-first.
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

        # Oldest first so the commentary feed renders in the right order
        new_entries.sort(key=lambda e: e.get("minute", 0))

        logger.info(f"📝 {match_id}: {len(new_entries)} new commentary entries")

        self.forwarder.forward_commentary_bulk(match_id, new_entries)
        self.store.add_commentary_bulk(match_id, new_entries)
        self.store.add_forwarded_event_signatures_bulk(match_id, new_signatures)

    @staticmethod
    def _team_lineup_for_forwarder(team_lineup: Dict[str, Any]) -> Dict[str, Any]:
        """
        threesixtyfive.fetch_lineups() returns each side as
        {"formation", "status", "members": [...]}. forwarder.py's
        clean_team() reads "players" (not "members") and has no concept
        of a separate "bench" list from 365Scores -- so map members
        straight into "players" here. If a given member dict carries a
        365Scores field indicating starting vs. bench (check the raw
        payload -- e.g. a "lineup"/"isStarting" style flag), split it out
        into "bench" here too; left as a single "players" list for now
        since fetch_lineups()'s own docstring doesn't document that field.
        """
        if not team_lineup:
            return {}
        return {
            "formation": team_lineup.get("formation"),
            "players": team_lineup.get("members", []),
        }

    def _fetch_and_forward_lineups(self, match: Dict[str, Any]):
        """Fetch lineups and forward to Rust API using the new /web/game/ endpoint."""
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")

        # Get competitor IDs from match data (stored during scraping)
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
                # threesixtyfive.fetch_lineups() returns
                # {"fixture_id", "home", "away"} flat -- forwarder.py's
                # forward_lineups() expects "fixture_id", "home_team",
                # "away_team", and a nested "lineups": {home, away}, each
                # with a "players" key (not "members"). Reshape here
                # rather than touching forwarder.py.
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

                # Store in MongoDB (store the shaped lineups so what's in
                # Mongo matches what was actually forwarded)
                self.store.store_lineups(match_id, lineups_shaped)

                # Forward to Rust API
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
        """
        Store + forward a statistics snapshot.

        Only ever called at halftime, during a stoppage, or at full-time
        (see should_forward_statistics() / classify_match_phase()) -- not
        on every live tick. If `game` (from this cycle's already-fetched
        fetch_game_details() response) is supplied, it's reused directly
        instead of making a second network request.
        """
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
            minute = stats.get("minute", 0)
            team_stats = {"home": stats.get("home", {}), "away": stats.get("away", {})}

            self.store.add_statistics_snapshot(match_id, team_stats, minute)

            # threesixtyfive.fetch_statistics() returns home/away/minute
            # flat -- forwarder.py's forward_statistics() requires a
            # top-level "fixture_id" plus a nested "statistics": {home,
            # away}, or it silently logs "Missing fixture_id" and returns
            # False. Wrap it into that shape here.
            self.forwarder.forward_statistics({
                "fixture_id": match_id,
                "minute": minute,
                "statistics": team_stats,
            })
            logger.debug(f"📊 Statistics forwarded for {match_id} at {minute}'")

    def _fetch_live_updates(self, match: Dict[str, Any]):
        """Fetch live updates (scores, events, commentary)."""
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
        home_comp = game.get("homeCompetitor", {})
        away_comp = game.get("awayCompetitor", {})

        # Update scores
        home_score = home_comp.get("score")
        away_score = away_comp.get("score")

        # 365Scores uses -1 as a sentinel for "no score yet" (pre-match),
        # not None. Treating -1 as a real score writes garbage into Mongo
        # and logs a fake "score updated" event for matches that haven't
        # actually started scoring yet.
        has_real_score = (
            home_score is not None
            and away_score is not None
            and home_score >= 0
            and away_score >= 0
        )

        if has_real_score:
            self.store.update_score(match_id, home_score, away_score)
            logger.info(f"📊 {match_id}: Score updated {home_score}-{away_score}")
        else:
            logger.debug(
                f"{match_id}: Ignoring placeholder score "
                f"({home_score}-{away_score}), match hasn't started scoring yet"
            )

        # Classify the match phase from statusText, and fetch/forward
        # statistics ONLY at halftime, during a stoppage, or at full-time --
        # never on a normal in-play tick. We reuse the `game` object from
        # the fetch_game_details() call above, so this costs no extra
        # network request.
        raw_status_text = game.get("statusText")
        phase = threesixtyfive.classify_match_phase(raw_status_text)
        if self.state_machine.should_forward_statistics(match_id, phase):
            logger.info(f"📊 {match_id}: phase={phase} ({raw_status_text!r}) - fetching statistics")
            self._fetch_and_forward_statistics(match, game=game)
            self.state_machine.mark_stats_forwarded(match_id, phase)

        # Check if match ended
        if phase == "fulltime":
            self.store.update_status(match_id, "completed")
            self._finalize_match_result(match)
            return

        # Forward live update -- only send real scores downstream, never -1
        live_update = {
            "fixture_id": match_id,
            "event_type": "live_update",
            "home_score": home_score if has_real_score else 0,
            "away_score": away_score if has_real_score else 0,
            "minute": game.get("gameTime", 0),
            "status": "live",
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

        finalize_payload = {
            "fixture_id": match_id,
            "result": result,
            "home_score": home_score,
            "away_score": away_score,
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