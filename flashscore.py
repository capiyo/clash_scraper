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


# ============================================================
# HELPERS: key casing + safe numeric coercion
# ============================================================

def _snake_to_camel(key: str) -> str:
    """Convert a snake_case key to camelCase."""
    parts = key.split("_")
    if len(parts) == 1:
        return key
    return parts[0] + "".join(p.title() for p in parts[1:])


def _camelize_keys(obj: Any) -> Any:
    """
    Recursively convert all dict keys from snake_case to camelCase.
    Lists and nested dicts are handled recursively. Non-dict/list values
    are returned unchanged.
    """
    if isinstance(obj, dict):
        return {_snake_to_camel(k): _camelize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camelize_keys(item) for item in obj]
    return obj


# Fields that the Rust API expects as integers (i32 / Option<i32>).
# Anything matching these keys (snake_case OR camelCase form) will be
# safely coerced to int when possible, to avoid 422s from floats like 1.0.
_INT_FIELDS = {
    "home_score", "homeScore",
    "away_score", "awayScore",
    "minute",
    "shots", "shots_on_target", "shotsOnTarget",
    "shots_off_target", "shotsOffTarget",
    "corners", "fouls",
    "yellow_cards", "yellowCards",
    "red_cards", "redCards",
    "offsides", "passes",
    "possession", "pass_accuracy", "passAccuracy",
    "jersey_number", "jerseyNumber",
}


def _coerce_ints(obj: Any) -> Any:
    """
    Recursively walk a dict/list structure and coerce any value whose key
    is in _INT_FIELDS to a real int (e.g. 1.0 -> 1), leaving None untouched.
    Safe no-op if the value can't be cleanly converted.
    """
    if isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                new_obj[k] = _coerce_ints(v)
            elif k in _INT_FIELDS and v is not None:
                try:
                    new_obj[k] = int(v)
                except (TypeError, ValueError):
                    new_obj[k] = v
            else:
                new_obj[k] = v
        return new_obj
    if isinstance(obj, list):
        return [_coerce_ints(item) for item in obj]
    return obj


