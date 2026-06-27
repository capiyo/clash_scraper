"""
365Scores API client for World Cup data.
Uses the confirmed-working /web/games/fixtures/ endpoint.
"""
from __future__ import annotations

import logging
import requests
from typing import List, Dict, Any, Optional

logger = logging.getLogger("worldcup_poller.sources.threesixtyfive")

# Base URL for 365Scores API
BASE_URL = "https://webws.365scores.com"

# Default headers (mimicking browser request)
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.365scores.com/",
    "Origin": "https://www.365scores.com",
}


def fetch_games_by_competition(
    competition_ids: List[int],
    timezone_name: str = "Africa/Nairobi",
    user_country_id: int = 413,
    show_odds: bool = True,
) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch games for given competition IDs using the /web/games/fixtures/ endpoint.
    
    This endpoint was confirmed via browser DevTools capture on the actual
    365Scores World Cup page. The games/current/ endpoint silently ignores
    the competitions parameter, while games/fixtures/ respects it.
    """
    params = {
        "appTypeId": 5,
        "langId": 1,
        "timezoneName": timezone_name,
        "userCountryId": user_country_id,
        "competitions": ",".join(str(cid) for cid in competition_ids),
        "showOdds": str(show_odds).lower(),
        "includeTopBettingOpportunity": "1",
        "topBookmaker": "14",
    }

    url = f"{BASE_URL}/web/games/fixtures/"
    
    try:
        logger.debug(f"Fetching from {url} with params {params}")
        response = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        games = data.get("games", [])
        logger.info(f"fetch_games_by_competition({competition_ids}): {len(games)} games returned")
        
        return games
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch games from 365Scores: {e}")
        return None
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        return None


def fetch_game_details(game_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch detailed information for a specific game.
    """
    url = f"{BASE_URL}/web/games/details/"
    params = {
        "appTypeId": 5,
        "langId": 1,
        "gameId": game_id,
        "showOdds": "true",
    }
    
    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch game details for {game_id}: {e}")
        return None