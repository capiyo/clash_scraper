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


import re

# All keywords are matched with regex word boundaries (\b), never plain
# substring containment -- naive "in" checks cause false positives like
# "pen" matching inside "suspended", or "ended" matching inside
# "suspended" too. \b works fine for multi-word phrases like "half time"
# since spaces are already non-word characters.
HALFTIME_STATUS_KEYWORDS = ("ht", "half time", "halftime")
STOPPED_STATUS_KEYWORDS = ("stopped", "suspended", "interrupted", "delayed", "abandoned")
FULLTIME_STATUS_KEYWORDS = ("ft", "aet", "pen", "ended", "finished", "full-time", "full time", "penalties")


def _matches(text: str, keywords: tuple) -> bool:
    return any(re.search(r"\b" + re.escape(kw) + r"\b", text) for kw in keywords)


def classify_match_phase(status_text: Optional[str]) -> Optional[str]:
    """
    Classify a 365Scores statusText into one of the three moments we care
    about for statistics snapshots: "halftime", "stopped", "fulltime".
    Returns None if the match is in open play (or status is unknown).
    """
    text = (status_text or "").strip().lower()
    if not text:
        return None
    if _matches(text, FULLTIME_STATUS_KEYWORDS):
        return "fulltime"
    if _matches(text, HALFTIME_STATUS_KEYWORDS):
        return "halftime"
    if _matches(text, STOPPED_STATUS_KEYWORDS):
        return "stopped"
    return None


def extract_statistics_from_game(game: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the statistics payload from an already-fetched `game` object.
    Lets callers that already hold a `game` dict (e.g. the poller's live
    loop) skip a redundant fetch_game_details() call.
    """
    return {
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
        "minute": int(game.get("gameTime", 0) or 0),
        "status_text": game.get("statusText"),
    }


def fetch_statistics(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int
) -> Optional[Dict[str, Any]]:
    """
    Fetch statistics from the game details endpoint.

    NOTE: this makes its own fetch_game_details() call. If you already
    have a `game` object on hand, prefer extract_statistics_from_game(game)
    to avoid a redundant network request.
    """
    data = fetch_game_details(game_id, away_id, home_id, competition_id)
    
    if not data or "game" not in data:
        return None
    
    game = data.get("game", {})
    return extract_statistics_from_game(game)


def fetch_commentary(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int
) -> List[Dict[str, Any]]:
    """
    Fetch commentary via 365Scores' separate play-by-play feed
    (pbpgenerator.365scores.com). The /web/game/ endpoint does NOT embed
    commentary text directly -- game.commentary is not a real field. It
    only returns game.playByPlay.feedURL, a pointer to this separate feed.

    Two things the raw feed gets wrong that we correct here:
      1. The feedURL 365Scores returns comes pre-built with lang=37
         (Dutch), not English -- every other endpoint in this file uses
         langId=1, so we force lang=1 here too before fetching.
      2. Entry field names are PascalCase (.NET-style): Comment, Timeline,
         Type, TypeName, Period, Title, IsMajor, Players -- not the
         lowercase minute/text/type/team/player shape used elsewhere.
         Kickoff / half-end markers have no "Comment" field -- they use
         "Title" instead (e.g. "Rust 0-0" / half-time score) -- so we
         fall back to Title.

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
    pbp = game.get("playByPlay") or {}
    feed_url = pbp.get("feedURL")

    if not feed_url:
        logger.debug(f"No playByPlay feedURL for {game_id}")
        return []

    # Force English -- 365Scores returns lang=37 (Dutch) by default here.
    feed_url = re.sub(r"lang=\d+", "lang=1", feed_url)

    try:
        response = requests.get(feed_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        raw = response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch play-by-play feed for {game_id}: {e}")
        return []
    except ValueError as e:
        logger.error(f"Failed to parse play-by-play JSON for {game_id}: {e}")
        return []

    # We don't rely on knowing the exact wrapper key name -- find the
    # first top-level list of dicts in the response instead.
    raw_commentary = []
    if isinstance(raw, list):
        raw_commentary = raw
    elif isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                raw_commentary = value
                break

    if not raw_commentary:
        logger.debug(f"No commentary entries in play-by-play feed for {game_id}")
        return []

    commentary_list = []
    for entry in raw_commentary:
        minute_raw = entry.get("Timeline")
        try:
            minute = int(minute_raw) if minute_raw is not None else 0
        except (ValueError, TypeError):
            minute = 0

        text = entry.get("Comment") or entry.get("Title") or ""

        players = entry.get("Players") or []
        player = players[0].get("PlayerName") if players else None

        commentary_list.append({
            "minute": minute,
            "text": text,
            "type": entry.get("TypeName", "commentary"),
            "team": None,  # not directly present -- CompetitorNum(1/2) could be mapped to team name if needed later
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
            "minute": int(game.get("gameTime", 0) or 0)
        },
        "commentary": game.get("commentary", []),
        "score": {
            "home": game.get("homeCompetitor", {}).get("score", 0),
            "away": game.get("awayCompetitor", {}).get("score", 0),
        },
        "status": game.get("statusText"),
        "time_elapsed": int(game.get("gameTime", 0) or 0),
        "is_finished": is_game_finished(game),
    }