def _prep_camel(data: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce ints first, then camelCase the keys. Used for endpoints
    whose Rust structs expect camelCase JSON (live-update, lineups)."""
    return _camelize_keys(_coerce_ints(data))


def _prep_snake(data: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce ints only, keep snake_case keys as-is. Used for endpoints
    whose Rust structs expect snake_case JSON (statistics)."""
    return _coerce_ints(data)


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
            if hasattr(e, 'response') and e.response is not None:
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
        """Forward a single fixture to the Rust API."""
        return self._post("/games", _prep_camel(fixture))

    def forward_fixtures_bulk(self, fixtures: List[Dict[str, Any]]) -> bool:
        """Forward multiple fixtures in bulk."""
        return self._post("/games/bulk", _prep_camel({"fixtures": fixtures}))

    # ============================================================
    # LIVE UPDATES
    # ============================================================

    def forward_live_update(self, update: Dict[str, Any]) -> bool:
        """
        Forward a live match update to the Rust API.
        Rust expects camelCase keys (fixtureId, homeScore, etc.) and
        integer scores/minutes, so we coerce + camelCase before sending.
        """
        if "timestamp" not in update:
            update["timestamp"] = datetime.now(timezone.utc).isoformat()
        return self._post("/games/live-update", _prep_camel(update))

    def forward_score_update(self, match_id: str, home_score: int, away_score: int, minute: int) -> bool:
        """
        Forward a score update.
        NOTE: the Rust handler (UpdateGameScoreRequest) requires "matchId"
        (not "fixtureId") and has no minute/event_type field at all — those
        two are sent for logging/back-compat but will be ignored server-side.
        """
        payload = {
            "match_id": match_id,
            "event_type": "score",
            "home_score": home_score,
            "away_score": away_score,
            "minute": minute,
        }
        return self._post("/games/score", _prep_camel(payload))

    def forward_status_update(self, match_id: str, status: str, is_live: bool, available_for_voting: bool) -> bool:
        """Forward a status update."""
        payload = {
            "fixture_id": match_id,
            "status": status,
            "is_live": is_live,
            "available_for_voting": available_for_voting,
        }
        return self._post("/games/status", _prep_camel(payload))

    # ============================================================
    # EVENTS (Goals, Cards, Substitutions)
    # ============================================================

    def forward_event(self, event: Dict[str, Any]) -> bool:
        """Forward a single event to the Rust API."""
        return self._post("/games/events", _prep_camel(event))

    def forward_bulk_events(self, bulk: Dict[str, Any]) -> bool:
        """Forward multiple events at once."""
        return self._post("/games/events/bulk", _prep_camel(bulk))

    def forward_event_batch(self, fixture_id: str, events: List[Dict[str, Any]]) -> bool:
        """Forward a batch of events for a fixture."""
        return self._post(f"/games/{fixture_id}/events/batch", _prep_camel({"events": events}))

    # ============================================================
    # COMMENTARY
    # ============================================================

    def forward_commentary(self, commentary: Dict[str, Any]) -> bool:
        """Forward commentary to the Rust API."""
        return self._post("/games/commentary", _prep_camel(commentary))

    def forward_commentary_bulk(self, fixture_id: str, entries: List[Dict[str, Any]]) -> bool:
        """Forward multiple commentary entries for a fixture."""
        payload = {
            "match_id": fixture_id,
            "entries": entries
        }
        return self._post("/games/commentary/bulk", _prep_camel(payload))

    # ============================================================
    # LINEUPS
    # ============================================================

    def forward_lineups(self, lineups: Dict[str, Any]) -> bool:
        """
        Forward lineups to the Rust API.
        Rust expects camelCase keys (jerseyNumber, playerId, etc.).
        """
        return self._post("/games/lineups", _prep_camel(lineups))

    def forward_lineups_simplified(self, fixture_id: str, home_players: List[Dict], away_players: List[Dict]) -> bool:
        """Forward simplified lineups (just starting XI)."""
        payload = {
            "fixture_id": fixture_id,
            "home": home_players,
            "away": away_players,
        }
        return self._post("/games/lineups/simplified", _prep_camel(payload))

    # ============================================================
    # STATISTICS
    # (Rust structs here use plain snake_case fields, no rename attrs —
    # so we only coerce ints, we do NOT camelCase these.)
    # ============================================================

    def forward_statistics(self, statistics: Dict[str, Any]) -> bool:
        """Forward match statistics to the Rust API."""
        return self._post("/games/statistics", _prep_snake(statistics))

    def forward_statistics_bulk(self, stats_bulk: Dict[str, Any]) -> bool:
        """Forward multiple statistics snapshots at once."""
        return self._post("/games/statistics/bulk", _prep_snake(stats_bulk))

    def forward_statistics_snapshot(self, fixture_id: str, minute: int, stats: Dict[str, Any]) -> bool:
        """Forward a single statistics snapshot."""
        payload = {
            "fixture_id": fixture_id,
            "minute": minute,
            "statistics": stats,
        }
        return self._post("/games/statistics/snapshot", _prep_snake(payload))

    # ============================================================
    # MATCH FINALIZATION
    # ============================================================

    def finalize_match(self, finalize_data: Dict[str, Any]) -> bool:
        """Finalize match result."""
        return self._post("/games/finalize", _prep_camel(finalize_data))

    def forward_match_result(self, fixture_id: str, result: str, home_score: int, away_score: int) -> bool:
        """Forward just the match result."""
        payload = {
            "fixture_id": fixture_id,
            "result": result,
            "home_score": home_score,
            "away_score": away_score,
        }
        return self._post("/games/result", _prep_camel(payload))

    def move_to_history(self, fixture_id: str) -> bool:
        """Move a completed match to history."""
        return self._post(f"/games/{fixture_id}/move-to-history", {})

    # ============================================================
    # NOTIFICATIONS
    # ============================================================

    def forward_notification(self, notification: Dict[str, Any]) -> bool:
        """Forward a notification to the Rust API."""
        return self._post("/games/notify", _prep_camel(notification))

    def forward_lineups_available_notification(self, fixture_id: str, home_team: str, away_team: str) -> bool:
        """Send notification that lineups are available."""
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
        return self._post("/games/notify", _prep_camel(payload))

    def forward_match_live_notification(self, fixture_id: str, home_team: str, away_team: str) -> bool:
        """Send notification that match is live."""
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
        return self._post("/games/notify", _prep_camel(payload))

    def forward_goal_notification(self, fixture_id: str, scorer: str, minute: int, home_score: int, away_score: int) -> bool:
        """Send notification that a goal was scored."""
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
        return self._post("/games/notify", _prep_camel(payload))

    def forward_match_ended_notification(self, fixture_id: str, home_team: str, away_team: str, result: str) -> bool:
        """Send notification that match has ended."""
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
        return self._post("/games/notify", _prep_camel(payload))

    # ============================================================
    # GAME MANAGEMENT
    # ============================================================

    def get_game(self, match_id: str) -> Optional[Dict[str, Any]]:
        """Get a game by match_id from the Rust API."""
        return self._get(f"/games/match/{match_id}")

    def get_live_games(self) -> Optional[List[Dict[str, Any]]]:
        """Get all live games from the Rust API."""
        return self._get("/games/live")

    def get_upcoming_games(self) -> Optional[List[Dict[str, Any]]]:
        """Get all upcoming games from the Rust API."""
        return self._get("/games/upcoming")

    def get_history_games(self, limit: int = 50, skip: int = 0) -> Optional[List[Dict[str, Any]]]:
        """Get history games from the Rust API."""
        return self._get("/games/history", {"limit": limit, "skip": skip})

    # ============================================================
    # BULK SYNC
    # ============================================================

    def sync_fixtures(self, fixtures: List[Dict[str, Any]]) -> bool:
        """Sync all fixtures at once (full update)."""
        return self._post("/games/sync", _prep_camel({"fixtures": fixtures}))

    def sync_live_data(self, live_data: Dict[str, Any]) -> bool:
        """Sync live data for multiple matches at once."""
        return self._post("/games/sync/live", _prep_camel(live_data))

    # ============================================================
    # HEALTH CHECK
    # ============================================================

    def health_check(self) -> bool:
        """Check if the Rust API is healthy."""
        result = self._get("/health")
        return result is not None and result.get("status") == "healthy"

    def ping(self) -> bool:
        """Simple ping to check API availability."""
        try:
            response = self.session.get(f"{self.api_url}/ping", timeout=5)
            return response.status_code == 200
        except Exception:
            return False


# ============================================================
# FACTORY FUNCTION
# ============================================================

def create_forwarder(api_url: str = None, **kwargs) -> Forwarder:
    """Create a Forwarder instance with optional configuration."""
    import os
    if api_url is None:
        api_url = os.environ.get("FANCLASH_API", "https://clash-api-m5mr.onrender.com/api")
    return Forwarder(api_url, **kwargs)