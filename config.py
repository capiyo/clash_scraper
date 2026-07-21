"""
Central configuration for the internationals poller (formerly World Cup-only).

ARCHITECTURE:
365Scores is the sole live data source -- fixtures discovery, score, status,
and structured events (goal/card/sub) all come from it.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# MongoDB
MONGO_URI = os.environ.get("MONGO_URI", "")
MONGO_DB = os.environ.get("MONGO_DB", "clashdb")
MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "fixtures")

# Rust API
FANCLASH_API = os.environ.get("FANCLASH_API", "https://clash-api-m5mr.onrender.com/api")

# 365Scores
THREESIXTYFIVE_BASE_URL = "https://webws.365scores.com"
THREESIXTYFIVE_APP_TYPE_ID = 5
THREESIXTYFIVE_LANG_ID = 1
THREESIXTYFIVE_USER_COUNTRY_ID = 413
THREESIXTYFIVE_TIMEZONE = "Africa/Nairobi"

# Polling
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
SCRAPE_DAYS_AHEAD = 7

# ---------------------------------------------------------------------------
# Competitions
# ---------------------------------------------------------------------------
# 365Scores "competitions" query param takes the SAME numeric id that shows
# up in the human-facing URL: 365scores.com/.../league/<slug>-<id>
#
# Confirmed ids (checked directly on 365scores.com):
#   5930 = FIFA World Cup                (was WORLD_CUP_COMPETITION_IDS)
#   7016 = UEFA Nations League           -- 2026/27 league phase starts
#          Thu 24 Sep 2026 (Matchday 1, 24-26 Sep 2026)
#   6071 = "European Qualifiers"         -- UEFA's shared brand/ID for EURO
#          qualifying, reused every cycle. Currently empty (between cycles).
#          EURO 2028 qualifying Matchday 1 is 26-27 March 2027 (draw held
#          6 Dec 2026 in Belfast). Nothing will appear on this id via
#          fetch_games_by_competition() until closer to that date.
#   5421 = "UEFA WC Qualification"       -- World Cup qualifiers (separate
#          from Euro qualifiers -- don't conflate the two)
#
# COMPETITION_IDS replaces the old single-purpose WORLD_CUP_COMPETITION_IDS.
# Kept as a dict so the scraper can tag each fixture with which competition
# it belongs to and so you can add/remove competitions without touching
# scraper.py.
COMPETITION_IDS: dict[str, int] = {
    "world_cup": 5930,
    "nations_league": 7016,
    "euro_qualifiers": 6071,
    "wc_qualifiers": 5421,
}

# Back-compat alias -- some older code/imports may still reference this name.
WORLD_CUP_COMPETITION_IDS = [COMPETITION_IDS["world_cup"]]
