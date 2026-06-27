"""
World Cup fixture scraper — fetches this week's fixtures only (today + 6 days).
Uses /web/games/fixtures/ endpoint (confirmed working via DevTools capture).
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
from dotenv import load_dotenv
from mongo_store import FixtureStore
from sources import threesixtyfive

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worldcup_poller.scraper")

WORLD_CUP_COMPETITION_IDS = [5930]
SCRAPE_DAYS_AHEAD = 7


def _status_to_internal(status_text: str) -> str:
    text = (status_text or "").strip().lower()
    if text in ("finished", "ft", "ended", "full-time"):
        return "completed"
    if text in ("", "scheduled", "not started"):
        return "upcoming"
    return "live"


def _parse_kickoff(start_time_raw: str | None) -> datetime.datetime:
    now = datetime.datetime.now(datetime.timezone.utc)
    if not start_time_raw:
        return now
    try:
        return datetime.datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return now


def scrape_world_cup_fixtures(store: FixtureStore) -> int:
    today_utc = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = today_utc + datetime.timedelta(days=SCRAPE_DAYS_AHEAD)

    logger.info(f"Fetching WC fixtures from 365Scores (competitions={WORLD_CUP_COMPETITION_IDS}) ...")
    games = threesixtyfive.fetch_games_by_competition(WORLD_CUP_COMPETITION_IDS)

    if games is None:
        raise RuntimeError("fetch_games_by_competition returned None")

    logger.info(f"365Scores returned {len(games)} raw games")

    in_window = []
    for g in games:
        kickoff = _parse_kickoff(g.get("startTime"))
        if today_utc <= kickoff.date() < cutoff:
            in_window.append(g)

    logger.info(f"{len(in_window)} games within {SCRAPE_DAYS_AHEAD}-day window")

    upserted = 0
    for game in in_window:
        game_id = str(game.get("id"))
        home_team = (game.get("homeCompetitor") or {}).get("name", "Unknown")
        away_team = (game.get("awayCompetitor") or {}).get("name", "Unknown")
        comp_name = game.get("competitionDisplayName", "")
        kickoff = _parse_kickoff(game.get("startTime"))
        status = _status_to_internal(game.get("statusText", ""))
        match_id = f"wc26_{game_id}"

        store.upsert_fixture(
            match_id=match_id,
            threesixtyfive_game_id=game_id,
            home_team=home_team,
            away_team=away_team,
            kickoff_utc=kickoff,
            status=status,
            competition_name=comp_name,
            odds=game.get("odds", {})
        )
        upserted += 1
        logger.info(f"Upserted {match_id}: {home_team} vs {away_team} [{status}]")

    return upserted


def main():
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        logger.error("MONGO_URI environment variable is required")
        sys.exit(1)

    store = FixtureStore(mongo_uri)
    try:
        count = scrape_world_cup_fixtures(store)
        logger.info(f"Scrape complete: {count} fixtures upserted")
    except Exception as exc:
        logger.error(f"Scrape failed: {exc}")
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()