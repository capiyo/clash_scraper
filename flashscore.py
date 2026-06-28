"""
Flashscore live commentary fetcher.

RESOLVER REPLACED: get_flashscore_id() previously hit
https://www.flashscore.co.ke/search/{team} {team}/, which always 404s
(Flashscore's search box is JS-driven against an internal API, not a
server-rendered GET route).

NEW APPROACH: build an in-memory map of {normalized team names -> match_id}
once per process (or on TTL expiry) by pulling Flashscore's own World Cup
schedule feed -- the SAME verified feed family already used by
worldcup_poller_flashscore.py (global.flashscore.ninja/2/x/feed/,
to_{stage}_{season}_{page} / t_1_8_{WC_ID}_3_en_{page}). Team names from
clashdb.fixtures (365Scores) are then matched against that map at lookup
time. The resolved match_id is used ONLY in-memory to build the commentary
URL -- it is never written to MongoDB. clashdb.fixtures stays 365Scores-only,
exactly as it is today (see mongo_store.py's upsert_fixture schema).

Call sites in poller.py are unchanged: fetch_live_commentary(home_team,
away_team) still takes team names and returns commentary entries.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("worldcup_poller.flashscore")

# ─────────────────────────────────────────────────────────────────────────────
# Flashscore feed config -- copied from the VERIFIED working config in
# worldcup_poller_flashscore.py. Same host/path/token, since that's the one
# confirmed to return real data (not the 43.flashscore.ninja host used by
# the old commentary-only fetcher's resolver attempt).
# ─────────────────────────────────────────────────────────────────────────────

FS_NINJA_HOST = "global.flashscore.ninja"
FS_FEED_BASE = f"https://{FS_NINJA_HOST}/2/x/feed/"
X_FSIGN_TOKEN = "SW9D1eZo"
WC_TOURNAMENT_ID = "lvUBR5F8"  # Flashscore internal World Cup 2026 ID

# Commentary feed -- UNVERIFIED in this exact host/path combo. You confirmed
# 43.flashscore.ninja/43/x/feed/df_lcpo_1_{id} returns real commentary with a
# real match_id. That's a different host than the schedule/live feeds. Both
# are tried below so you don't have to manually pick.
_COMMENTARY_URL_CANDIDATES = [
    "https://43.flashscore.ninja/43/x/feed/df_lcpo_1_{match_id}",
    f"{FS_FEED_BASE}df_lcpo_1_{{match_id}}",
]

_HEADERS = {
    "Accept": "text/plain, */*; q=0.01",
    "Referer": "https://www.flashscore.com/",
    "Origin": "https://www.flashscore.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "X-Fsign": X_FSIGN_TOKEN,
}

# How long the in-memory name->ID map is trusted before being rebuilt.
# Fixtures don't change identity mid-tournament, so this can be long --
# it's just here so a process that runs for days picks up newly-added
# rounds without a restart.
_MAP_TTL_SECONDS = 6 * 3600  # 6 hours

# ─────────────────────────────────────────────────────────────────────────────
# In-memory state (NOT persisted to Mongo or disk anywhere)
# ─────────────────────────────────────────────────────────────────────────────

_name_to_id_map: Dict[Tuple[str, str], str] = {}
_map_built_at: float = 0.0
_flashscore_cache: Dict[str, Optional[str]] = {}  # per-matchup ID cache


def _normalize(name: str) -> str:
    """Lowercase, strip accents/punctuation/common suffixes so 365Scores'
    and Flashscore's naming converge on the same key."""
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().strip()
    name = re.sub(r"\b(national team|nt|fc|the)\b", "", name)
    name = re.sub(r"[^a-z0-9 ]", "", name)
    return re.sub(r"\s+", " ", name).strip()


# Add pairs here as you find 365Scores/Flashscore naming mismatches during
# the tournament (e.g. logged "no match found" warnings below).
_ALIASES = {
    "south korea": "korea republic",
    "usa": "united states",
    "ivory coast": "cote divoire",
}


def _candidates(name: str) -> set:
    n = _normalize(name)
    out = {n}
    if n in _ALIASES:
        out.add(_ALIASES[n])
    for k, v in _ALIASES.items():
        if v == n:
            out.add(k)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Low-level feed fetch + parse (same row format as worldcup_poller_flashscore.py)
