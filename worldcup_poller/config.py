"""
Central configuration for the World Cup live poller.

ARCHITECTURE (post-pivot):
    365Scores is the sole live data source — fixtures discovery, score,
    status, and structured events (goal/card/sub) all come from it.

    Sofascore and 1xBet have been removed entirely:
      - Sofascore: confirmed dead. Every curl_cffi impersonation profile
        (chrome120/124/131, safari17_0/18_0) returns 403, on both the
        original IP and a fresh VPN IP. This is a WAF hardening, not an
        IP ban — there is no fix to chase here.
      - 365Scores has NO prose commentary. Confirmed by direct inspection
        of a live game's `events` array (structured eventType.name /
        subTypeName only, no `text` field) and a 404 on a guessed
        textWidget endpoint. It is being kept ONLY as a score/event
        source, not a commentary source.

    API-Football remains available for fixtures/lineups/statistics if
    you choose to use it, but is not required for the score/event path
    anymore — 365Scores' games/current + game detail covers fixture
    discovery and live events on its own.

    Prose commentary: no working free source currently exists. The
    /api/games/commentary endpoint on the Rust side is fully wired and
    ready (CommentaryEntry has a `text` field) — there's simply nothing
    upstream feeding it right now. commentary.py is kept as a no-op
    hook so wiring in a real source later (paid API, AI-generated, or
    a future scrape target) doesn't require touching poller.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# --- Mongo ---
MONGO_DB = "clashdb"
MONGO_COLLECTION = "fixtures"

# --- Backend (fanclash-api, Rust/Axum) ---
# Confirmed from src/main.rs: .nest("/api/games", routes::games::routes())
FANCLASH_API_BASE = "https://fanclash-api.onrender.com"
FANCLASH_GAMES_BASE = f"{FANCLASH_API_BASE}/api/games"

# Confirmed exact paths from routes/games.rs:
FANCLASH_LIVE_UPDATE_PATH = "/live-update"          # POST, body: LiveGameUpdate
FANCLASH_EVENTS_PATH = "/events"                    # POST, body: EventRequest
FANCLASH_EVENTS_BULK_PATH = "/events/bulk"          # POST, body: Vec<EventRequest>
FANCLASH_COMMENTARY_PATH = "/commentary"            # POST, body: CommentaryUpdate
FANCLASH_SCORE_PATH = "/{match_id}/score"           # PUT,  body: UpdateGameScore
FANCLASH_STATUS_PATH = "/{match_id}/status"         # PUT,  body: GameStatusUpdate
FANCLASH_LINEUPS_PATH = "/lineups"                  # POST, body: LineupsUpdate
FANCLASH_STATISTICS_PATH = "/statistics"            # POST
FANCLASH_STATISTICS_BULK_PATH = "/statistics/bulk"  # POST

# --- 365Scores ---
THREESIXTYFIVE_BASE = "https://webws.365scores.com/web"
THREESIXTYFIVE_APP_TYPE_ID = 5
THREESIXTYFIVE_LANG_ID = 1
THREESIXTYFIVE_FOOTBALL_SPORT_ID = 1

# --- API-Football (optional secondary, lineups/statistics only) ---
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_DAILY_LIMIT = 100

# --- Request pacing ---
REQUEST_TIMEOUT_SECONDS = 15
MIN_REQUEST_SPACING_SECONDS = 1.0
JITTER_MIN_SECONDS = 0.5
JITTER_MAX_SECONDS = 1.5


@dataclass
class EndpointConfig:
    poll_interval_seconds: float
    failure_threshold: int = 4
    cooldown_seconds: float = 120.0
    half_open_trial_count: int = 1


@dataclass
class PollerConfig:
    fixtures: EndpointConfig = field(
        default_factory=lambda: EndpointConfig(poll_interval_seconds=300.0)
    )
    live: EndpointConfig = field(
        default_factory=lambda: EndpointConfig(
            poll_interval_seconds=15.0,
            failure_threshold=5,
            cooldown_seconds=90.0,
        )
    )

    # Cap on concurrent in-flight 365Scores requests across all fixtures.
    live_concurrency_limit: int = 3


DEFAULT_CONFIG = PollerConfig()

# 365Scores eventType.name -> our event_type vocabulary, matching what
# receive_live_update (Rust) pattern-matches on: "match_end", "half_time",
# "second_half", else falls through to "live". Anything not in this map
# falls through to a generic descriptive string built from the raw name.
EVENT_TYPE_MAP = {
    "goal": "goal",
    "own goal": "goal",
    "penalty": "goal",
    "yellow card": "yellow_card",
    "red card": "red_card",
    "substitution": "substitution",
}

# 365Scores statusGroup / statusText values that should map to match_end /
# half_time / second_half for the Rust side's status derivation. Verify
# these against real statusText values the first time a match goes
# through full-time — logged via logger.info the first time each new
# statusText is seen, so mismatches surface fast instead of silently
# falling through to "live".
STATUS_TEXT_MAP = {
    "ht": "half_time",
    "halftime": "half_time",
    "half-time": "half_time",
    "2nd half": "second_half",
    "second half": "second_half",
    "ft": "match_end",
    "full-time": "match_end",
    "finished": "match_end",
    "ended": "match_end",
}
