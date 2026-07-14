"""
Forwarder.

Pushes 365Scores-derived data to fanclash-api (Rust/Axum), using the
exact field names and paths confirmed from routes/games.rs and
models/game.rs:

    POST {FANCLASH_GAMES_BASE}/live-update   <- LiveGameUpdate
    PUT  {FANCLASH_GAMES_BASE}/{match_id}/score   <- UpdateGameScore
    PUT  {FANCLASH_GAMES_BASE}/{match_id}/status  <- GameStatusUpdate
    POST {FANCLASH_GAMES_BASE}/commentary    <- CommentaryUpdate (unused
                                                  for now — no prose
                                                  source exists, see
                                                  config.py docstring)

LiveGameUpdate field names (from models/game.rs), reproduced exactly —
get any of these wrong and serde will reject the payload outright:
    fixture_id, event_type, home_score, away_score, minute,
    minute_display, scorer, player, assist, team, player_out,
    player_in, on_target, blocked

Plain `requests`, not curl_cffi — this is your own backend, not a
defended source.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests

import config

logger = logging.getLogger("worldcup_poller.forwarder")


def _post(path: str, payload: dict[str, Any]) -> bool:
    url = f"{config.FANCLASH_GAMES_BASE}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code >= 300:
            logger.warning("POST %s -> %s: %s", url, resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as exc:
        logger.warning("POST %s failed: %s", url, exc)
        return False


def _put(path: str, payload: dict[str, Any]) -> bool:
    url = f"{config.FANCLASH_GAMES_BASE}{path}"
    try:
        resp = requests.put(url, json=payload, timeout=10)
        if resp.status_code >= 300:
            logger.warning("PUT %s -> %s: %s", url, resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as exc:
        logger.warning("PUT %s failed: %s", url, exc)
        return False


def forward_live_update(
    match_id: str,
    event_type: str,
    home_score: int,
    away_score: int,
    minute: int,
    minute_display: str,
    scorer: Optional[str] = None,
    player: Optional[str] = None,
    assist: Optional[str] = None,
    team: Optional[str] = None,
    player_out: Optional[str] = None,
    player_in: Optional[str] = None,
    on_target: Optional[bool] = None,
    blocked: Optional[bool] = None,
) -> bool:
    """Maps 1:1 onto LiveGameUpdate (models/game.rs). This is the
    endpoint that also broadcasts to channel websockets and auto-
    finalizes pledges when event_type == 'match_end' on the Rust side —
    prefer this over the plain /score or /status endpoints whenever
    you have a real event to report, not just a score delta."""
    payload = {
        "fixture_id": match_id,
        "event_type": event_type,
        "home_score": home_score,
        "away_score": away_score,
        "minute": minute,
        "minute_display": minute_display,
        "scorer": scorer,
        "player": player,
        "assist": assist,
        "team": team,
        "player_out": player_out,
        "player_in": player_in,
        "on_target": on_target,
        "blocked": blocked,
    }
    return _post(config.FANCLASH_LIVE_UPDATE_PATH, payload)


def forward_score_only(match_id: str, home_score: int, away_score: int, status: Optional[str] = None, is_live: Optional[bool] = None, time_elapsed: Optional[int] = None) -> bool:
    """Maps onto UpdateGameScore. Use this for a plain score refresh
    with no specific event attached (e.g. a periodic sync tick) — it
    does NOT broadcast a typed event_type or trigger auto-finalize."""
    payload: dict[str, Any] = {
        "match_id": match_id,
        "home_score": home_score,
        "away_score": away_score,
    }
    if status is not None:
        payload["status"] = status
    if is_live is not None:
        payload["is_live"] = is_live
    if time_elapsed is not None:
        payload["time_elapsed"] = time_elapsed
    return _put(config.FANCLASH_SCORE_PATH.format(match_id=match_id), payload)


def forward_status(match_id: str, status: str, is_live: bool) -> bool:
    """Maps onto GameStatusUpdate. status must be one of the Rust
    side's valid_statuses: upcoming, soon, live, completed — NOT the
    same vocabulary as LiveGameUpdate's event_type (match_end/
    half_time/etc). Don't conflate the two."""
    payload = {"match_id": match_id, "status": status, "is_live": is_live}
    return _put(config.FANCLASH_STATUS_PATH.format(match_id=match_id), payload)


def forward_commentary(match_id: str, entry: dict[str, Any]) -> bool:
    """Maps onto CommentaryUpdate { match_id, entry: CommentaryEntry }.
    Not currently called anywhere in poller.py — no prose source exists
    yet. Kept ready so wiring one in later is a one-line change in
    poller.py, not a forwarder rewrite."""
    payload = {"match_id": match_id, "entry": entry}
    return _post(config.FANCLASH_COMMENTARY_PATH, payload)
