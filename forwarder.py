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

    def _put(self, endpoint: str, data: Dict[str, Any]) -> bool:
        url = f"{self.api_url}{endpoint}"
        try:
            response = self.session.put(url, json=data, timeout=self.timeout)
            response.raise_for_status()
            logger.info(f"✅ PUT to {endpoint} successful")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to PUT to {endpoint}: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response: {e.response.text[:500]}")
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
        if ts is None:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if isinstance(ts, datetime):
            return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if isinstance(ts, str):
            ts = ts.replace('+00:00', 'Z')
            if '.' not in ts:
                ts = ts.replace('Z', '').replace('+00:00', '')
                ts = ts + ".000Z"
            if not ts.endswith('Z') and '+' not in ts:
                ts = ts + "Z"
            return ts
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _clean(self, data: Dict) -> Dict:
        return {k: v for k, v in data.items() if v is not None}

    # ============================================================
    # LIVE UPDATES
    # ============================================================

    def forward_live_update(self, update: Dict[str, Any]) -> bool:
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
    # COMMENTARY
    # ============================================================

    def _normalize_commentary_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        try:
            minute = int(entry.get("minute", 0))
        except (ValueError, TypeError):
            minute = 0

        text = str(entry.get("text", ""))

        event_type = entry.get("type")
        if not event_type:
            event_type = entry.get("event_type")
        if not event_type:
            event_type = "commentary"
        event_type = str(event_type)

        created_at = entry.get("createdAt")
        if not created_at:
            created_at = entry.get("created_at")
        created_at = self._format_timestamp(created_at)

        return self._clean({
            "minute": minute,
            "text": text,
            "type": event_type,
            "team": entry.get("team"),
            "player": entry.get("player"),
            "createdAt": created_at,
        })

    def forward_commentary(self, commentary: Dict[str, Any]) -> bool:
        entry = commentary.get("entry", {})
        match_id = commentary.get("match_id")

        if not match_id:
            logger.error("Missing match_id in commentary")
            return False

        payload = {
            "match_id": str(match_id),
            "entry": self._normalize_commentary_entry(entry),
        }

        logger.debug(f"📤 Commentary payload: {payload}")
        return self._post("/games/commentary", payload)

    def forward_commentary_bulk(self, fixture_id: str, entries: List[Dict[str, Any]]) -> bool:
        if not fixture_id:
            logger.error("Missing fixture_id in bulk commentary")
            return False
        if not entries:
            return True

        normalized = [self._normalize_commentary_entry(e) for e in entries]
        payload = {
            "match_id": str(fixture_id),
            "entries": normalized,
        }

        logger.debug(
            f"📤 Bulk commentary payload: {len(normalized)} entries for {fixture_id}"
        )
        return self._post("/games/commentary/bulk", payload)

    # ============================================================
    # STATISTICS
    # ============================================================

    def forward_statistics(self, statistics: Dict[str, Any]) -> bool:
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
    # LINEUPS
    # ============================================================

    def forward_lineups(self, lineups: Dict[str, Any]) -> bool:
        lineups_data = lineups.get("lineups", {})

        def clean_player(member: Dict[str, Any]) -> Dict[str, Any]:
            position = member.get("position")
            position_name = (
                position.get("name") or position.get("shortName")
                if isinstance(position, dict)
                else position
            ) or ""

            jersey_number = (
                member.get("jerseyNumber")
                or member.get("shirtNumber")
                or member.get("num")
                or 0
            )

            captain = bool(
                member.get("captain")
                or member.get("isCaptain")
                or member.get("captainFlag")
            )

            player_id = member.get("id") or member.get("playerId")

            return {
                "name": member.get("name") or member.get("shortName") or "Unknown",
                "position": position_name,
                "jerseyNumber": jersey_number,
                "captain": captain,
                "lineup": "starting" if member.get("status") == 1 else "bench",
                "playerId": str(player_id) if player_id is not None else None,
            }

        def clean_team(data):
            members = data.get("players") or data.get("members") or []
            starting = [clean_player(m) for m in members if m.get("status") == 1]
            bench = [clean_player(m) for m in members if m.get("status") != 1]
            bench.extend(clean_player(m) for m in data.get("bench", []))

            return {
                "formation": data.get("formation", "4-4-2"),
                "coach": {"name": data.get("coach", {}).get("name", "Unknown")},
                "players": starting,
                "bench": bench,
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
    # BET SETTLEMENT
    # ============================================================

    def settle_bets(self, fixture_id: str, result: str) -> bool:
        """
        Settle all bets for a completed match.
        
        Args:
            fixture_id: The match ID (e.g., "wc26_4627864")
            result: "home", "away", or "draw"
        
        Returns:
            True if settlement succeeded, False otherwise
        """
        if result not in ["home", "away", "draw"]:
            logger.error(f"Invalid result for settlement: {result}")
            return False

        payload = {
            "fixture_id": fixture_id,
            "result": result,
        }
        
        logger.info(f"💰 Settling bets for {fixture_id} with result: {result}")
        return self._post("/actions/bet/settle", payload)

    # ============================================================
    # HISTORY / ARCHIVE
    # ============================================================

    def move_to_history(self, fixture_id: str) -> bool:
        """Move a completed match to history."""
        if not fixture_id:
            logger.error("Missing fixture_id for move_to_history")
            return False
        return self._post(f"/games/{fixture_id}/move-to-history", {})

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
        return self._put(f"/games/{match_id}/score", payload)

    def forward_status_update(self, match_id: str, status: str, is_live: bool, available_for_voting: bool) -> bool:
        payload = {
            "matchId": match_id,
            "status": status,
            "isLive": is_live,
            "availableForVoting": available_for_voting,
        }
        return self._put(f"/games/{match_id}/status", payload)

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

    def forward_match_result(self, fixture_id: str, result: str, home_score: int, away_score: int) -> bool:
        payload = {
            "fixtureId": fixture_id,
            "result": result,
            "homeScore": int(home_score),
            "awayScore": int(away_score),
        }
        return self._post("/games/result", payload)

    def forward_lineups_simplified(self, fixture_id: str, home_players: List[Dict], away_players: List[Dict]) -> bool:
        payload = {
            "fixtureId": fixture_id,
            "home": home_players,
            "away": away_players,
        }
        return self._post("/games/lineups/simplified", payload)

    def forward_statistics_bulk(self, stats_bulk: Dict[str, Any]) -> bool:
        return self._post("/games/statistics/bulk", stats_bulk)

    def forward_statistics_snapshot(self, fixture_id: str, minute: int, stats: Dict[str, Any]) -> bool:
        payload = {
            "fixture_id": fixture_id,
            "minute": int(minute),
            "statistics": {
                "home": self._clean(stats.get("home", {})),
                "away": self._clean(stats.get("away", {})),
            }
        }
        return self._post("/games/statistics/snapshot", payload)

    def forward_lineups_available_notification(self, fixture_id: str, home_team: str, away_team: str) -> bool:
        payload = {
            "fixtureId": fixture_id,
            "eventType": "lineups_available",
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
            "fixtureId": fixture_id,
            "eventType": "match_live",
            "title": f"⚽ {home_team} vs {away_team} is LIVE!",
            "body": "Match is now live. Follow the action!",
            "data": {
                "home_team": home_team,
                "away_team": away_team,
                "type": "match_live"
            }
        }
        return self._post("/games/notify", payload)

    def forward_goal_notification(self, fixture_id: str, scorer: str, minute: int, home_score: int, away_score: int) -> bool:
        payload = {
            "fixtureId": fixture_id,
            "eventType": "goal_scored",
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
            "fixtureId": fixture_id,
            "eventType": "match_ended",
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