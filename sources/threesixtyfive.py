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


def fetch_game_details(game_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch detailed information for a specific game including scores, events, and commentary.
    """
    url = f"{BASE_URL}/web/games/details/"
    params = {
        "appTypeId": 5,
        "langId": 1,
        "gameId": game_id,
        "showOdds": "true",
        "includeEvents": "true",
        "includeCommentary": "true",
    }
    
    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch game details for {game_id}: {e}")
        return None


def fetch_lineups(game_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch lineups for a specific game from 365Scores.
    
    Returns:
        {
            "home": {
                "formation": "4-3-3",
                "coach": {"name": "Coach Name"},
                "players": [
                    {"name": "Player", "position": "GK", "jerseyNumber": 1, "captain": false},
                    ...
                ],
                "bench": [...]
            },
            "away": {...}
        }
    """
    url = f"{BASE_URL}/web/games/lineups/"
    params = {
        "appTypeId": 5,
        "langId": 1,
        "gameId": game_id,
    }
    
    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        lineups = data.get("lineups", {})
        
        # Transform to match Rust model structure
        return _transform_lineups(lineups, game_id)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch lineups for {game_id}: {e}")
        return None
    except ValueError as e:
        logger.error(f"Failed to parse lineups JSON for {game_id}: {e}")
        return None


def _transform_lineups(raw_lineups: Dict, game_id: str) -> Dict[str, Any]:
    """
    Transform 365Scores lineup format to match Rust LineupsDocument struct.
    """
    result = {
        "fixture_id": game_id,
        "lineups": {
            "home": {
                "formation": "4-4-2",  # Default, 365Scores might not provide formation directly
                "coach": {"name": raw_lineups.get("homeCoach", "Unknown")},
                "players": [],
                "bench": []
            },
            "away": {
                "formation": "4-4-2",
                "coach": {"name": raw_lineups.get("awayCoach", "Unknown")},
                "players": [],
                "bench": []
            }
        }
    }
    
    # Parse home team players
    home_players = raw_lineups.get("homePlayers", [])
    for player in home_players:
        result["lineups"]["home"]["players"].append({
            "name": player.get("name", ""),
            "position": player.get("position", ""),
            "jersey_number": player.get("jerseyNumber", 0),
            "captain": player.get("captain", False),
            "lineup": player.get("lineup", "starting"),
            "player_id": player.get("playerId", "")
        })
    
    # Parse home bench
    home_bench = raw_lineups.get("homeBench", [])
    for player in home_bench:
        result["lineups"]["home"]["bench"].append({
            "name": player.get("name", ""),
            "position": player.get("position", ""),
            "jersey_number": player.get("jerseyNumber", 0),
            "captain": player.get("captain", False),
            "lineup": "bench",
            "player_id": player.get("playerId", "")
        })
    
    # Parse away team players
    away_players = raw_lineups.get("awayPlayers", [])
    for player in away_players:
        result["lineups"]["away"]["players"].append({
            "name": player.get("name", ""),
            "position": player.get("position", ""),
            "jersey_number": player.get("jerseyNumber", 0),
            "captain": player.get("captain", False),
            "lineup": "starting",
            "player_id": player.get("playerId", "")
        })
    
    # Parse away bench
    away_bench = raw_lineups.get("awayBench", [])
    for player in away_bench:
        result["lineups"]["away"]["bench"].append({
            "name": player.get("name", ""),
            "position": player.get("position", ""),
            "jersey_number": player.get("jerseyNumber", 0),
            "captain": player.get("captain", False),
            "lineup": "bench",
            "player_id": player.get("playerId", "")
        })
    
    return result


def fetch_statistics(game_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch match statistics for a specific game from 365Scores.
    
    Returns:
        {
            "fixture_id": "wc26_123",
            "statistics": {
                "home": {
                    "possession": 55,
                    "shots": 12,
                    "shots_on_target": 5,
                    "corners": 6,
                    "fouls": 10,
                    "yellow_cards": 2,
                    "red_cards": 0
                },
                "away": {
                    "possession": 45,
                    "shots": 8,
                    "shots_on_target": 3,
                    "corners": 3,
                    "fouls": 12,
                    "yellow_cards": 1,
                    "red_cards": 0
                }
            },
            "minute": 67
        }
    """
    url = f"{BASE_URL}/web/games/statistics/"
    params = {
        "appTypeId": 5,
        "langId": 1,
        "gameId": game_id,
    }
    
    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        return _transform_statistics(data, game_id)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch statistics for {game_id}: {e}")
        return None
    except ValueError as e:
        logger.error(f"Failed to parse statistics JSON for {game_id}: {e}")
        return None


def _transform_statistics(raw_stats: Dict, game_id: str) -> Dict[str, Any]:
    """
    Transform 365Scores statistics format to match Rust model.
    """
    home_stats = raw_stats.get("home", {})
    away_stats = raw_stats.get("away", {})
    
    return {
        "fixture_id": game_id,
        "statistics": {
            "home": {
                "possession": home_stats.get("possession", 0),
                "shots": home_stats.get("shots", 0),
                "shots_on_target": home_stats.get("shotsOnTarget", 0),
                "shots_off_target": home_stats.get("shotsOffTarget", 0),
                "corners": home_stats.get("corners", 0),
                "fouls": home_stats.get("fouls", 0),
                "yellow_cards": home_stats.get("yellowCards", 0),
                "red_cards": home_stats.get("redCards", 0),
                "offsides": home_stats.get("offsides", 0),
            },
            "away": {
                "possession": away_stats.get("possession", 0),
                "shots": away_stats.get("shots", 0),
                "shots_on_target": away_stats.get("shotsOnTarget", 0),
                "shots_off_target": away_stats.get("shotsOffTarget", 0),
                "corners": away_stats.get("corners", 0),
                "fouls": away_stats.get("fouls", 0),
                "yellow_cards": away_stats.get("yellowCards", 0),
                "red_cards": away_stats.get("redCards", 0),
                "offsides": away_stats.get("offsides", 0),
            }
        },
        "minute": raw_stats.get("minute", 0)
    }


def fetch_complete_match_data(game_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch all match data: details, lineups, and statistics in one go.
    """
    result = {
        "game_id": game_id,
        "details": None,
        "lineups": None,
        "statistics": None
    }
    
    # Fetch details
    details = fetch_game_details(game_id)
    if details:
        result["details"] = details
    
    # Fetch lineups
    lineups = fetch_lineups(game_id)
    if lineups:
        result["lineups"] = lineups
    
    # Fetch statistics
    stats = fetch_statistics(game_id)
    if stats:
        result["statistics"] = stats
    
    return result