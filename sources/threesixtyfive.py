"""
365Scores API client for World Cup data.
Fetches: fixtures, live scores, events, lineups, statistics, and commentary.
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


def is_game_finished(game: Dict[str, Any]) -> bool:
    """
    Determine if a game has finished.

    365Scores uses "Ended" as the primary status text for completed games.
    Other possible values: "Finished", "FT", "Full Time", "AET", "Pen"

    Signals checked, in order:
      1. game.chartEvents.statuses[0].isFinished -- explicit bool
      2. game.justEnded -- fires the moment a match ends
      3. game.statusText -- confirmed 365Scores value is "Ended"
      4. game.gameTime >= 90 with no extra time
    """
    # Check 1: Explicit isFinished flag
    try:
        statuses = (game.get("chartEvents") or {}).get("statuses") or []
        if statuses and "isFinished" in statuses[0]:
            return bool(statuses[0]["isFinished"])
    except (AttributeError, IndexError, TypeError):
        pass

    # Check 2: justEnded flag
    if game.get("justEnded"):
        return True

    # Check 3: statusText - 365Scores uses "Ended"
    status_text = (game.get("statusText") or "").strip().lower()
    finished_keywords = ["ended", "finished", "ft", "full-time", "aet", "pen", "penalties"]
    if status_text in finished_keywords:
        return True

    # Check 4: Time-based fallback - if gameTime >= 90 and not extra time
    time_elapsed = game.get("gameTime", 0)
    if time_elapsed >= 90:
        # Don't mark if it's half time or extra time
        if "half" not in status_text and "extra" not in status_text:
            # Also check if we have a winner (both scores set)
            home_comp = game.get("homeCompetitor", {})
            away_comp = game.get("awayCompetitor", {})
            if home_comp.get("score") is not None and away_comp.get("score") is not None:
                return True

    # Check 5: If game has ended but statusText contains "ended" in any form
    if "ended" in status_text:
        return True

    return False


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
    lang_id: int = 1,
    user_country_id: int = 413
) -> Optional[Dict[str, Any]]:
    """
    Fetch full game details including lineups using the /web/game/ endpoint.
    
    Args:
        game_id: 365Scores game ID (e.g., "4627864")
        away_id: Away team competitor ID
        home_id: Home team competitor ID
        competition_id: Competition ID (e.g., 5930)
        lang_id: Language ID (1 = English)
        user_country_id: Country ID (413 = Kenya)
    
    Returns:
        Full game data including lineups, statistics, events, commentary
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

    # Player names live in a separate top-level "members" array on the
    # game object, keyed by the same "id" used inside lineups.members[].
    # The lineup entries themselves never include a name field, so we
    # have to join them here.
    roster = {m["id"]: m for m in game.get("members", []) if "id" in m}

    def _attach_names(lineup: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not lineup:
            return {}
        for player in lineup.get("members", []):
            info = roster.get(player.get("id"))
            if info:
                player["name"] = info.get("name")
                player["shortName"] = info.get("shortName")
                player["athleteId"] = info.get("athleteId")
        return lineup

    home_lineups = _attach_names(home_lineups)
    away_lineups = _attach_names(away_lineups)

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
            "shots_off_target": game.get("homeShotsOffTarget"),
            "corners": game.get("homeCorners"),
            "fouls": game.get("homeFouls"),
            "yellow_cards": game.get("homeYellowCards"),
            "red_cards": game.get("homeRedCards"),
            "offsides": game.get("homeOffsides"),
            "passes": game.get("homePasses"),
            "pass_accuracy": game.get("homePassAccuracy"),
        },
        "away": {
            "possession": game.get("awayPossession"),
            "shots": game.get("awayShots"),
            "shots_on_target": game.get("awayShotsOnTarget"),
            "shots_off_target": game.get("awayShotsOffTarget"),
            "corners": game.get("awayCorners"),
            "fouls": game.get("awayFouls"),
            "yellow_cards": game.get("awayYellowCards"),
            "red_cards": game.get("awayRedCards"),
            "offsides": game.get("awayOffsides"),
            "passes": game.get("awayPasses"),
            "pass_accuracy": game.get("awayPassAccuracy"),
        },
        "minute": game.get("gameTime", 0)
    }
    
    return stats


def fetch_commentary(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int
) -> List[Dict[str, Any]]:
    """
    Fetch commentary from the game details endpoint.
    
    Returns:
        List of commentary entries with:
        {
            "minute": int,
            "text": str,
            "type": str,
            "team": Optional[str],
            "player": Optional[str],
        }
        Note: createdAt is added by the poller when forwarding.
    """
    data = fetch_game_details(game_id, away_id, home_id, competition_id)
    
    if not data or "game" not in data:
        return []
    
    game = data.get("game", {})
    raw_commentary = game.get("commentary", [])
    
    if not raw_commentary:
        logger.debug(f"No commentary available for {game_id}")
        return []
    
    commentary_list = []
    for entry in raw_commentary:
        # Extract minute from the entry
        minute = entry.get("minute", 0)
        if isinstance(minute, str):
            try:
                minute = int(minute)
            except (ValueError, TypeError):
                minute = 0
        
        # Determine event type
        event_type = entry.get("type", "commentary")
        
        # Extract team and player info
        team = None
        if entry.get("team") and isinstance(entry.get("team"), dict):
            team = entry.get("team", {}).get("name")
        
        player = None
        if entry.get("player") and isinstance(entry.get("player"), dict):
            player = entry.get("player", {}).get("name")
        
        commentary_list.append({
            "minute": minute,
            "text": entry.get("text", ""),
            "type": event_type,
            "team": team,
            "player": player,
        })
    
    logger.info(f"fetch_commentary({game_id}): Found {len(commentary_list)} entries")
    return commentary_list


def fetch_complete_match_data(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int
) -> Optional[Dict[str, Any]]:
    """
    Fetch all match data: details, lineups, statistics, and commentary in one go.
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
                "shots_off_target": game.get("homeShotsOffTarget"),
                "corners": game.get("homeCorners"),
                "fouls": game.get("homeFouls"),
                "yellow_cards": game.get("homeYellowCards"),
                "red_cards": game.get("homeRedCards"),
                "offsides": game.get("homeOffsides"),
                "passes": game.get("homePasses"),
                "pass_accuracy": game.get("homePassAccuracy"),
            },
            "away": {
                "possession": game.get("awayPossession"),
                "shots": game.get("awayShots"),
                "shots_on_target": game.get("awayShotsOnTarget"),
                "shots_off_target": game.get("awayShotsOffTarget"),
                "corners": game.get("awayCorners"),
                "fouls": game.get("awayFouls"),
                "yellow_cards": game.get("awayYellowCards"),
                "red_cards": game.get("awayRedCards"),
                "offsides": game.get("awayOffsides"),
                "passes": game.get("awayPasses"),
                "pass_accuracy": game.get("awayPassAccuracy"),
            },
            "minute": game.get("gameTime", 0)
        },
        "commentary": game.get("commentary", []),
        "score": {
            "home": game.get("homeCompetitor", {}).get("score", 0),
            "away": game.get("awayCompetitor", {}).get("score", 0),
        },
        "status": game.get("statusText"),
        "time_elapsed": game.get("gameTime", 0),
        "is_finished": is_game_finished(game),
    }