# ─────────────────────────────────────────────────────────────────────────────

def _fs_get(query: str, timeout: int = 15) -> Optional[str]:
    url = f"{FS_FEED_BASE}{query}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return resp.text
        logger.debug(f"Flashscore feed {query} -> HTTP {resp.status_code}")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"Flashscore feed {query} failed: {e}")
        return None


def _parse_rows(raw: str) -> List[Dict[str, str]]:
    rows = []
    for row in raw.split("~"):
        row = row.strip()
        if not row:
            continue
        f: Dict[str, str] = {}
        for part in row.split("¬"):
            if "÷" in part:
                k, _, v = part.partition("÷")
                f[k.strip()] = v.strip()
        if f:
            rows.append(f)
    return rows


_STRIP_TAGS = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return " ".join(_STRIP_TAGS.sub("", s or "").split())


def _get_season_stage_ids() -> Tuple[Optional[str], Optional[str]]:
    """Tournament header row gives season_id (ZC) / stage_id (ZE), needed
    to build the to_{stage}_{season}_{page} schedule endpoint."""
    raw = _fs_get(f"t_1_8_{WC_TOURNAMENT_ID}_3_en_1")
    if not raw:
        return None, None
    for f in _parse_rows(raw):
        if "ZA" in f:
            season_id = f.get("ZC", "").strip()
            stage_id = f.get("ZE", "").strip()
            if season_id and stage_id:
                return season_id, stage_id
    return None, None


def _extract_matches_from_schedule(raw: str) -> List[Tuple[str, str, str]]:
    """Returns list of (match_id, home_team, away_team) from a
    to_{stage}_{season}_{page} response (LME÷/LMJ÷/LMK÷ fields)."""
    out = []
    for f in _parse_rows(raw):
        match_id = f.get("LME", "").strip()
        home = _clean(f.get("LMJ", ""))
        away = _clean(f.get("LMK", ""))
        if match_id and home and away:
            out.append((match_id, home, away))
    return out


def _extract_matches_from_today_feed(raw: str) -> List[Tuple[str, str, str]]:
    """Returns list of (match_id, home_team, away_team) from a
    t_1_8_{WC_ID}_3_en_{page} response (AA÷/CX÷/AE÷ fields)."""
    out = []
    for f in _parse_rows(raw):
        match_id = f.get("AA", "").strip()
        home = _clean(f.get("CX", "") or f.get("FH", ""))
        away = _clean(f.get("AE", "") or f.get("AF", ""))
        if match_id and home and away:
            out.append((match_id, home, away))
    return out


def _build_name_to_id_map() -> Dict[Tuple[str, str], str]:
    """
    Pulls the Flashscore World Cup schedule feed and builds an in-memory
    {(normalized_home, normalized_away): match_id} map. Nothing here
    touches MongoDB -- this map lives only in this process's memory and is
    rebuilt on TTL expiry or process restart.
    """
    name_map: Dict[Tuple[str, str], str] = {}

    season_id, stage_id = _get_season_stage_ids()

    if season_id and stage_id:
        for page in range(1, 20):
            raw = _fs_get(f"to_{stage_id}_{season_id}_{page}")
            if not raw or len(raw.strip()) < 10:
                break
            matches = _extract_matches_from_schedule(raw)
            if not matches:
                break
            for match_id, home, away in matches:
                name_map[(_normalize(home), _normalize(away))] = match_id
            time.sleep(0.5)  # be polite, this runs infrequently anyway
    else:
        logger.warning("Could not resolve season_id/stage_id, trying today-feed fallback")

    if not name_map:
        for page in range(1, 6):
            raw = _fs_get(f"t_1_8_{WC_TOURNAMENT_ID}_3_en_{page}")
            if not raw or len(raw.strip()) < 10:
                break
            matches = _extract_matches_from_today_feed(raw)
            if not matches:
                break
            for match_id, home, away in matches:
                name_map[(_normalize(home), _normalize(away))] = match_id
            time.sleep(0.5)

    logger.info(f"Built in-memory Flashscore name->ID map: {len(name_map)} fixtures")
    return name_map


def _ensure_map_fresh() -> None:
    global _name_to_id_map, _map_built_at
    age = time.time() - _map_built_at
    if not _name_to_id_map or age > _MAP_TTL_SECONDS:
        _name_to_id_map = _build_name_to_id_map()
        _map_built_at = time.time()


