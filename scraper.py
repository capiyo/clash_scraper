"""
World Cup fixture scraper — fetches this week's fixtures only (today + 6 days).
Calls threesixtyfive.fetch_games_by_competition(), which now hits the
CONFIRMED-working /web/games/fixtures/ endpoint.
"""

# Invoke-RestMethod -Uri "https://clash-scraper.onrender.com/scrape" -Method GET
from __future__ import annotations

import datetime
import logging
import os
import sys

from dotenv import load_dotenv

from mongo_store import FixtureStore
from sources import threesixtyfive
import config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worldcup_poller.scraper")

# 365Scores competitionId for FIFA World Cup 2026.
WORLD_CUP_COMPETITION_IDS: list[int] = [5930]
SCRAPE_DAYS_AHEAD = 7


import re

def _status_to_internal(status_text: str) -> str:
    text = (status_text or "").strip().lower()
    if text in ("finished", "ft", "ended", "full-time", "aet", "pen"):
        return "completed"
    
    # Word-boundary matching prevents "Live" in "Live Lineups" from matching
    live_patterns = (r"\blive\b", r"\b1st half\b", r"\b2nd half\b", r"\bht\b", r"\bhalftime\b", r"\bin progress\b")
    if any(re.search(pattern, text) for pattern in live_patterns):
        return "live"
    
    return "upcoming"

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

    logger.info(
        "Fetching WC fixtures from 365Scores (competitions=%s) ...",
        WORLD_CUP_COMPETITION_IDS,
    )
    games = threesixtyfive.fetch_games_by_competition(WORLD_CUP_COMPETITION_IDS)

    if games is None:
        raise RuntimeError("fetch_games_by_competition returned None")

    logger.info("365Scores returned %d raw games", len(games))

    if not games:
        logger.warning(
            "0 games returned for competition IDs %s — if this persists, "
            "re-verify the games/fixtures/ URL via DevTools on "
            "365scores.com's WC fixtures page.",
            WORLD_CUP_COMPETITION_IDS,
        )
        return 0

    # Safety-net filter to today -> today+6 days by kickoff date.
    in_window: list[dict] = []
    for g in games:
        kickoff = _parse_kickoff(g.get("startTime"))
        if today_utc <= kickoff.date() < cutoff:
            in_window.append(g)

    logger.info(
        "%d games within %d-day window (%s to %s)",
        len(in_window), SCRAPE_DAYS_AHEAD, today_utc, cutoff,
    )

    upserted = 0
    for game in in_window:
        game_id = str(game.get("id"))
        home_team = (game.get("homeCompetitor") or {}).get("name", "Unknown")
        away_team = (game.get("awayCompetitor") or {}).get("name", "Unknown")
        home_competitor_id = (game.get("homeCompetitor") or {}).get("id")
        away_competitor_id = (game.get("awayCompetitor") or {}).get("id")
        competition_id = game.get("competitionId")
        comp_name = game.get("competitionDisplayName", "")
        kickoff = _parse_kickoff(game.get("startTime"))
        status = _status_to_internal(game.get("statusText", ""))
        match_id = f"wc26_{game_id}"

        store.upsert_fixture(
            match_id=match_id,
            threesixtyfive_game_id=game_id,
            home_team=home_team,
            away_team=away_team,
            home_competitor_id=home_competitor_id,
            away_competitor_id=away_competitor_id,
            competition_id=competition_id,
            kickoff_utc=kickoff,
            status=status,
            competition_name=comp_name,
            odds=game.get("odds", {})
        )
        upserted += 1
        logger.info(
            "Upserted %s: %s vs %s [%s] kickoff=%s (%s)",
            match_id, home_team, away_team, status,
            kickoff.strftime("%Y-%m-%d %H:%M"), comp_name,
        )

    return upserted


def main() -> None:
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        logger.error("MONGO_URI environment variable is required")
        sys.exit(1)

    store = FixtureStore(mongo_uri)
    try:
        count = scrape_world_cup_fixtures(store)
        logger.info("Scrape complete: %d fixtures upserted", count)
    except Exception as exc:
        logger.error("Scrape failed: %s", exc)
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()