"""
365Scores source.

The sole live data source post-pivot: fixture discovery, score, status,
and structured events (goal/card/sub). Confirmed working with plain
`requests` — no curl_cffi/Chrome impersonation needed, since 365Scores'
WAF doesn't appear to fingerprint TLS the way Sofascore's does.

Confirmed NOT present in this source: prose/text commentary. The `game`
object's `events` array only carries structured fields (gameTime,
eventType.name, eventType.subTypeName) — no `text` field anywhere, and
a guessed dedicated commentary endpoint (textWidget) returned 404. Don't
re-add commentary-fetching logic here without a freshly confirmed
endpoint — see config.py's module docstring for the full history of
what's been ruled out and why.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import requests

import config

logger = logging.getLogger("worldcup_poller.sources.threesixtyfive")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.365scores.com/",
    "Origin": "https://www.365scores.com",
}

_seen_status_texts: set[str] = set()


def _get_json(url: str) -> Optional[dict[str, Any]]:
    time.sleep(random.uniform(config.JITTER_MIN_SECONDS, config.JITTER_MAX_SECONDS))
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=config.REQUEST_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("365Scores request failed for %s: %s", url, exc)
        return None

    if resp.status_code != 200:
        logger.warning("365Scores returned status %s for %s", resp.status_code, url)
        return None

    try:
        return resp.json()
    except Exception as exc:
        logger.warning("365Scores JSON decode failed for %s: %s", url, exc)
        return None


def fetch_current_games() -> Optional[list[dict[str, Any]]]:
    """All current (live + recently scheduled) football games. Used both
    for fixture discovery (scraper.py) and for resolving which games are
    currently live (poller.py)."""
    url = (
        f"{config.THREESIXTYFIVE_BASE}/games/current/"
        f"?appTypeId={config.THREESIXTYFIVE_APP_TYPE_ID}"
        f"&langId={config.THREESIXTYFIVE_LANG_ID}"
        f"&sports={config.THREESIXTYFIVE_FOOTBALL_SPORT_ID}"
    )
    data = _get_json(url)
    if data is None:
        return None
    return data.get("games", [])


def fetch_game_detail(game_id: str) -> Optional[dict[str, Any]]:
    """Full game object: score, status, gameTime, events[]."""
    url = (
        f"{config.THREESIXTYFIVE_BASE}/game/"
        f"?appTypeId={config.THREESIXTYFIVE_APP_TYPE_ID}"
        f"&langId={config.THREESIXTYFIVE_LANG_ID}"
        f"&gameId={game_id}"
    )
    data = _get_json(url)
    if data is None:
        return None
    return data.get("game", data)


def resolve_game_id(home_team: str, away_team: str) -> Optional[str]:
    """Best-effort match of a fixture (known by team names) to a
    365Scores game_id, by scanning current games. Case-insensitive
    substring match since naming conventions differ slightly between
    providers (e.g. 'Korea Republic' vs 'South Korea')."""
    games = fetch_current_games()
    if not games:
        return None

    home_lower = home_team.lower()
    away_lower = away_team.lower()

    for g in games:
        g_home = (g.get("homeCompetitor", {}) or {}).get("name", "").lower()
        g_away = (g.get("awayCompetitor", {}) or {}).get("name", "").lower()
        home_match = home_lower in g_home or g_home in home_lower
        away_match = away_lower in g_away or g_away in away_lower
        if home_match and away_match:
            return str(g.get("id"))
    return None


def _normalize_event_type(raw_name: str) -> str:
    key = raw_name.strip().lower()
    return config.EVENT_TYPE_MAP.get(key, key.replace(" ", "_"))


def _normalize_status(status_text: str) -> str:
    key = status_text.strip().lower()
    if key not in _seen_status_texts:
        _seen_status_texts.add(key)
        logger.info("365Scores new statusText seen: %r", status_text)
    return config.STATUS_TEXT_MAP.get(key, "live")


def extract_score_and_status(game: dict[str, Any]) -> dict[str, Any]:
    """Pulls the score/status fields out of a 365Scores game object,
    in our own normalized shape — caller decides which backend endpoint(s)
    to push this into."""
    home = game.get("homeCompetitor", {}) or {}
    away = game.get("awayCompetitor", {}) or {}
    status_text = game.get("statusText", "") or game.get("shortStatusText", "")

    return {
        "home_score": home.get("score", 0) or 0,
        "away_score": away.get("score", 0) or 0,
        "minute": game.get("gameTime"),
        "minute_display": game.get("gameTimeDisplay", ""),
        "status_text_raw": status_text,
        "normalized_status": _normalize_status(status_text),
    }


def extract_events(game: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalizes 365Scores' `events` array into the shape forwarder.py's
    LiveGameUpdate builder expects. Each event already carries the score
    AT THAT POINT via competitorId — but 365Scores' event objects don't
    include a running score snapshot, so the caller (poller.py) is
    responsible for attaching the game's current score to each event
    before forwarding, since the Rust side's LiveGameUpdate requires
    home_score/away_score on every update.
    """
    home = game.get("homeCompetitor", {}) or {}
    away = game.get("awayCompetitor", {}) or {}
    home_id = home.get("id")
    away_id = away.get("id")

    normalized = []
    for e in game.get("events", []):
        event_type_obj = e.get("eventType", {}) or {}
        raw_name = event_type_obj.get("name", "")
        sub_type_name = event_type_obj.get("subTypeName")

        competitor_id = e.get("competitorId")
        team_side = None
        if competitor_id == home_id:
            team_side = "home_team"
        elif competitor_id == away_id:
            team_side = "away_team"

        normalized.append({
            "event_type": _normalize_event_type(raw_name),
            "raw_event_name": raw_name,
            "sub_type_name": sub_type_name,
            "minute": e.get("gameTime"),
            "minute_display": e.get("gameTimeDisplay", ""),
            "team": team_side,
            # 365Scores' event payload doesn't carry player names in
            # the sample we've inspected — `members` at the game level
            # may have this; not yet confirmed. Leave None rather than
            # guess a field that might not exist.
            "player": None,
        })
    return normalized
