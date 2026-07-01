"""
Forwards updates from poller to Rust backend API.
Handles: fixtures, live updates, events, commentary, lineups, statistics, finalization, notifications.
"""
from __future__ import annotations

import logging
import requests
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("worldcup_poller.forwarder")


class Forwarder:
    def __init__(self, api_url: str, timeout: int = 30, max_retries: int = 3):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        
        # Create session with retry logic
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WorldCupPoller/1.0",
        })
        
        # Retry strategy for transient failures
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST", "PUT", "GET", "DELETE"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _post(self, endpoint: str, data: Dict[str, Any]) -> bool:
        """Generic POST request with error handling."""
        url = f"{self.api_url}{endpoint}"
        try:
            response = self.session.post(url, json=data, timeout=self.timeout)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to POST to {endpoint}: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response: {e.response.text[:500]}")
            return False

    def _put(self, endpoint: str, data: Dict[str, Any]) -> bool:
        """Generic PUT request with error handling."""
        url = f"{self.api_url}{endpoint}"
        try:
            response = self.session.put(url, json=data, timeout=self.timeout)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to PUT to {endpoint}: {e}")
            return False

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Generic GET request with error handling."""
        url = f"{self.api_url}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to GET from {endpoint}: {e}")
            return None

    # ============================================================
    # FIXTURE MANAGEMENT
    # ============================================================

    def forward_fixture(self, fixture: Dict[str, Any]) -> bool:
        """
        Forward a single fixture to the Rust API.
        """
        return self._post("/games", fixture)

    def forward_fixtures_bulk(self, fixtures: List[Dict[str, Any]]) -> bool:
        """
        Forward multiple fixtures in bulk.
        """
        return self._post("/games/bulk", {"fixtures": fixtures})

    # ============================================================
    # LIVE UPDATES
    # ============================================================

    def forward_live_update(self, update: Dict[str, Any]) -> bool:
        """
        Forward a live match update to the Rust API.
        Expected payload:
        {
            "fixture_id": "wc26_123",
            "event_type": "live_update|score|status|goal|card|substitution",
            "home_score": 1,
            "away_score": 0,
            "minute": 67,
            "minute_display": "67'",
            "status": "live|completed",
            "is_live": true,
            "available_for_voting": false,
            "scorer": "home_team|away_team",
            "player": "Player Name",
            "assist": "Assist Name",
            "team": "home|away"
        }
        """
        # Add timestamp if not present
        if "timestamp" not in update:
            update["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        return self._post("/games/live-update", update)

    def forward_score_update(self, match_id: str, home_score: int, away_score: int, minute: int) -> bool:
        """
        Forward a score update.
        """
        payload = {
            "fixture_id": match_id,
            "event_type": "score",
            "home_score": home_score,
            "away_score": away_score,
            "minute": minute,
        }
        return self._post("/games/score", payload)

    def forward_status_update(self, match_id: str, status: str, is_live: bool, available_for_voting: bool) -> bool:
        """
        Forward a status update.
        """
        payload = {
            "fixture_id": match_id,
            "status": status,
            "is_live": is_live,
            "available_for_voting": available_for_voting,
        }
        return self._post("/games/status", payload)

    # ============================================================
    # EVENTS (Goals, Cards, Substitutions)
    # ============================================================

    def forward_event(self, event: Dict[str, Any]) -> bool:
        """
        Forward a single event to the Rust API.
        Expected payload:
        {
            "fixture_id": "wc26_123",
            "event_type": "goal|yellow_card|red_card|substitution|penalty|own_goal",
            "minute": 23,
            "team": "home|away",
            "player": "Player Name",
            "assist": "Assist Name (optional)",
            "home_score": 1,
            "away_score": 0,
        }
        """
        return self._post("/games/events", event)

    def forward_bulk_events(self, bulk: Dict[str, Any]) -> bool:
        """
        Forward multiple events at once.
        Expected payload:
        {
            "fixture_id": "wc26_123",
            "events": [
                {
                    "event_type": "goal",
                    "minute": 23,
                    "team": "home",
                    "player": "Player Name",
                    ...
                }
            ]
        }
        """
        return self._post("/games/events/bulk", bulk)

    def forward_event_batch(self, fixture_id: str, events: List[Dict[str, Any]]) -> bool:
        """
        Forward a batch of events for a fixture.
        """
        return self._post(f"/games/{fixture_id}/events/batch", {"events": events})

    # ============================================================
    # COMMENTARY
    # ============================================================

    def forward_commentary(self, commentary: Dict[str, Any]) -> bool:
        """
        Forward commentary to the Rust API.
        Expected payload:
        {
            "match_id": "wc26_123",
            "entry": {
                "minute": 23,
                "text": "Great goal by Player!",
                "type": "goal|chance|card|substitution|general",
                "team": "home|away",
                "player": "Player Name",
                "created_at": "2026-06-27T15:00:00Z"
            }
        }
        """
        return self._post("/games/commentary", commentary)

    def forward_commentary_bulk(self, fixture_id: str, entries: List[Dict[str, Any]]) -> bool:
        """
        Forward multiple commentary entries for a fixture.
        """
        payload = {
            "match_id": fixture_id,
            "entries": entries
        }
        return self._post("/games/commentary/bulk", payload)

    # ============================================================
    # LINEUPS - FIXED
    # ============================================================

    def forward_lineups(self, lineups: Dict[str, Any]) -> bool:
        """
        Forward lineups to the Rust API.
        """
        # Convert to exact Rust API format
        payload = {
            "fixtureId": lineups.get("fixture_id"),
            "homeTeam": lineups.get("home_team"),
            "awayTeam": lineups.get("away_team"),
            "lineups": {
                "home": self._convert_lineup_side(lineups.get("lineups", {}).get("home", {})),
                "away": self._convert_lineup_side(lineups.get("lineups", {}).get("away", {}))
            }
        }
        return self._post("/games/lineups", payload)
    
    def _convert_lineup_side(self, side: Dict[str, Any]) -> Dict[str, Any]:
        """Convert lineup side to Rust API format."""
        return {
            "formation": side.get("formation", "4-4-2"),
            "coach": {"name": side.get("coach", {}).get("name", "Unknown")},
            "players": self._convert_players(side.get("players", [])),
            "bench": self._convert_players(side.get("bench", []))
        }
    
    def _convert_players(self, players: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert players to Rust API format."""
        return [{
            "name": p.get("name", "Unknown"),
            "position": p.get("position", "Unknown"),
            "jerseyNumber": p.get("jerseyNumber", 0),
            "captain": p.get("captain", False),
            "lineup": p.get("lineup", "starting"),
            "playerId": p.get("playerId")
        } for p in players]

    def forward_lineups_simplified(self, fixture_id: str, home_players: List[Dict], away_players: List[Dict]) -> bool:
        """
        Forward simplified lineups (just starting XI).
        """
        payload = {
            "fixture_id": fixture_id,
            "home": home_players,
            "away": away_players,
        }
        return self._post("/games/lineups/simplified", payload)

    # ============================================================
    # STATISTICS - FIXED
    # ============================================================

    def forward_statistics(self, statistics: Dict[str, Any]) -> bool:
        """
        Forward match statistics to the Rust API.
        """
        payload = {
            "fixture_id": statistics.get("fixture_id"),
            "minute": statistics.get("minute", 0),
            "statistics": {
                "home": self._convert_statistics_side(statistics.get("statistics", {}).get("home", {})),
                "away": self._convert_statistics_side(statistics.get("statistics", {}).get("away", {}))
            }
        }
        return self._post("/games/statistics", payload)
    
    def _convert_statistics_side(self, side: Dict[str, Any]) -> Dict[str, Any]:
        """Convert statistics to Rust API format."""
        return {
            "possession": side.get("possession"),
            "shots": side.get("shots"),
            "shots_on_target": side.get("shots_on_target"),
            "shots_off_target": side.get("shots_off_target"),
            "corners": side.get("corners"),
            "fouls": side.get("fouls"),
            "yellow_cards": side.get("yellow_cards"),
            "red_cards": side.get("red_cards"),
            "offsides": side.get("offsides"),
            "passes": side.get("passes"),
            "pass_accuracy": side.get("pass_accuracy")
        }

    def forward_statistics_bulk(self, stats_bulk: Dict[str, Any]) -> bool:
        """
        Forward multiple statistics snapshots at once.
        """
        snapshots = stats_bulk.get("snapshots", [])
        converted_snapshots = []
        for snapshot in snapshots:
            converted_snapshots.append({
                "minute": snapshot.get("minute", 0),
                "statistics": {
                    "home": self._convert_statistics_side(snapshot.get("statistics", {}).get("home", {})),
                    "away": self._convert_statistics_side(snapshot.get("statistics", {}).get("away", {}))
                }
            })
        
        payload = {
            "fixture_id": stats_bulk.get("fixture_id"),
            "snapshots": converted_snapshots
        }
        return self._post("/games/statistics/bulk", payload)

    def forward_statistics_snapshot(self, fixture_id: str, minute: int, stats: Dict[str, Any]) -> bool:
        """
        Forward a single statistics snapshot.
        """
        payload = {
            "fixture_id": fixture_id,
            "minute": minute,
            "statistics": {
                "home": self._convert_statistics_side(stats.get("home", {})),
                "away": self._convert_statistics_side(stats.get("away", {}))
            }
        }
        return self._post("/games/statistics/snapshot", payload)

    # ============================================================
    # MATCH FINALIZATION
    # ============================================================

    def finalize_match(self, finalize_data: Dict[str, Any]) -> bool:
        """
        Finalize match result.
        """
        return self._post("/games/finalize", finalize_data)

    def forward_match_result(self, fixture_id: str, result: str, home_score: int, away_score: int) -> bool:
        """
        Forward just the match result.
        """
        payload = {
            "fixture_id": fixture_id,
            "result": result,
            "home_score": home_score,
            "away_score": away_score,
        }
        return self._post("/games/result", payload)

    def move_to_history(self, fixture_id: str) -> bool:
        """
        Move a completed match to history.
        """
        return self._post(f"/games/{fixture_id}/move-to-history", {})

    # ============================================================
    # NOTIFICATIONS
    # ============================================================

    def forward_notification(self, notification: Dict[str, Any]) -> bool:
        """
        Forward a notification to the Rust API.
        """
        return self._post("/games/notify", notification)

    def forward_lineups_available_notification(self, fixture_id: str, home_team: str, away_team: str) -> bool:
        """
        Send notification that lineups are available.
        """
        payload = {
            "fixture_id": fixture_id,
            "event_type": "lineups_available",
            "title": f"📋 Lineups are out! {home_team} vs {away_team}",
            "body": f"Check the starting XI for {home_team} vs {away_team}.",
            "data": {
                "home_team": home_team,
                "away_team": away_team,
                "type": "lineups_available"
            }
        }
        return self._post("/games/notify", payload)

    def forward_match_live_notification(self, fixture_id: str, home_team: str, away_team: str) -> bool:
        """
        Send notification that match is live.
        """
        payload = {
            "fixture_id": fixture_id,
            "event_type": "match_live",
            "title": f"⚽ {home_team} vs {away_team} is LIVE!",
            "body": f"The match has kicked off! Follow the action.",
            "data": {
                "home_team": home_team,
                "away_team": away_team,
                "type": "match_live"
            }
        }
        return self._post("/games/notify", payload)

    def forward_goal_notification(self, fixture_id: str, scorer: str, minute: int, home_score: int, away_score: int) -> bool:
        """
        Send notification that a goal was scored.
        """
        payload = {
            "fixture_id": fixture_id,
            "event_type": "goal_scored",
            "title": f"⚽ GOAL! {scorer} scores!",
            "body": f"{scorer} scores at {minute}'! Score: {home_score}-{away_score}",
            "data": {
                "scorer": scorer,
                "minute": minute,
                "home_score": home_score,
                "away_score": away_score,
                "type": "goal_scored"
            }
        }
        return self._post("/games/notify", payload)

    def forward_match_ended_notification(self, fixture_id: str, home_team: str, away_team: str, result: str) -> bool:
        """
        Send notification that match has ended.
        """
        payload = {
            "fixture_id": fixture_id,
            "event_type": "match_ended",
            "title": f"🏁 Full Time: {home_team} vs {away_team}",
            "body": f"Match ended. Result: {result}",
            "data": {
                "home_team": home_team,
                "away_team": away_team,
                "result": result,
                "type": "match_ended"
            }
        }
        return self._post("/games/notify", payload)

    # ============================================================
    # GAME MANAGEMENT
    # ============================================================

    def get_game(self, match_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a game by match_id from the Rust API.
        """
        return self._get(f"/games/match/{match_id}")

    def get_live_games(self) -> Optional[List[Dict[str, Any]]]:
        """
        Get all live games from the Rust API.
        """
        return self._get("/games/live")

    def get_upcoming_games(self) -> Optional[List[Dict[str, Any]]]:
        """
        Get all upcoming games from the Rust API.
        """
        return self._get("/games/upcoming")

    def get_history_games(self, limit: int = 50, skip: int = 0) -> Optional[List[Dict[str, Any]]]:
        """
        Get history games from the Rust API.
        """
        return self._get("/games/history", {"limit": limit, "skip": skip})

    # ============================================================
    # BULK SYNC
    # ============================================================

    def sync_fixtures(self, fixtures: List[Dict[str, Any]]) -> bool:
        """
        Sync all fixtures at once (full update).
        """
        return self._post("/games/sync", {"fixtures": fixtures})

    def sync_live_data(self, live_data: Dict[str, Any]) -> bool:
        """
        Sync live data for multiple matches at once.
        """
        return self._post("/games/sync/live", live_data)

    # ============================================================
    # HEALTH CHECK
    # ============================================================

    def health_check(self) -> bool:
        """
        Check if the Rust API is healthy.
        """
        result = self._get("/health")
        return result is not None and result.get("status") == "healthy"

    def ping(self) -> bool:
        """
        Simple ping to check API availability.
        """
        try:
            response = self.session.get(f"{self.api_url}/ping", timeout=5)
            return response.status_code == 200
        except:
            return False


# ============================================================
# FACTORY FUNCTION
# ============================================================

def create_forwarder(api_url: str = None, **kwargs) -> Forwarder:
    """
    Create a Forwarder instance with optional configuration.
    """
    import os
    if api_url is None:
        api_url = os.environ.get("FANCLASH_API", "https://clash-api-m5mr.onrender.com/api")
    return Forwarder(api_url, **kwargs)