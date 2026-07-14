"""
365Scores source — sole live data source post-pivot.
No curl_cffi, no pagination loops — games/current already returns
today's and near-future fixtures. We filter by date in Python.
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
    "Accept":  "application/json",
    "Referer": "https://www.365scores.com/",
    "Origin":  "https://www.365scores.com",
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
        logger.warning("365Scores returned %s for %s", resp.status_code, url)
        return None
    try:
        return resp.json()
    except Exception as exc:
        logger.warning("365Scores JSON decode failed for %s: %s", url, exc)
        return None


def fetch_current_games() -> Optional[list[dict[str, Any]]]:
    """Today's games for all sports=1 (football). Used by poller for live detection."""
    url = (
        f"{config.THREESIXTYFIVE_BASE}/games/current/"
        f"?appTypeId={config.THREESIXTYFIVE_APP_TYPE_ID}"
        f"&langId={config.THREESIXTYFIVE_LANG_ID}"
        f"&sports={config.THREESIXTYFIVE_FOOTBALL_SPORT_ID}"
    )
    data = _get_json(url)
    return data.get("games", []) if data else None


def fetch_games_by_competition(competition_ids: list[int]) -> Optional[list[dict[str, Any]]]:
    """Fetch today's games filtered to specific competition IDs.
    
    Uses the competitions= param which 365Scores supports natively.
    No pagination — games/current is already date-scoped to today/near-future.
    Date filtering to a 7-day window is done in the caller (scraper.py).
    """
    if not competition_ids:
        return []

    competitions_param = ",".join(str(c) for c in competition_ids)
    url = (
        f"{config.THREESIXTYFIVE_BASE}/games/current/"
        f"?appTypeId={config.THREESIXTYFIVE_APP_TYPE_ID}"
        f"&langId={config.THREESIXTYFIVE_LANG_ID}"
        f"&sports={config.THREESIXTYFIVE_FOOTBALL_SPORT_ID}"
        f"&competitions={competitions_param}"
    )
    data = _get_json(url)
    return data.get("games", []) if data else None


def fetch_game_detail(game_id: str) -> Optional[dict[str, Any]]:
    """Full game object: score, status, gameTime, events[]."""
    url = (
        f"{config.THREESIXTYFIVE_BASE}/game/"
        f"?appTypeId={config.THREESIXTYFIVE_APP_TYPE_ID}"
        f"&langId={config.THREESIXTYFIVE_LANG_ID}"
        f"&gameId={game_id}"
    )
    data = _get_json(url)
    return data.get("game", data) if data else None


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
    home = game.get("homeCompetitor") or {}
    away = game.get("awayCompetitor") or {}
    status_text = game.get("statusText", "") or game.get("shortStatusText", "")
    return {
        "home_score":        home.get("score", 0) or 0,
        "away_score":        away.get("score", 0) or 0,
        "minute":            game.get("gameTime"),
        "minute_display":    game.get("gameTimeDisplay", ""),
        "status_text_raw":   status_text,
        "normalized_status": _normalize_status(status_text),
    }


def extract_events(game: dict[str, Any]) -> list[dict[str, Any]]:
    home    = game.get("homeCompetitor") or {}
    away    = game.get("awayCompetitor") or {}
    home_id = home.get("id")
    away_id = away.get("id")

    normalized = []
    for e in game.get("events", []):
        event_type_obj = e.get("eventType") or {}
        raw_name       = event_type_obj.get("name", "")

        competitor_id = e.get("competitorId")
        team_side = None
        if competitor_id == home_id:
            team_side = "home_team"
        elif competitor_id == away_id:
            team_side = "away_team"

        normalized.append({
            "event_type":     _normalize_event_type(raw_name),
            "raw_event_name": raw_name,
            "sub_type_name":  (event_type_obj.get("subTypeName")),
            "minute":         e.get("gameTime"),
            "minute_display": e.get("gameTimeDisplay", ""),
            "team":           team_side,
            "player":         None,
        })
    return normalized
