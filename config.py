"""
Central configuration for the World Cup live poller.

ARCHITECTURE:
365Scores is the sole live data source — fixtures discovery, score, status,
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
WORLD_CUP_COMPETITION_IDS = [5930]
SCRAPE_DAYS_AHEAD = 7