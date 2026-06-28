"""
Flashscore integration: schedule-map builder (for one-time ID resolution)
and live commentary fetcher (by ID).

ARCHITECTURE NOTE: this module does NOT write to MongoDB and does NOT
resolve names on every call. Resolution happens once per fixture, driven by
poller.py during the "soon"/early-"live" window, using build_schedule_map()
below. The resolved flashscore_id is persisted by poller.py via
FixtureStore.set_flashscore_id() (see mongo_store.py). After that, the hot
polling path only calls fetch_live_commentary_by_id() with the stored ID --
no name-matching happens during live polling.

This keeps the expensive, fragile part (matching 365Scores names against
Flashscore names) out of the 15-second live-poll loop, and keeps it from
running more often than necessary.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("worldcup_poller.flashscore")

# ─────────────────────────────────────────────────────────────────────────────
# Feed config
# ─────────────────────────────────────────────────────────────────────────────

FS_NINJA_HOST = "global.flashscore.ninja"
FS_FEED_BASE = f"https://{FS_NINJA_HOST}/2/x/feed/"
X_FSIGN_TOKEN = "SW9D1eZo"
WC_TOURNAMENT_ID = "lvUBR5F8"  # Flashscore internal World Cup 2026 ID

# Commentary feed -- confirmed working by hand against 43.flashscore.ninja
# with a real match_id. global.flashscore.ninja is tried as a fallback
# since it's the verified host for the *schedule* feed, but the commentary
# feed there is unverified -- if it never succeeds in your logs, drop it
# from this list.
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

# Add pairs here as you find 365Scores/Flashscore naming mismatches.
_ALIASES = {
    "south korea": "korea republic",
    "usa": "united states",
    "ivory coast": "cote divoire",
}


# ─────────────────────────────────────────────────────────────────────────────
# Name normalization
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().strip()
    name = re.sub(r"\b(national team|nt|fc|the)\b", "", name)
    name = re.sub(r"[^a-z0-9 ]", "", name)
    return re.sub(r"\s+", " ", name).strip()


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
# Low-level feed fetch + parse
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
    out = []
    for f in _parse_rows(raw):
        match_id = f.get("LME", "").strip()
        home = _clean(f.get("LMJ", ""))
        away = _clean(f.get("LMK", ""))
        if match_id and home and away:
            out.append((match_id, home, away))
    return out


def _extract_matches_from_today_feed(raw: str) -> List[Tuple[str, str, str]]:
    out = []
    for f in _parse_rows(raw):
        match_id = f.get("AA", "").strip()
        home = _clean(f.get("CX", "") or f.get("FH", ""))
        away = _clean(f.get("AE", "") or f.get("AF", ""))
        if match_id and home and away:
            out.append((match_id, home, away))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Public: schedule map (used by poller.py's resolution step, NOT by the
# commentary hot path)
# ─────────────────────────────────────────────────────────────────────────────

def build_schedule_map() -> Dict[Tuple[str, str], str]:
    """
    Pulls Flashscore's World Cup schedule feed and returns an in-memory
    {(normalized_home, normalized_away): match_id} map.

    Call this from poller.py once (e.g. cached on the Poller instance, or
    rebuilt every N hours), not per-fixture. Resolution results should be
    persisted via FixtureStore.set_flashscore_id() so this never needs to
    run during live polling.
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
            time.sleep(0.5)
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

    if not name_map:
        logger.error(
            "Flashscore schedule map came back EMPTY -- commentary resolution "
            "will fail for all fixtures until this succeeds. Check feed "
            "endpoints/host if this persists."
        )
    else:
        logger.info(f"Built Flashscore schedule map: {len(name_map)} fixtures")

    return name_map


