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
        
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WorldCupPoller/1.0",
        })
        
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
        url = f"{self.api_url}{endpoint}"
        try:
            response = self.session.put(url, json=data, timeout=self.timeout)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to PUT to {endpoint}: {e}")
            return False

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
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
        return self._post("/games", fixture)

    def forward_fixtures_bulk(self, fixtures: List[Dict[str, Any]]) -> bool:
        return self._post("/games/bulk", {"fixtures": fixtures})

    # ============================================================
    # LIVE UPDATES - FIXED
    # ============================================================

    def forward_live_update(self, update: Dict[str, Any]) -> bool:
       if "timestamp" not in update:
        update["timestamp"] = datetime.now(timezone.utc).isoformat()
       payload = {
        "fixtureId": update.get("fixture_id"),
        "eventType": update.get("event_type"),
        "homeScore": update.get("home_score", 0),
        "awayScore": update.get("away_score", 0),
        "minute": update.get("minute", 0),
        "minuteDisplay": update.get("minute_display"),
        "status": update.get("status"),
        "isLive": update.get("is_live"),
        "availableForVoting": update.get("available_for_voting"),
        "scorer": update.get("scorer"),
        "player": update.get("player"),
        "assist": update.get("assist"),
        "team": update.get("team"),
        "timestamp": update.get("timestamp"),
    }
       return self._post("/games/live-update", payload)

