"""
Live poller for World Cup matches with smart state management.
Handles: upcoming → soon → live → completed → archived
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv

from forwarder import Forwarder
from mongo_store import FixtureStore
from sources import threesixtyfive
import scraper

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worldcup_poller.poller")

# Polling intervals (in seconds)
POLL_INTERVAL_LIVE = 15
POLL_INTERVAL_SOON = 30
POLL_INTERVAL_UPCOMING = 300

# Time thresholds (in minutes before kickoff)
SOON_THRESHOLD_MINUTES = 60
LINEUP_EARLY_THRESHOLD = 60
LINEUP_LATE_THRESHOLD = 40

# 365Scores status groups
STATUS_GROUP_SCHEDULED = 2
STATUS_GROUP_LIVE = 3
STATUS_GROUP_FINISHED = 4


class MatchStateMachine:
    def __init__(self, store: FixtureStore, forwarder: Forwarder):
        self.store = store
        self.forwarder = forwarder
        self.lineups_fetched = set()
        self.stats_started = set()
        self.completed_notified = set()
        self.settlement_retries = {}  # match_id -> attempt_count
        self.max_settlement_retries = 3

    def determine_state(self, match: Dict[str, Any]) -> str:
        """Determine the current state based on kickoff time."""
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

        if minutes_until_kickoff <= 0:
            return "live"
        if minutes_until_kickoff <= SOON_THRESHOLD_MINUTES:
            return "soon"
        return "upcoming"

    def should_update_status(self, match: Dict[str, Any]) -> Optional[str]:
        """Determine if status should be updated. Smart correction included."""
        current_status = match.get("status", "upcoming")
        minutes_to_kickoff = match.get("minutes_to_kickoff")
        state = match.get("_state")

        if current_status == "completed":
            return None

        if state == "live" and current_status != "live":
            if minutes_to_kickoff is not None and minutes_to_kickoff <= 0:
                logger.info(f"⏰ Forcing 'live': state=live, status={current_status}")
                return "live"

        if state == "soon" and current_status == "live":
            if minutes_to_kickoff is not None and minutes_to_kickoff > 0:
                logger.info(f"🔄 Correcting 'live' → 'soon' ({minutes_to_kickoff:.0f} mins to kickoff)")
                return "soon"

        if state == "upcoming" and current_status in ("soon", "live"):
            if minutes_to_kickoff is not None and minutes_to_kickoff > SOON_THRESHOLD_MINUTES:
                logger.info(f"🔄 Correcting '{current_status}' → 'upcoming' ({minutes_to_kickoff:.0f} mins to kickoff)")
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

    def should_fetch_lineups(
        self,
        match: Dict[str, Any],
        state: str,
        minutes_to_kickoff: Optional[float] = None,
    ) -> bool:
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
                logger.info(f"📋 {match_id}: {minutes_to_kickoff:.0f} mins to kickoff - fetching lineups")
            return should_fetch

        return False

    def should_forward_statistics(self, match_id: str, phase: Optional[str]) -> bool:
        if phase not in ("halftime", "stopped", "fulltime"):
            return False
        return (match_id, phase) not in self.stats_started

    def mark_stats_forwarded(self, match_id: str, phase: str):
        self.stats_started.add((match_id, phase))

    def should_finalize_result(self, match: Dict[str, Any]) -> bool:
        match_id = match.get("matchId")
        status = match.get("status", "")

        if status != "completed":
            return False

        if match_id in self.completed_notified:
            return False

        return True

    def should_retry_settlement(self, match_id: str) -> bool:
        attempts = self.settlement_retries.get(match_id, 0)
        return attempts < self.max_settlement_retries

    def record_settlement_attempt(self, match_id: str):
        self.settlement_retries[match_id] = self.settlement_retries.get(match_id, 0) + 1

    def mark_settlement_success(self, match_id: str):
        self.settlement_retries.pop(match_id, None)

    def mark_lineups_done(self, match_id: str):
        self.lineups_fetched.add(match_id)

    def mark_completed_notified(self, match_id: str):
        self.completed_notified.add(match_id)


class Poller:
    # How often the scheduled (non-reactive) rescrape runs, regardless of
    # whether any match has completed. This is a *backstop* for the reactive
    # trigger in _trigger_rescrape (called right after a match is archived)
    # -- it exists purely to cover cases where a match never cleanly
    # finalizes (stuck state, crashed poll cycle, etc.) and the reactive
    # path never fires, so `fixtures` could otherwise silently starve.
    # Deliberately NOT a cron job / separate Render service -- this is a
    # plain elapsed-time check inside the existing poll loop, so the
    # scraper still only actually runs once or twice a day, using the
    # single already-running process.
    SCHEDULED_RESCRAPE_INTERVAL = timedelta(hours=12)  # twice a day

    def __init__(self, store: FixtureStore, forwarder: Forwarder):
        self.store = store
        self.forwarder = forwarder
        self.state_machine = MatchStateMachine(store, forwarder)
        self.running = False
        self.poll_count = 0
        # Seeded to "already due" so the very first poll cycle after startup
        # also performs a scrape -- covers the case where the service was
        # just deployed/restarted and `fixtures` is empty.
        self.last_scheduled_scrape = datetime.now(timezone.utc) - self.SCHEDULED_RESCRAPE_INTERVAL

    def start(self):
        self.running = True
        logger.info("🚀 Poller started. Checking all matches...")

        while self.running:
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f"Poll cycle failed: {e}", exc_info=True)
            self.poll_count += 1
            time.sleep(3)

    def _maybe_scheduled_rescrape(self):
        """Backstop rescrape, independent of match completion. Runs at most
        once per SCHEDULED_RESCRAPE_INTERVAL (twice a day) -- checked on
        every poll cycle, but only actually triggers a scrape when the
        interval has elapsed, so this does not turn into a de facto cron
        job running every few seconds."""
        now = datetime.now(timezone.utc)
        if now - self.last_scheduled_scrape >= self.SCHEDULED_RESCRAPE_INTERVAL:
            self.last_scheduled_scrape = now
            self._trigger_rescrape(reason="scheduled twice-daily backstop")

    def poll_once(self):
        self._maybe_scheduled_rescrape()

        all_fixtures = self.store.get_all_fixtures()

        if not all_fixtures:
            logger.debug("No fixtures found")
            return

        logger.info(f"📊 Poll cycle #{self.poll_count}: Processing {len(all_fixtures)} fixtures")

        for match in all_fixtures:
            try:
                self._process_match(match)
            except Exception as e:
                logger.error(f"Error processing match {match.get('matchId')}: {e}", exc_info=True)

    def _verify_live_status_with_365scores(self, match: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")
        away_id = match.get("away_competitor_id")
        home_id = match.get("home_competitor_id")
        competition_id = match.get("competition_id", 5930)

        if not all([game_id, away_id, home_id]):
            return False, None

        try:
            details = threesixtyfive.fetch_game_details(
                game_id=game_id,
                away_id=away_id,
                home_id=home_id,
                competition_id=competition_id,
            )
        except Exception as e:
            logger.debug(f"Could not verify live status for {match_id}: {e}")
            return False, None

        if not details or "game" not in details:
            return False, None

        game = details.get("game", {})
        status_group = game.get("statusGroup")
        status_text = game.get("statusText", "")

        home_score = game.get("homeCompetitor", {}).get("score")
        away_score = game.get("awayCompetitor", {}).get("score")
        has_real_score = (
            home_score is not None and away_score is not None and 
            home_score >= 0 and away_score >= 0
        )

        logger.debug(
            f"{match_id}: 365Scores statusGroup={status_group}, statusText='{status_text}', "
            f"score={home_score}-{away_score}"
        )

        if has_real_score and (home_score > 0 or away_score > 0):
            return True, status_text

        if status_group == STATUS_GROUP_LIVE:
            return True, status_text
        elif status_group == STATUS_GROUP_SCHEDULED:
            return False, "scheduled"
        elif status_group == STATUS_GROUP_FINISHED:
            return False, "finished"
        else:
            live_markers = ("1st half", "2nd half", "first half", "second half", "halftime", "ht", "live")
            if any(marker in status_text.lower() for marker in live_markers):
                return True, status_text
            return False, status_text

    def _process_match(self, match: Dict[str, Any]):
        match_id = match.get("matchId")
        game_id = match.get("threesixtyfiveGameId")

        if not game_id:
            logger.warning(f"No 365Scores game_id for {match_id}, skipping")
            return

        kickoff_utc = match.get("kickoffUtc")
        minutes_to_kickoff = None
        kickoff_passed = False

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
                kickoff_passed = minutes_to_kickoff <= 0

        match["minutes_to_kickoff"] = minutes_to_kickoff

        state = self.state_machine.determine_state(match)
        match["_state"] = state

        current_status = match.get("status", "upcoming")

        if kickoff_passed and current_status != "live":
            logger.warning(
                f"⏰ {match_id}: Kickoff passed ({minutes_to_kickoff:.0f} mins ago) "
                f"but status is '{current_status}' — FORCING 'live'"
            )
            self.store.update_status(match_id, "live")
            self.forwarder.forward_live_update({
                "fixture_id": match_id,
                "event_type": "status_change",
                "status": "live",
                "is_live": True,
                "available_for_voting": False,
                "minutes_to_kickoff": minutes_to_kickoff,
            })
            match["status"] = "live"
            current_status = "live"
            self._notify_match_live(match)

        new_status = self.state_machine.should_update_status(match)

        if new_status and new_status != current_status:
            if new_status == "live":
                if kickoff_passed:
                    logger.info(f"⏰ {match_id}: Kickoff passed, transitioning to 'live'")
                else:
                    is_actually_live, _ = self._verify_live_status_with_365scores(match)
                    if not is_actually_live and minutes_to_kickoff is not None and minutes_to_kickoff > 2:
                        logger.info(
                            f"⏳ {match_id}: 365Scores says not live yet, delaying 'soon' → 'live' "
                            f"({minutes_to_kickoff:.0f} mins to kickoff)"
                        )
                        new_status = None
                    elif not is_actually_live and minutes_to_kickoff is not None and minutes_to_kickoff <= 2:
                        logger.info(
                            f"⏰ {match_id}: Near kickoff ({minutes_to_kickoff:.0f} mins) — "
                            f"transitioning to 'live' even though 365Scores not updated yet"
                        )

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
                match["status"] = new_status
                current_status = new_status

                if new_status == "completed":
                    self._finalize_match_result(match)
                    return
                if new_status == "live":
                    self._notify_match_live(match)

        if self.state_machine.should_fetch_lineups(match, current_status, minutes_to_kickoff):
            self._fetch_and_forward_lineups(match)
            self.state_machine.mark_lineups_done(match_id)

        if current_status == "live":
            self._fetch_live_updates(match)

        if current_status == "live":
            self._fetch_commentary(match)

        if self.state_machine.should_finalize_result(match):
            self._finalize_match_result(match)

        self.store.record_last_poll(match_id)

    def _fetch_commentary(self, match: Dict[str, Any]):
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
        # NOTE: removed self.store.add_commentary_bulk(match_id, new_entries)
        # direct-write here. It used upsert=True and was silently recreating
        # zombie fixture documents (partial docs with an auto-generated
        # ObjectId _id) whenever commentary kept arriving for a match that
        # had already been archived to games_history and deleted from
        # `fixtures`. forward_commentary_bulk above already persists this
        # correctly via the Rust API, which safely no-ops (DocumentNotFound)
        # instead of upserting when the fixture is gone.
        self.store.add_forwarded_event_signatures_bulk(match_id, new_signatures)

    @staticmethod
    def _team_lineup_for_forwarder(team_lineup: Dict[str, Any]) -> Dict[str, Any]:
        if not team_lineup:
            return {}
        return {
            "formation": team_lineup.get("formation"),
            "players": team_lineup.get("members", []),
        }

    def _fetch_and_forward_lineups(self, match: Dict[str, Any]):
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

    def _settle_and_complete_match(self, match: Dict[str, Any], game: Dict[str, Any]):
        """Settle bets, mark completed, and move to history."""
        match_id = match.get("matchId")
        
        home_comp = game.get("homeCompetitor", {})
        away_comp = game.get("awayCompetitor", {})
        
        home_score = home_comp.get("score")
        away_score = away_comp.get("score")
        
        if home_score is None or away_score is None:
            logger.warning(f"⚠️ Missing scores for {match_id}, cannot settle")
            return
        
        if home_score > away_score:
            result = "home"
        elif away_score > home_score:
            result = "away"
        else:
            result = "draw"
        
        logger.info(f"💰 Settling bets for {match_id}: {result} ({home_score}-{away_score})")
        
        settle_success = self.forwarder.settle_bets(match_id, result)
        
        if settle_success:
            logger.info(f"✅ Bets settled for {match_id}")
            self.state_machine.mark_settlement_success(match_id)
            
            self.store.update_score(match_id, home_score, away_score)
            self.store.update_status(match_id, "completed")
            
            history_success = self.forwarder.move_to_history(match_id)
            if history_success:
                logger.info(f"📦 {match_id} moved to history")
                self.state_machine.mark_completed_notified(match_id)
                self._trigger_rescrape(reason=f"{match_id} archived via settlement")
            else:
                logger.warning(f"⚠️ Match {match_id} settled but failed to move to history")
        else:
            self.state_machine.record_settlement_attempt(match_id)
            logger.warning(
                f"⚠️ Settlement failed for {match_id} (attempt {self.state_machine.settlement_retries.get(match_id, 0)})"
            )

    def _fetch_live_updates(self, match: Dict[str, Any]):
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

        home_comp = game.get("homeCompetitor", {})
        away_comp = game.get("awayCompetitor", {})

        home_score = home_comp.get("score")
        away_score = away_comp.get("score")

        has_real_score = (
            home_score is not None and away_score is not None and 
            home_score >= 0 and away_score >= 0
        )

        if has_real_score:
            self.store.update_score(match_id, home_score, away_score)
            logger.info(f"📊 {match_id}: Score updated {home_score}-{away_score}")

        phase = threesixtyfive.classify_match_phase(status_text)
        if self.state_machine.should_forward_statistics(match_id, phase):
            logger.info(f"📊 {match_id}: phase={phase} ({status_text!r}) - fetching statistics")
            self._fetch_and_forward_statistics(match, game=game)
            self.state_machine.mark_stats_forwarded(match_id, phase)

        # ─── MATCH END DETECTION ──────────────────────────────────────────────
        if phase == "fulltime" or status_group == STATUS_GROUP_FINISHED:
            logger.info(f"🏁 {match_id}: Match ended")
            
            # Check if already settled
            if match_id in self.state_machine.completed_notified:
                logger.info(f"⏭️ {match_id} already completed and notified")
                return
            
            # Check if we should retry settlement
            if not self.state_machine.should_retry_settlement(match_id):
                logger.warning(f"⚠️ {match_id}: Max settlement retries reached, forcing completion")
                self.store.update_status(match_id, "completed")
                self._finalize_match_result(match)
                return
            
            # Settle bets and complete match
            self._settle_and_complete_match(match, game)
            return

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

    def _trigger_rescrape(self, reason: str = ""):
        """Immediately re-run fixture discovery so a freshly-archived slot
        in `fixtures` gets refilled with upcoming matches right away,
        instead of waiting for the next scheduled /scrape trigger.

        Runs synchronously in the poll loop -- a slow 365Scores response
        here will delay polling of other live matches for that cycle. If
        that becomes a problem in practice (e.g. several matches finishing
        around the same time during a busy tournament window), move the
        body of this into a daemon thread instead of calling it inline."""
        try:
            logger.info(f"🔄 Triggering rescrape ({reason})...")
            new_count = scraper.scrape_world_cup_fixtures(self.store)
            logger.info(f"✅ Rescrape complete: {new_count} fixtures upserted")
        except Exception as e:
            logger.error(f"❌ Rescrape failed: {e}")

    def _finalize_match_result(self, match: Dict[str, Any]):
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

        success = self.forwarder.move_to_history(match_id)
        if success:
            self.state_machine.mark_completed_notified(match_id)
            logger.info(f"🏁 Match {match_id} finalized: {result} ({home_score}-{away_score})")
            self._trigger_rescrape(reason=f"{match_id} finalized")

    def _notify_match_live(self, match: Dict[str, Any]):
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