def resolve_from_map(
    schedule_map: Dict[Tuple[str, str], str],
    home_team: str,
    away_team: str,
) -> Optional[str]:
    """Look up a match_id from a pre-built schedule map for one fixture."""
    home_candidates = _candidates(home_team)
    away_candidates = _candidates(away_team)

    for (m_home, m_away), match_id in schedule_map.items():
        direct = m_home in home_candidates and m_away in away_candidates
        swapped = m_home in away_candidates and m_away in home_candidates
        if direct or swapped:
            return match_id

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public: commentary fetch by ID -- this IS the hot-path function, called
# every live poll cycle once a fixture has a persisted flashscore_id.
# ─────────────────────────────────────────────────────────────────────────────

def fetch_live_commentary_by_id(match_id: str) -> List[Dict[str, Any]]:
    """Fetch commentary directly given an already-resolved match_id."""
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

    logger.debug(f"No commentary found for match_id={match_id} on any known host")
    return []


def _parse_minute(raw_time: str) -> int:
    """
    Flashscore's commentary time field is a string like '23', '45+2', or
    possibly empty. Rust's CommentaryEntry.minute is a required i32 (no
    Option), so this must always return *something* -- falls back to 0
    rather than dropping the entry, since losing a real commentary line is
    worse than mislabeling its minute as 0 in the rare unparseable case.
    """
    if not raw_time:
        return 0
    match = re.match(r"\d+", raw_time.strip())
    if match:
        try:
            return int(match.group(0))
        except ValueError:
            return 0
    return 0


# Keyword -> event type, checked in order against the commentary text.
# Rust's CommentaryEntry.event_type is a required String (no Option, no
# server-side default), so every entry needs one of these -- "general" is
# the fallback when no keyword matches. This is a heuristic on free text,
# not a structured field from Flashscore, so it can occasionally
# misclassify -- but it's strictly better than the same flat label on every
# entry, since your own forward_commentary docstring lists exactly these
# categories as what downstream (Flutter) expects to style differently.
_TYPE_KEYWORDS = [
    ("goal", ("⚽", "goal!", " scores", "scored")),
    ("card", ("🟨", "🟥", "yellow card", "red card", "booked", "booking")),
    ("substitution", ("🔄", "substitution", "comes on", "replaces")),
    ("chance", ("🎯", "penalty", "missed", "chance", "saves", "save!")),
]


def _infer_event_type(text: str) -> str:
    lowered = text.lower()
    for event_type, keywords in _TYPE_KEYWORDS:
        if any(kw.lower() in lowered for kw in keywords):
            return event_type
    return "general"


def _parse_commentary(raw: str) -> List[Dict[str, Any]]:
    """
    Parses Flashscore's commentary feed into the exact shape Rust's
    CommentaryEntry struct requires (see Game model: minute: i32 required,
    type: String required (serde rename "type"), createdAt: BsonDateTime
    required). All three were previously missing/mistyped (sent as
    time/text/source), which is what caused every /games/commentary POST
    to 422.
    """
    entries = []
    for part in raw.split("¬~MB÷"):
        if not part.strip():
            continue
        time_match = re.search(r"¬MK÷([^¬]+)", part)
        text_match = re.search(r"¬MD÷([^¬]+)", part)
        text = (text_match.group(1) if text_match else "").replace("¬", "").strip()
        if text:
            raw_time = (time_match.group(1) if time_match else "").strip()
            entries.append({
                "minute": _parse_minute(raw_time),
                "text": text,
                "type": _infer_event_type(text),
                "team": None,
                "player": None,
                # RFC3339 -- matches forward_live_update's working
                # timestamp format (DateTime<Utc>). If Rust's BsonDateTime
                # rejects this (different from chrono's DateTime<Utc>),
                # the 422 body will show a deserialization error on this
                # field specifically -- swap for Mongo extended JSON
                # {"$date": <millis>} if so.
                "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
    return entries


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    smap = build_schedule_map()
    for h, a in [("Colombia", "Portugal"), ("Croatia", "Ghana"), ("Panama", "England")]:
        mid = resolve_from_map(smap, h, a)
        print(f"{h} vs {a} -> match_id={mid}")
        if mid:
            entries = fetch_live_commentary_by_id(mid)
            print(f"  -> {len(entries)} commentary entries")