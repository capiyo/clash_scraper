"""
Flashscore schedule resolution + live commentary for the World Cup poller.

poller.py imports exactly three things from this module:

    from flashscore import build_schedule_map, resolve_from_map, fetch_live_commentary_by_id

Job of this module:
  1. build_schedule_map()          -- scrape Flashscore's WC schedule feed
                                       once, return {(home, away): flashscore_id}
  2. resolve_from_map(map, h, a)   -- look up a fixture's flashscore_id from
                                       that map, using the same name
                                       normalization/alias logic as
                                       flashscore_lookup.py (handles swapped
                                       home/away order too)
  3. fetch_live_commentary_by_id() -- fetch + parse Flashscore's live text
                                       commentary feed for an already-known
                                       flashscore_id, returned pre-shaped to
                                       match the Rust CommentaryEntry fields
                                       (minute, text, type, team, player,
                                       created_at) so poller.py can forward
                                       each entry as-is.

Reuses (does not duplicate) the normalization/alias/commentary-parsing logic
already in flashscore_lookup.py, so there's a single source of truth for
_ALIASES and the commentary feed format.

Tournament/season/stage IDs below were captured via DevTools network
inspection on 2026-07-01 (flashscore.com/football/world/world-cup/fixtures/,
filter Network tab by "flashscore.ninja"). Flashscore does not expose a
stable name-based lookup endpoint, so these are hardcoded. If they ever stop
resolving fixtures (schedule map keeps coming back empty), recapture them the
same way: open the WC fixtures page, watch for a request whose URL matches
`to_<stage>_<season>_<page>` (full schedule) or `t_1_8_<tournament>_3_en_<page>`
(today's matches only), and update the constants below.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from flashscore_lookup import (
    FS_FEED_BASE,
    _HEADERS,
    _candidates,
    _normalize,
    fetch_live_commentary as _fetch_raw_commentary,
)

logger = logging.getLogger("worldcup_poller.flashscore")

# Captured via DevTools -- see module docstring.
WC_SEASON_ID = "6kKoWOjD"
WC_STAGE_ID = "zeSHfCx3"
WC_TOURNAMENT_ID = "lvUBR5F8"

_REQUEST_TIMEOUT = 10
_MAX_PAGES = 5


# ============================================================
# LOW-LEVEL FEED FETCH + PARSE
# ============================================================

def fs_get(path: str) -> Optional[str]:
    """
    GET a Flashscore.ninja feed path (e.g. "t_1_8_<id>_3_en_1") and return
    the raw pipe/glyph-delimited response text, or None on any failure.
    Never raises -- callers treat None as "no data this call".
    """
    url = f"{FS_FEED_BASE}{path}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        logger.warning("fs_get(%s) failed: %s", path, e)
        return None


def _parse_rows(raw: str) -> Iterator[Dict[str, str]]:
    """
    Split a raw Flashscore feed response into row dicts of field->value
    pairs. Rows are separated by "¬~"; within a row, fields are separated
    by "¬" and each field is "KEY÷VALUE".
    """
    if not raw:
        return
    for chunk in raw.split("¬~"):
        if not chunk:
            continue
        fields: Dict[str, str] = {}
        for pair in chunk.split("¬"):
            if "÷" not in pair:
                continue
            key, _, value = pair.partition("÷")
            if key:
                fields[key] = value
        if fields:
            yield fields


def _clean(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.replace("&amp;", "&").strip()


def _parse_match_rows(raw: str) -> Iterator[Dict[str, str]]:
    """Yield only rows that represent an actual match (have a match id, AA)."""
    for row in _parse_rows(raw):
        if row.get("AA"):
            yield row


def _row_to_fixture(row: Dict[str, str]) -> Optional[Tuple[str, str, str]]:
    """
    Extract (match_id, home_team, away_team) from a parsed match row.

    Field mapping (verified against a live feed capture, 2026-07-01):
      AA = match_id
      CX = home team name   (dup: FH)
      AF = away team name   (dup: FK)   <-- NOT AE. AE duplicates the home
                                              team name and is not usable
                                              as the away team.
    Returns None if the row is missing required fields, or if home/away
    resolve to the same name (a sign the field mapping broke again --
    logged loudly rather than silently accepted).
    """
    match_id = (row.get("AA") or "").strip()
    home = _clean(row.get("CX") or row.get("FH"))
    away = _clean(row.get("AF") or row.get("FK"))

    if not match_id or not home or not away:
        logger.debug("Skipping incomplete match row: %s", row)
        return None

    if home.strip().lower() == away.strip().lower():
        logger.warning(
            "home_team == away_team ('%s') for match_id=%s -- skipping. Raw row: %s",
            home, match_id, row,
        )
        return None

    return match_id, home, away


# ============================================================
# SCHEDULE MAP
# ============================================================

def build_schedule_map() -> Dict[Tuple[str, str], str]:
    """
    Scrape Flashscore's World Cup schedule and return
    {(normalized_home, normalized_away): flashscore_match_id}.

    Tries the full-tournament schedule feed first; if that comes back
    empty (observed to happen when the full schedule isn't published yet
    for these IDs), falls back to the today-only feed, which at minimum
    resolves fixtures that are happening today.
    """
    schedule: Dict[Tuple[str, str], str] = {}

    def _collect(path_fmt: str) -> int:
        added = 0
        for page in range(1, _MAX_PAGES + 1):
            raw = fs_get(path_fmt.format(page=page))
            if not raw:
                break
            rows = list(_parse_match_rows(raw))
            if not rows:
                break
            for row in rows:
                parsed = _row_to_fixture(row)
                if not parsed:
                    continue
                match_id, home, away = parsed
                schedule[(_normalize(home), _normalize(away))] = match_id
                added += 1
        return added

    full_path = f"to_{WC_STAGE_ID}_{WC_SEASON_ID}_{{page}}"
    added = _collect(full_path)
    if added:
        logger.info("build_schedule_map: %d fixtures from full schedule feed", added)
        return schedule

    logger.info("Full schedule feed empty -- falling back to today's feed")
    today_path = f"t_1_8_{WC_TOURNAMENT_ID}_3_en_{{page}}"
    added = _collect(today_path)
    logger.info("build_schedule_map: %d fixtures total", added)
    return schedule


def resolve_from_map(
    schedule_map: Dict[Tuple[str, str], str],
    home_team: str,
    away_team: str,
) -> Optional[str]:
    """
    Look up a fixture's flashscore_id from a map built by build_schedule_map().
    Handles name normalization/aliases (via flashscore_lookup._candidates)
    and swapped home/away order.
    """
    if not schedule_map or not home_team or not away_team:
        return None

    home_candidates = _candidates(home_team)
    away_candidates = _candidates(away_team)

    for (h, a), match_id in schedule_map.items():
        direct = h in home_candidates and a in away_candidates
        swapped = h in away_candidates and a in home_candidates
        if direct or swapped:
            return match_id

    return None


# ============================================================
# LIVE COMMENTARY
# ============================================================

def fetch_live_commentary_by_id(flashscore_id: str) -> List[Dict[str, Any]]:
    """
    Fetch + parse Flashscore's live text commentary for a known
    flashscore_id, pre-shaped to match the Rust CommentaryEntry fields
    (minute, text, type, team, player, created_at) so poller.py/forwarder.py
    can forward each entry with minimal further transformation.

    Reuses flashscore_lookup.fetch_live_commentary() for the actual
    fetch + raw parse ({"time", "text", "source"} entries), then adapts
    the shape here.
    """
    if not flashscore_id:
        return []

    raw_entries = _fetch_raw_commentary(flashscore_id)
    out: List[Dict[str, Any]] = []
    for entry in raw_entries:
        time_str = entry.get("time", "") or ""
        minute_match = re.search(r"\d+", time_str)
        minute = int(minute_match.group()) if minute_match else 0
        out.append({
            "minute": minute,
            "text": entry.get("text", ""),
            "type": "commentary",
            "team": None,
            "player": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    smap = build_schedule_map()
    print(f"Schedule map has {len(smap)} fixtures")
    for (h, a), mid in list(smap.items())[:10]:
        print(f"  {h} vs {a} -> {mid}")

    for home, away in [("Colombia", "Portugal"), ("Croatia", "Ghana"), ("Panama", "England")]:
        fs_id = resolve_from_map(smap, home, away)
        print(f"{home} vs {away} -> {fs_id or 'NOT FOUND'}")