"""
365Scores API client for World Cup data.
Fetches: fixtures, live scores, events, lineups, and statistics.
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


def fetch_game_details(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int,
    lang_id: int = 37,
    user_country_id: int = 7
) -> Optional[Dict[str, Any]]:
    """
    Fetch full game details including lineups using the /web/game/ endpoint.
    
    Args:
        game_id: 365Scores game ID (e.g., "4627864")
        away_id: Away team competitor ID
        home_id: Home team competitor ID
        competition_id: Competition ID (e.g., 5930)
        lang_id: Language ID (37 = Dutch, 1 = English)
        user_country_id: Country ID (7 = Netherlands, 413 = Kenya)
    
    Returns:
        Full game data including lineups, statistics, events, etc.
    """
    matchup_id = f"{away_id}-{home_id}-{competition_id}"
    
    params = {
        "appTypeId": 5,
        "langId": lang_id,
        "timezoneName": "Africa/Nairobi",
        "userCountryId": user_country_id,
        "gameId": game_id,
        "matchupId": matchup_id,
    }
    
    url = f"{BASE_URL}/web/game/"
    
    try:
        logger.debug(f"Fetching game details from {url} with params {params}")
        response = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        logger.info(f"fetch_game_details({game_id}): Success")
        return data
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch game details for {game_id}: {e}")
        return None
    except ValueError as e:
        logger.error(f"Failed to parse JSON response for {game_id}: {e}")
        return None


def fetch_lineups(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int
) -> Optional[Dict[str, Any]]:
    """
    Fetch only lineups from the game details endpoint.
    
    Returns:
        {
            "home": {
                "formation": "4-3-3",
                "status": "Confirmed",
                "members": [...]
            },
            "away": {
                "formation": "4-2-3-1",
                "status": "Confirmed",
                "members": [...]
            }
        }
    """
    data = fetch_game_details(game_id, away_id, home_id, competition_id)
    
    if not data or "game" not in data:
        logger.warning(f"No game data found for {game_id}")
        return None
    
    game = data.get("game", {})
    
    home_competitor = game.get("homeCompetitor", {})
    away_competitor = game.get("awayCompetitor", {})
    
    home_lineups = home_competitor.get("lineups")
    away_lineups = away_competitor.get("lineups")
    
    if not home_lineups and not away_lineups:
        logger.debug(f"No lineups available for {game_id}")
        return None
    
    result = {
        "fixture_id": f"wc26_{game_id}",
        "home": home_lineups or {},
        "away": away_lineups or {},
    }
    
    logger.info(f"fetch_lineups({game_id}): Found lineups")
    return result


def fetch_statistics(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int
) -> Optional[Dict[str, Any]]:
    """
    Fetch statistics from the game details endpoint.
    """
    data = fetch_game_details(game_id, away_id, home_id, competition_id)
    
    if not data or "game" not in data:
        return None
    
    game = data.get("game", {})
    
    # Statistics are in the game object
    stats = {
        "home": {
            "possession": game.get("homePossession"),
            "shots": game.get("homeShots"),
            "shots_on_target": game.get("homeShotsOnTarget"),
            "corners": game.get("homeCorners"),
            "fouls": game.get("homeFouls"),
            "yellow_cards": game.get("homeYellowCards"),
            "red_cards": game.get("homeRedCards"),
        },
        "away": {
            "possession": game.get("awayPossession"),
            "shots": game.get("awayShots"),
            "shots_on_target": game.get("awayShotsOnTarget"),
            "corners": game.get("awayCorners"),
            "fouls": game.get("awayFouls"),
            "yellow_cards": game.get("awayYellowCards"),
            "red_cards": game.get("awayRedCards"),
        },
        "minute": game.get("gameTime", 0)
    }
    
    return stats


def fetch_complete_match_data(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int
) -> Optional[Dict[str, Any]]:
    """
    Fetch all match data: details, lineups, and statistics in one go.
    """
    data = fetch_game_details(game_id, away_id, home_id, competition_id)
    
    if not data or "game" not in data:
        return None
    
    game = data.get("game", {})
    
    return {
        "game_id": game_id,
        "details": game,
        "lineups": {
            "home": game.get("homeCompetitor", {}).get("lineups", {}),
            "away": game.get("awayCompetitor", {}).get("lineups", {}),
        },
        "statistics": {
            "home": {
                "possession": game.get("homePossession"),
                "shots": game.get("homeShots"),
                "shots_on_target": game.get("homeShotsOnTarget"),
            },
            "away": {
                "possession": game.get("awayPossession"),
                "shots": game.get("awayShots"),
                "shots_on_target": game.get("awayShotsOnTarget"),
            },
        },
        "score": {
            "home": game.get("homeCompetitor", {}).get("score", 0),
            "away": game.get("awayCompetitor", {}).get("score", 0),
        },
        "status": game.get("statusText"),
        "time_elapsed": game.get("gameTime", 0),
    }