def forward_commentary(self, commentary: Dict[str, Any]) -> bool:
    entry = dict(commentary.get("entry", {}))
    if "created_at" in entry:
        entry["createdAt"] = entry.pop("created_at")
    entry.setdefault("createdAt", datetime.now(timezone.utc).isoformat())
    if "event_type" in entry:
        entry["type"] = entry.pop("event_type")
    payload = {"match_id": commentary.get("match_id"), "entry": entry}
    return self._post("/games/commentary", payload)

    def forward_score_update(self, match_id: str, home_score: int, away_score: int, minute: int) -> bool:
        payload = {
            "fixture_id": match_id,
            "event_type": "score",
            "home_score": home_score,
            "away_score": away_score,
            "minute": minute,
        }
        return self._post("/games/score", payload)

    def forward_status_update(self, match_id: str, status: str, is_live: bool, available_for_voting: bool) -> bool:
        payload = {
            "fixture_id": match_id,
            "status": status,
            "is_live": is_live,
            "available_for_voting": available_for_voting,
        }
        return self._post("/games/status", payload)

    # ============================================================
    # EVENTS
    # ============================================================

    def forward_event(self, event: Dict[str, Any]) -> bool:
        return self._post("/games/events", event)

    def forward_bulk_events(self, bulk: Dict[str, Any]) -> bool:
        return self._post("/games/events/bulk", bulk)

    def forward_event_batch(self, fixture_id: str, events: List[Dict[str, Any]]) -> bool:
        return self._post(f"/games/{fixture_id}/events/batch", {"events": events})

    # ============================================================
    # COMMENTARY - FIXED
    # ============================================================

    def forward_commentary(self, commentary: Dict[str, Any]) -> bool:
        """
        Forward commentary to the Rust API.
        Rust expects "createdAt" (camelCase).
        """
        entry = commentary.get("entry", {})
        payload = {
            "match_id": commentary.get("match_id"),
            "entry": {
                "minute": entry.get("minute", 0),
                "text": entry.get("text", ""),
                "type": entry.get("type", "general"),
                "team": entry.get("team"),
                "player": entry.get("player"),
                "createdAt": entry.get("created_at", datetime.now(timezone.utc).isoformat()),
            }
        }
        return self._post("/games/commentary", payload)

    def forward_commentary_bulk(self, fixture_id: str, entries: List[Dict[str, Any]]) -> bool:
        payload = {
            "match_id": fixture_id,
            "entries": entries
        }
        return self._post("/games/commentary/bulk", payload)

    # ============================================================
    # LINEUPS - WORKING
    # ============================================================

    def forward_lineups(self, lineups: Dict[str, Any]) -> bool:
        """
        Forward lineups to the Rust API.
        Rust expects camelCase field names.
        """
        payload = {
            "fixtureId": lineups.get("fixture_id"),
            "homeTeam": lineups.get("home_team"),
            "awayTeam": lineups.get("away_team"),
            "lineups": {
                "home": lineups.get("lineups", {}).get("home", {}),
                "away": lineups.get("lineups", {}).get("away", {})
            }
        }
        return self._post("/games/lineups", payload)

    def forward_lineups_simplified(self, fixture_id: str, home_players: List[Dict], away_players: List[Dict]) -> bool:
        payload = {
            "fixture_id": fixture_id,
            "home": home_players,
            "away": away_players,
        }
        return self._post("/games/lineups/simplified", payload)

    # ============================================================
    # STATISTICS
    # ============================================================

    def forward_statistics(self, statistics: Dict[str, Any]) -> bool:
        """
        Forward match statistics to the Rust API.
        Rust expects snake_case field names.
        """
        payload = {
            "fixture_id": statistics.get("fixture_id"),
            "minute": statistics.get("minute", 0),
            "statistics": {
                "home": {
                    "possession": statistics.get("statistics", {}).get("home", {}).get("possession"),
                    "shots": statistics.get("statistics", {}).get("home", {}).get("shots"),
                    "shots_on_target": statistics.get("statistics", {}).get("home", {}).get("shots_on_target"),
                    "shots_off_target": statistics.get("statistics", {}).get("home", {}).get("shots_off_target"),
                    "corners": statistics.get("statistics", {}).get("home", {}).get("corners"),
                    "fouls": statistics.get("statistics", {}).get("home", {}).get("fouls"),
                    "yellow_cards": statistics.get("statistics", {}).get("home", {}).get("yellow_cards"),
                    "red_cards": statistics.get("statistics", {}).get("home", {}).get("red_cards"),
                    "offsides": statistics.get("statistics", {}).get("home", {}).get("offsides"),
                    "passes": statistics.get("statistics", {}).get("home", {}).get("passes"),
                    "pass_accuracy": statistics.get("statistics", {}).get("home", {}).get("pass_accuracy"),
                },
                "away": {
                    "possession": statistics.get("statistics", {}).get("away", {}).get("possession"),
                    "shots": statistics.get("statistics", {}).get("away", {}).get("shots"),
                    "shots_on_target": statistics.get("statistics", {}).get("away", {}).get("shots_on_target"),
                    "shots_off_target": statistics.get("statistics", {}).get("away", {}).get("shots_off_target"),
                    "corners": statistics.get("statistics", {}).get("away", {}).get("corners"),
                    "fouls": statistics.get("statistics", {}).get("away", {}).get("fouls"),
                    "yellow_cards": statistics.get("statistics", {}).get("away", {}).get("yellow_cards"),
                    "red_cards": statistics.get("statistics", {}).get("away", {}).get("red_cards"),
                    "offsides": statistics.get("statistics", {}).get("away", {}).get("offsides"),
                    "passes": statistics.get("statistics", {}).get("away", {}).get("passes"),
                    "pass_accuracy": statistics.get("statistics", {}).get("away", {}).get("pass_accuracy"),
                }
            }
        }
        return self._post("/games/statistics", payload)

    def forward_statistics_bulk(self, stats_bulk: Dict[str, Any]) -> bool:
        return self._post("/games/statistics/bulk", stats_bulk)

    def forward_statistics_snapshot(self, fixture_id: str, minute: int, stats: Dict[str, Any]) -> bool:
        payload = {
            "fixture_id": fixture_id,
            "minute": minute,
            "statistics": {
                "home": {
                    "possession": stats.get("home", {}).get("possession"),
                    "shots": stats.get("home", {}).get("shots"),
                    "shots_on_target": stats.get("home", {}).get("shots_on_target"),
                    "shots_off_target": stats.get("home", {}).get("shots_off_target"),
                    "corners": stats.get("home", {}).get("corners"),
                    "fouls": stats.get("home", {}).get("fouls"),
                    "yellow_cards": stats.get("home", {}).get("yellow_cards"),
                    "red_cards": stats.get("home", {}).get("red_cards"),
                    "offsides": stats.get("home", {}).get("offsides"),
                    "passes": stats.get("home", {}).get("passes"),
                    "pass_accuracy": stats.get("home", {}).get("pass_accuracy"),
                },
                "away": {
                    "possession": stats.get("away", {}).get("possession"),
                    "shots": stats.get("away", {}).get("shots"),
                    "shots_on_target": stats.get("away", {}).get("shots_on_target"),
                    "shots_off_target": stats.get("away", {}).get("shots_off_target"),
                    "corners": stats.get("away", {}).get("corners"),
                    "fouls": stats.get("away", {}).get("fouls"),
                    "yellow_cards": stats.get("away", {}).get("yellow_cards"),
                    "red_cards": stats.get("away", {}).get("red_cards"),
                    "offsides": stats.get("away", {}).get("offsides"),
                    "passes": stats.get("away", {}).get("passes"),
                    "pass_accuracy": stats.get("away", {}).get("pass_accuracy"),
                }
            }
        }
        return self._post("/games/statistics/snapshot", payload)

    # ============================================================
    # MATCH FINALIZATION
    # ============================================================

    def finalize_match(self, finalize_data: Dict[str, Any]) -> bool:
        return self._post("/games/finalize", finalize_data)

    def forward_match_result(self, fixture_id: str, result: str, home_score: int, away_score: int) -> bool:
        payload = {
            "fixture_id": fixture_id,
            "result": result,
            "home_score": home_score,
            "away_score": away_score,
        }
        return self._post("/games/result", payload)

    def move_to_history(self, fixture_id: str) -> bool:
        return self._post(f"/games/{fixture_id}/move-to-history", {})

    # ============================================================
    # NOTIFICATIONS
    # ============================================================

    def forward_notification(self, notification: Dict[str, Any]) -> bool:
        return self._post("/games/notify", notification)

    def forward_lineups_available_notification(self, fixture_id: str, home_team: str, away_team: str) -> bool:
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
        return self._get(f"/games/match/{match_id}")

    def get_live_games(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/games/live")

    def get_upcoming_games(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/games/upcoming")

    def get_history_games(self, limit: int = 50, skip: int = 0) -> Optional[List[Dict[str, Any]]]:
        return self._get("/games/history", {"limit": limit, "skip": skip})

    # ============================================================
    # BULK SYNC
    # ============================================================

    def sync_fixtures(self, fixtures: List[Dict[str, Any]]) -> bool:
        return self._post("/games/sync", {"fixtures": fixtures})

    def sync_live_data(self, live_data: Dict[str, Any]) -> bool:
        return self._post("/games/sync/live", live_data)

    # ============================================================
    # HEALTH CHECK
    # ============================================================

    def health_check(self) -> bool:
        result = self._get("/health")
        return result is not None and result.get("status") == "healthy"

    def ping(self) -> bool:
        try:
            response = self.session.get(f"{self.api_url}/ping", timeout=5)
            return response.status_code == 200
        except:
            return False


def create_forwarder(api_url: str = None, **kwargs) -> Forwarder:
    import os
    if api_url is None:
        api_url = os.environ.get("FANCLASH_API", "https://clash-api-m5mr.onrender.com/api")
    return Forwarder(api_url, **kwargs)