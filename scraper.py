"""
Internationals fixture scraper -- fetches this week's fixtures only
(today + SCRAPE_DAYS_AHEAD) across whatever competitions are configured
in config.COMPETITION_IDS (World Cup, Nations League, Euro Qualifiers, ...).

Calls threesixtyfive.fetch_games_by_competition(), which hits the
CONFIRMED-working /web/games/fixtures/ endpoint.

This replaces the old World-Cup-only scraper: same data source, same
upsert flow, just no longer hardcoded to a single competition id.
"""

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
logger = logging.getLogger("intl_poller.scraper")

# All competitions we track. Was: WORLD_CUP_COMPETITION_IDS = [5930]
COMPETITION_IDS: dict[str, int] = config.COMPETITION_IDS
ALL_COMPETITION_IDS: list[int] = list(COMPETITION_IDS.values())

# Reverse lookup so we can tag each fixture with a human-readable key
# (e.g. "nations_league") from the competitionId 365Scores returns.
_ID_TO_KEY = {v: k for k, v in COMPETITION_IDS.items()}

SCRAPE_DAYS_AHEAD = config.SCRAPE_DAYS_AHEAD


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


def scrape_international_fixtures(store: FixtureStore) -> int:
    today_utc = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = today_utc + datetime.timedelta(days=SCRAPE_DAYS_AHEAD)

    logger.info(
        "Fetching internationals fixtures from 365Scores (competitions=%s) ...",
        COMPETITION_IDS,
    )

    # Single call across all configured competitions -- fetch_games_by_competition
    # accepts a list and 365Scores returns the union, each game tagged with
    # its own competitionId so we can split them back out below.
    games = threesixtyfive.fetch_games_by_competition(ALL_COMPETITION_IDS)
    if games is None:
        raise RuntimeError("fetch_games_by_competition returned None")

    logger.info("365Scores returned %d raw games", len(games))
    if not games:
        logger.warning(
            "0 games returned for competition IDs %s. This is EXPECTED for "
            "euro_qualifiers (id %s) until closer to 26-27 March 2027 "
            "(Matchday 1 of EURO 2028 qualifying) -- 365Scores has nothing "
            "to return before fixtures are scheduled. For nations_league "
            "(id %s), expect games from 24 Sep 2026 onward. If 0 persists "
            "for a competition that SHOULD have fixtures, re-verify the "
            "games/fixtures/ URL via DevTools on 365scores.com.",
            ALL_COMPETITION_IDS,
            COMPETITION_IDS.get("euro_qualifiers"),
            COMPETITION_IDS.get("nations_league"),
        )
        return 0

    # Safety-net filter to today -> today+N days by kickoff date.
    in_window: list[dict] = []
    for g in games:
        kickoff = _parse_kickoff(g.get("startTime"))
        if today_utc <= kickoff.date() < cutoff:
            in_window.append(g)

    logger.info(
        "%d games within %d-day window (%s to %s)",
        len(in_window),
        SCRAPE_DAYS_AHEAD,
        today_utc,
        cutoff,
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
        comp_key = _ID_TO_KEY.get(competition_id, "unknown")

        kickoff = _parse_kickoff(game.get("startTime"))
        status = _status_to_internal(game.get("statusText", ""))

        # match_id prefix generalized from the old hardcoded "wc26_" to the
        # competition key, e.g. "nations_league_4627864" -- keeps ids unique
        # and readable across competitions instead of implying World Cup.
        match_id = f"{comp_key}_{game_id}"

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
            odds=game.get("odds", {}),
        )
        upserted += 1

        logger.info(
            "Upserted %s: %s vs %s [%s] kickoff=%s (%s)",
            match_id,
            home_team,
            away_team,
            status,
            kickoff.strftime("%Y-%m-%d %H:%M"),
            comp_name,
        )

    return upserted


def main() -> None:
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        logger.error("MONGO_URI environment variable is required")
        sys.exit(1)

    store = FixtureStore(mongo_uri)
    try:
        count = scrape_international_fixtures(store)
        logger.info("Scrape complete: %d fixtures upserted", count)
    except Exception as exc:
        logger.error("Scrape failed: %s", exc)
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
