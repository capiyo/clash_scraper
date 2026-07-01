"""
Forwards updates from poller to Rust backend API.
ALL field names match Rust structs EXACTLY.
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
            logger.info(f"✅ POST to {endpoint} successful")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to POST to {endpoint}: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response: {e.response.text[:500]}")
                import json
                logger.error(f"Payload: {json.dumps(data, indent=2)[:1000]}")
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

    def _format_timestamp(self, ts) -> str:
        """Format timestamp for Rust DateTime<Utc> - MUST include timezone"""
        if ts is None:
            return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        if isinstance(ts, datetime):
            return ts.isoformat().replace('+00:00', 'Z')
        if isinstance(ts, str):
            if not ts.endswith('Z') and '+' not in ts:
                return ts + 'Z'
            return ts
        return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    def _clean(self, data: Dict) -> Dict:
        """Remove None values from payload"""
        return {k: v for k, v in data.items() if v is not None}

    # ============================================================
    # LIVE UPDATES - MATCHES RUST LiveGameUpdate
    # ============================================================

    def forward_live_update(self, update: Dict[str, Any]) -> bool:
        """
        Rust expects LiveGameUpdate with camelCase fields.
        """
        payload = self._clean({
            "fixtureId": update.get("fixture_id"),
            "eventType": update.get("event_type"),
            "homeScore": int(update.get("home_score", 0)),
            "awayScore": int(update.get("away_score", 0)),
            "minute": int(update.get("minute", 0)),
            "minuteDisplay": update.get("minute_display"),
            "status": update.get("status"),
            "isLive": update.get("is_live"),
            "availableForVoting": update.get("available_for_voting"),
            "scorer": update.get("scorer"),
            "player": update.get("player"),
            "assist": update.get("assist"),
            "team": update.get("team"),
            "timestamp": self._format_timestamp(update.get("timestamp")),
        })
        return self._post("/games/live-update", payload)

    # ============================================================
    # COMMENTARY - MATCHES RUST CommentaryEntry
    # ============================================================

    def forward_commentary(self, commentary: Dict[str, Any]) -> bool:
        """
        Rust expects CommentaryEntry with:
        - "type" (NOT "event_type")
        - "createdAt" (NOT "created_at")
        """
        entry = commentary.get("entry", {})
        
        payload = {
            "match_id": commentary.get("match_id"),
            "entry": self._clean({
                "minute": int(entry.get("minute", 0)),
                "text": str(entry.get("text", "")),
                "type": entry.get("type") or entry.get("event_type") or "general",
                "team": entry.get("team"),
                "player": entry.get("player"),
                "createdAt": self._format_timestamp(
                    entry.get("createdAt") or entry.get("created_at")
                ),
            })
        }
        
        if payload.get("match_id") is None:
            logger.error("Missing match_id in commentary")
            return False
            
        return self._post("/games/commentary", payload)

    # ============================================================
    # STATISTICS - MATCHES RUST StatisticsSnapshotPayload
    # ============================================================

    def forward_statistics(self, statistics: Dict[str, Any]) -> bool:
        """
        Rust expects StatisticsSnapshotPayload with snake_case fields.
        """
        stats = statistics.get("statistics", {})
        
        payload = self._clean({
            "fixture_id": statistics.get("fixture_id"),
            "minute": int(statistics.get("minute", 0)),
            "statistics": {
                "home": self._clean(stats.get("home", {})),
                "away": self._clean(stats.get("away", {})),
            }
        })
        
        if payload.get("fixture_id") is None:
            logger.error("Missing fixture_id in statistics")
            return False
            
        return self._post("/games/statistics", payload)

    # ============================================================
    # LINEUPS - MATCHES RUST LineupsUpdate
    # ============================================================

    def forward_lineups(self, lineups: Dict[str, Any]) -> bool:
        """
        Rust expects LineupsUpdate with camelCase fields.
        """
        lineups_data = lineups.get("lineups", {})
        
        def clean_team(data):
            return {
                "formation": data.get("formation", "4-4-2"),
                "coach": {"name": data.get("coach", {}).get("name", "Unknown")},
                "players": data.get("players", []),
                "bench": data.get("bench", []),
            }
        
        payload = self._clean({
            "fixtureId": lineups.get("fixture_id"),
            "homeTeam": lineups.get("home_team"),
            "awayTeam": lineups.get("away_team"),
            "lineups": {
                "home": clean_team(lineups_data.get("home", {})),
                "away": clean_team(lineups_data.get("away", {})),
            }
        })
        
        if payload.get("fixtureId") is None:
            logger.error("Missing fixtureId in lineups")
            return False
            
        return self._post("/games/lineups", payload)

    # ============================================================
    # FINALIZE MATCH - MATCHES RUST FinalizeFixtureRequest
    # ============================================================

    def finalize_match(self, finalize_data: Dict[str, Any]) -> bool:
        """
        Rust expects only fixture_id and result.
        """
        payload = self._clean({
            "fixture_id": finalize_data.get("fixture_id"),
            "result": finalize_data.get("result"),
        })
        
        if payload.get("fixture_id") is None or payload.get("result") is None:
            logger.error("Missing fixture_id or result in finalize")
            return False
            
        return self._post("/games/finalize", payload)

    # ============================================================
    # OTHER METHODS
    # ============================================================

    def forward_fixture(self, fixture: Dict[str, Any]) -> bool:
        return self._post("/games", fixture)

    def forward_fixtures_bulk(self, fixtures: List[Dict[str, Any]]) -> bool:
        return self._post("/games/bulk", {"fixtures": fixtures})

    def forward_score_update(self, match_id: str, home_score: int, away_score: int, minute: int) -> bool:
        payload = {
            "matchId": match_id,
            "homeScore": int(home_score),
            "awayScore": int(away_score),
            "timeElapsed": int(minute),
        }
        return self._post(f"/games/{match_id}/score", payload)

    def forward_status_update(self, match_id: str, status: str, is_live: bool, available_for_voting: bool) -> bool:
        payload = {
            "matchId": match_id,
            "status": status,
            "isLive": is_live,
            "availableForVoting": available_for_voting,
        }
        return self._post(f"/games/{match_id}/status", payload)

    def forward_event(self, event: Dict[str, Any]) -> bool:
        payload = self._clean({
            "fixtureId": event.get("fixture_id"),
            "eventType": event.get("event_type"),
            "minute": int(event.get("minute", 0)),
            "team": event.get("team"),
            "player": event.get("player"),
            "assist": event.get("assist"),
            "homeScore": int(event.get("home_score", 0)),
            "awayScore": int(event.get("away_score", 0)),
        })
        return self._post("/games/events", payload)

    def forward_notification(self, notification: Dict[str, Any]) -> bool:
        payload = self._clean({
            "fixtureId": notification.get("fixtureId") or notification.get("fixture_id"),
            "eventType": notification.get("eventType") or notification.get("event_type"),
            "title": notification.get("title"),
            "body": notification.get("body"),
            "data": notification.get("data"),
        })
        return self._post("/games/notify", payload)

    def get_game(self, match_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/games/match/{match_id}")

    def get_live_games(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/games/live")

    def get_upcoming_games(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/games/upcoming")

    def get_history_games(self, limit: int = 50, skip: int = 0) -> Optional[List[Dict[str, Any]]]:
        return self._get("/games/history", {"limit": limit, "skip": skip})

    def sync_fixtures(self, fixtures: List[Dict[str, Any]]) -> bool:
        return self._post("/games/sync", {"fixtures": fixtures})

    def sync_live_data(self, live_data: Dict[str, Any]) -> bool:
        return self._post("/games/sync/live", live_data)

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