def _lookup_in_map(home_team: str, away_team: str) -> Optional[str]:
    _ensure_map_fresh()

    home_candidates = _candidates(home_team)
    away_candidates = _candidates(away_team)

    for (m_home, m_away), match_id in _name_to_id_map.items():
        direct = m_home in home_candidates and m_away in away_candidates
        swapped = m_home in away_candidates and m_away in home_candidates
        if direct or swapped:
            return match_id

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API -- unchanged signatures from the original file
# ─────────────────────────────────────────────────────────────────────────────

def get_flashscore_id(home_team: str, away_team: str) -> Optional[str]:
    """
    Get Flashscore match ID, resolved via the in-memory schedule map
    (never via the dead /search/ endpoint, never via MongoDB).
    """
    cache_key = f"{home_team}_{away_team}"

    if cache_key in _flashscore_cache:
        return _flashscore_cache[cache_key]

    match_id = _lookup_in_map(home_team, away_team)

    _flashscore_cache[cache_key] = match_id
    if match_id:
        logger.info(f"Resolved Flashscore ID for {home_team} vs {away_team}: {match_id}")
    else:
        logger.warning(
            f"No Flashscore match found for {home_team} vs {away_team} "
            f"(check _ALIASES if this is a real naming mismatch)"
        )
    return match_id


def fetch_live_commentary(home_team: str, away_team: str) -> List[Dict[str, Any]]:
    """Fetch live commentary using team names. Unchanged call signature --
    poller.py's _fetch_commentary() doesn't need to change at all."""
    match_id = get_flashscore_id(home_team, away_team)
    if not match_id:
        logger.debug(f"No Flashscore match ID for {home_team} vs {away_team}")
        return []
    return fetch_live_commentary_by_id(match_id)


def fetch_live_commentary_by_id(match_id: str) -> List[Dict[str, Any]]:
    """Fetch commentary directly given a match_id. Tries both known hosts
    since the commentary feed's correct host wasn't independently
    re-verified against global.flashscore.ninja -- only against
    43.flashscore.ninja, per your testing."""
    for url_template in _COMMENTARY_URL_CANDIDATES:
        url = url_template.format(match_id=match_id)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=10)
            if resp.status_code == 200 and resp.text.strip():
                entries = _parse_commentary(resp.text)
                if entries:
                    return entries
        except requests.exceptions.RequestException as e:
            logger.debug(f"Commentary fetch failed for {url}: {e}")
            continue

    logger.warning(f"No commentary found for match_id={match_id} on any known host")
    return []


def _parse_commentary(raw: str) -> List[Dict[str, Any]]:
    """Parse Flashscore commentary format. Unchanged."""
    entries = []
    for part in raw.split("¬~MB÷"):
        if not part.strip():
            continue
        time_match = re.search(r"¬MK÷([^¬]+)", part)
        text_match = re.search(r"¬MD÷([^¬]+)", part)
        text = (text_match.group(1) if text_match else "").replace("¬", "").strip()
        if text:
            entries.append({
                "time": (time_match.group(1) if time_match else "").strip(),
                "text": text,
                "source": "flashscore",
            })
    return entries


def clear_cache():
    """Clear both the matchup cache and the in-memory name->ID map
    (useful for testing, or forcing a fresh schedule pull mid-run)."""
    global _flashscore_cache, _name_to_id_map, _map_built_at
    _flashscore_cache = {}
    _name_to_id_map = {}
    _map_built_at = 0.0
    logger.info("Flashscore cache and name->ID map cleared")


def get_cache_size() -> int:
    """Get the number of cached matchup entries."""
    return len(_flashscore_cache)


def get_map_size() -> int:
    """Get the number of fixtures currently in the in-memory name->ID map."""
    return len(_name_to_id_map)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Smoke test against your earlier example matchups -- no DB needed.
    for h, a in [("Colombia", "Portugal"), ("Croatia", "Ghana"), ("Panama", "England")]:
        mid = get_flashscore_id(h, a)
        print(f"{h} vs {a} -> match_id={mid}")
        if mid:
            entries = fetch_live_commentary_by_id(mid)
            print(f"  -> {len(entries)} commentary entries")