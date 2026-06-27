"""
365Scores API client.

Fixture discovery endpoint CONFIRMED by direct browser DevTools network
capture on 2026-06-27, loading the actual World Cup fixtures page:

    GET https://webws.365scores.com/web/games/fixtures/
        ?appTypeId=5&langId=1&timezoneName=<tz>&userCountryId=<id>
        &competitions=<comma-separated competition ids>
        &showOdds=true&includeTopBettingOpportunity=1&topBookmaker=14

This is a DIFFERENT route from games/current/. games/current/ silently
ignores the `competitions` param and always returns a generic top-100
"what's happening right now" list — confirmed by requesting
competitions=5930 against games/current/ and getting back Botola 2,
Yemeni League, OBOS-ligaen, etc. with zero World Cup games.

games/fixtures/ is the endpoint the real 365scores.com web UI calls
when you load a competition's schedule/fixtures tab, and it correctly
filters by competitions=. Do not revert to games/current/ for
competition-scoped fixture discovery.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

import config

logger = logging.getLogger("worldcup_poller.sources.threesixtyfive")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.365scores.com/",
}

FIXTURES_PATH = "/games/fixtures/"


def fetch_games_by_competition(
    competition_ids: list[int],
    timezone_name: str = "UTC",
    user_country_id: Optional[int] = None,
) -> Optional[list[dict]]:
    """Fetch fixtures for the given competition IDs via the confirmed
    /web/games/fixtures/ endpoint.

    Args:
        competition_ids: 365Scores competition IDs to filter by, e.g. [5930].
        timezone_name: IANA tz name passed straight through to 365Scores
            (affects kickoff time display in the response, not which
            games are returned).
        user_country_id: optional 365Scores country ID; only affects
            odds/bookmaker fields in the response, safe to omit.

    Returns:
        List of raw game dicts as returned by 365Scores, or None on any
        request/parsing failure (treat as "try again later", not as
        confirmed zero fixtures).
    """
    params = {
        "appTypeId": config.THREESIXTYFIVE_APP_TYPE_ID,
        "langId": config.THREESIXTYFIVE_LANG_ID,
        "timezoneName": timezone_name,
        "competitions": ",".join(str(c) for c in competition_ids),
    }
    if user_country_id is not None:
        params["userCountryId"] = user_country_id

    url = f"{config.THREESIXTYFIVE_BASE}{FIXTURES_PATH}"

    try:
        resp = requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("fetch_games_by_competition failed: %s", exc)
        return None

    games = data.get("games", [])
    if not isinstance(games, list):
        logger.error(
            "Unexpected response shape: 'games' is %s, not a list",
            type(games),
        )
        return None

    logger.info(
        "fetch_games_by_competition(%s): %d games returned",
        competition_ids, len(games),
    )
    return games
