"""
MongoDB access for the poller. Field names match the Rust Game struct
EXACTLY (camelCase, per each #[serde(rename = "...")]) -- this was
previously broken: the file's docstring claimed to match Rust but every
write/query used snake_case, causing every fixture document to fail
deserialization on the Rust side ("invalid type: map, expected a string" /
documents silently skipped in GET /api/games).

Handles: fixtures, lineups, statistics, events, commentary, state management.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, List, Dict

from pymongo import MongoClient
from pymongo.collection import Collection

import config

logger = logging.getLogger("worldcup_poller.mongo")


class FixtureStore:
    def __init__(self, mongo_uri: str):
        self._client = MongoClient(mongo_uri)
        self._collection: Collection = self._client[config.MONGO_DB][config.MONGO_COLLECTION]
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create indexes for fast queries. Index keys use the same
        camelCase field names actually stored on documents."""
        try:
            self._collection.create_index("matchId", unique=True)
            self._collection.create_index("threesixtyfiveGameId")
            self._collection.create_index("status")
            self._collection.create_index([("status", 1), ("isLive", 1)])
            self._collection.create_index("kickoffUtc")
            self._collection.create_index("scrapedAt")
            self._collection.create_index("forwardedEventSignatures")
            logger.info("MongoDB indexes ensured")
        except Exception as e:
            logger.warning(f"Index creation issue: {e}")

    # ============================================================
    # FIXTURE CRUD OPERATIONS
    # ============================================================

    def upsert_fixture(
        self,
        match_id: str,
        threesixtyfive_game_id: str,
        home_team: str,
        away_team: str,
        kickoff_utc: datetime,
        status: str,
        home_competitor_id: Optional[str] = None,
        away_competitor_id: Optional[str] = None,
        competition_id: Optional[int] = None,
        competition_name: str = "FIFA World Cup 2026",
        odds: dict = None,
    ) -> None:
        """
        Upsert a fixture. Document keys match Rust's Game struct exactly
        (see models/game.rs): matchId, homeTeam, awayTeam, kickoffUtc,
        isLive, availableForVoting, homeWin/awayWin, scrapedAt, etc.

        NOTE: Game.kickoff_utc is DateTime<Utc> -- a *required* field, not
        Option -- so this must always be a real datetime, never None.
        NOTE: Game.home_competitor_id / away_competitor_id / competition_id
        are not actually fields on the Rust Game struct shown -- they're
        kept here as Python-side bookkeeping (used by poller.py for
        lineups/stats lookups against 365Scores) but are NOT part of the
        camelCase Rust contract, so they're stored as-is (snake_case) since
        Rust's deserializer will simply ignore unknown fields it doesn't
        have a struct field for (serde's default behavior is to ignore
        unrecognized keys, not error on them).
        """
        date_str = kickoff_utc.strftime("%Y-%m-%d")
        time_str = kickoff_utc.strftime("%H:%M")
        date_iso = kickoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Default odds to 1.0 if not provided
        home_win = 1.0
        away_win = 1.0
        draw = 1.0
        if odds:
            home_win = odds.get("homeWin", 1.0)
            away_win = odds.get("awayWin", 1.0)
            draw = odds.get("draw", 1.0)

        is_live = status == "live"
        available_for_voting = status in ("upcoming", "soon")

        # Build the document -- camelCase keys matching Game's #[serde(rename)]
        doc = {
            "matchId": match_id,
            "threesixtyfiveGameId": threesixtyfive_game_id,
            "homeTeam": home_team,
            "awayTeam": away_team,
            # Bookkeeping fields, not on the Rust struct -- harmless extras,
            # ignored by serde on read. Kept snake_case to make clear
            # they're Python-side only, not part of the Rust contract.
            "home_competitor_id": home_competitor_id,
            "away_competitor_id": away_competitor_id,
            "competition_id": competition_id,
            "league": competition_name,
            "date": date_str,
            "time": time_str,
            "dateIso": date_iso,
            "kickoffUtc": kickoff_utc,
            "homeScore": None,
            "awayScore": None,
            "status": status,
            "isLive": is_live,
            "availableForVoting": available_for_voting,
            "homeWin": home_win,
            "awayWin": away_win,
            "draw": draw,
            "scrapedAt": datetime.now(timezone.utc),
            "source": "365scores",
            "lastScrapedAt": datetime.now(timezone.utc),
        }

        # Fields that should ONLY be set on insert (user-generated data preserved)
        set_on_insert = {
            # CRITICAL: explicitly set _id to the same string as matchId.
            # Without this, MongoDB auto-generates _id as a BSON ObjectId.
            # Rust's Game.id field is `Option<String>` (#[serde(rename =
            # "_id")]) -- an ObjectId does NOT deserialize into a plain
            # String via serde (it needs bson::oid::ObjectId specifically,
            # or a string representation). This single mismatched field
            # was the actual cause of EVERY "invalid type: map, expected a
            # string" / "skipping malformed fixture document" error, even
            # after every other field was correctly renamed to camelCase --
            # the camelCase fix was necessary but not sufficient.
            "_id": match_id,
            "votes": 0,
            "voters": [],
            "comments": 0,
            "commentary": [],
            "commentaryCount": 0,
            "lastCommentaryAt": None,
            "lineups": None,
            "lineupsFetched": False,
            "lineupsFetchedAt": None,
            "statistics": [],
            "lastStatisticsMinute": None,
            "forwardedEventSignatures": [],
            "lastPolledAt": None,
            "completedAt": None,
            "movedToHistory": False,
            "createdAt": datetime.now(timezone.utc),
            "result": None,
            "timeElapsed": None,
            # Flashscore cross-reference -- resolved lazily by poller.py once
            # per fixture (name-match against Flashscore's schedule feed),
            # not on every scrape. Nullable: a fixture is fully valid with
            # this unset. NOT a Rust Game struct field -- Python/poller-side
            # bookkeeping only, ignored by serde on read. Kept snake_case to
            # signal that.
            "flashscore_id": None,
            "flashscore_resolved_at": None,
            "flashscore_resolve_attempts": 0,
        }

        self._collection.update_one(
            {"matchId": match_id},
            {
                "$set": doc,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

    def get_fixture(self, match_id: str) -> Optional[Dict[str, Any]]:
        """Get a single fixture by match_id."""
        return self._collection.find_one({"matchId": match_id})

    def get_fixtures_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get all fixtures with a given status."""
        return list(self._collection.find({"status": status}))

    def get_all_fixtures(self) -> List[Dict[str, Any]]:
        """Get all fixtures (all statuses)."""
        return list(self._collection.find({}))

    def get_fixtures_in_window(self, days_ahead: int = 7) -> List[Dict[str, Any]]:
        """Get fixtures within the next N days."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days_ahead)
        return list(self._collection.find({
            "kickoffUtc": {"$gte": now, "$lte": cutoff}
        }))

    def get_active_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures that are upcoming, soon, or live."""
        return list(self._collection.find({
            "status": {"$in": ["upcoming", "soon", "live"]}
        }))

    def get_in_progress_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures that are currently live."""
        return list(self._collection.find({"status": "live"}))

    def get_upcoming_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures that are upcoming or soon."""
        return list(self._collection.find({
            "status": {"$in": ["upcoming", "soon"]}
        }))

    def get_soon_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures in the 'soon' state."""
        return list(self._collection.find({"status": "soon"}))

    def get_completed_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures that are completed."""
        return list(self._collection.find({"status": "completed"}))

    def get_stale_completed_fixtures(self, hours: int = 1) -> List[Dict[str, Any]]:
        """Get completed fixtures older than N hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return list(self._collection.find({
            "status": "completed",
            "completedAt": {"$lt": cutoff}
        }))

    def get_threesixtyfive_game_id(self, match_id: str) -> Optional[str]:
        """Get the 365Scores game ID for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"threesixtyfiveGameId": 1}
        )
        return doc.get("threesixtyfiveGameId") if doc else None

    def get_game(self, match_id: str) -> Optional[Dict[str, Any]]:
        """Get full game document (alias for get_fixture)."""
        return self.get_fixture(match_id)

    # ============================================================
    # STATUS UPDATES
    # ============================================================

    def update_status(self, match_id: str, status: str) -> None:
        """Update match status."""
        is_live = status == "live"
        available_for_voting = status in ("upcoming", "soon")

        update = {
            "status": status,
            "isLive": is_live,
            "availableForVoting": available_for_voting,
            "scrapedAt": datetime.now(timezone.utc),
        }

        if status == "completed":
            update["completedAt"] = datetime.now(timezone.utc)

        self._collection.update_one(
            {"matchId": match_id},
            {"$set": update}
        )

    def update_score(self, match_id: str, home_score: int, away_score: int) -> None:
        """Update score for a match."""
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$set": {
                    "homeScore": home_score,
                    "awayScore": away_score,
                    "scrapedAt": datetime.now(timezone.utc),
                }
            }
        )

    def update_time_elapsed(self, match_id: str, time_elapsed: int) -> None:
        """Update the elapsed time for a match."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$set": {"timeElapsed": time_elapsed}}
        )

    def mark_live(self, match_id: str) -> None:
        """Mark a match as live."""
        self.update_status(match_id, "live")

    def mark_completed(self, match_id: str) -> None:
        """Mark a match as completed."""
        self.update_status(match_id, "completed")

    def record_last_poll(self, match_id: str) -> None:
        """Record last poll time. NOTE: lastPolledAt is not on the Rust
        Game struct shown -- harmless extra field, ignored by serde."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$set": {"lastPolledAt": datetime.now(timezone.utc)}}
        )

    # ============================================================
    # FLASHSCORE CROSS-REFERENCE
    # ============================================================
    # flashscore_id is resolved once per fixture (name-match against
    # Flashscore's own schedule feed) and persisted here, rather than
    # re-resolved on every commentary fetch. This keeps the hot polling
    # path (every 15s while live) to a plain field read instead of a
    # name-matching pass against an in-memory map on every cycle.
    #
    # These fields are NOT part of the Rust Game struct's camelCase
    # contract -- kept snake_case deliberately, since they're Python/
    # poller-side bookkeeping only. serde ignores unknown fields by
    # default, so this causes no deserialization issues on the Rust side.

    def needs_flashscore_resolution(self, match: Dict[str, Any], max_attempts: int = 5) -> bool:
        """
        True if this fixture still needs its Flashscore ID resolved --
        i.e. it doesn't have one yet, and hasn't already failed
        max_attempts times (so a permanently-unmatchable name pair stops
        being retried instead of hammering Flashscore's schedule feed
        forever).
        """
        if match.get("flashscore_id"):
            return False
        return match.get("flashscore_resolve_attempts", 0) < max_attempts

    def set_flashscore_id(self, match_id: str, flashscore_id: str) -> None:
        """Persist a successfully resolved Flashscore match ID."""
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$set": {
                    "flashscore_id": flashscore_id,
                    "flashscore_resolved_at": datetime.now(timezone.utc),
                }
            }
        )

    def record_flashscore_resolve_attempt(self, match_id: str) -> None:
        """Record a failed resolution attempt (no match found this try)."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$inc": {"flashscore_resolve_attempts": 1}}
        )

    def get_flashscore_id(self, match_id: str) -> Optional[str]:
        """Get the resolved Flashscore ID for a match, if any."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"flashscore_id": 1}
        )
        return doc.get("flashscore_id") if doc else None

    # ============================================================
    # LINEUPS
    # ============================================================
    # NOTE: Rust's Game.lineups is Option<LineupsDocument>, a TYPED
    # struct (homeLineup/awayLineup, each with formation/coach/players/
    # bench), not an arbitrary dict. If `lineups` here doesn't match that
    # exact shape, Rust will fail to deserialize the whole Game document
    # once this field is populated -- same class of bug as the field-name
    # mismatch that caused the original "skipping malformed fixture" errors.
    # The actual lineups write path in this codebase goes through the Rust
    # API's own /games/lineups handler (store_lineups in games.rs), which
    # builds the LineupsDocument shape correctly on the Rust side -- these
    # Python-side methods are kept for local/back-compat use but should NOT
    # be the primary write path while that Rust endpoint exists.

    def store_lineups(self, match_id: str, lineups: Dict) -> None:
        """Store lineups and mark as fetched.
        CAUTION: see note above -- prefer forwarding to the Rust
        /games/lineups endpoint (already done via forwarder.py) over
        writing this field directly from Python, to avoid shape drift."""
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$set": {
                    "lineups": lineups,
                    "lineupsFetched": True,
                    "lineupsFetchedAt": datetime.now(timezone.utc),
                    "scrapedAt": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    def mark_lineups_fetched(self, match_id: str) -> None:
        """Mark that lineups have been fetched."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$set": {"lineupsFetched": True, "lineupsFetchedAt": datetime.now(timezone.utc)}}
        )

    def get_lineups(self, match_id: str) -> Optional[Dict]:
        """Get stored lineups for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"lineups": 1, "lineupsFetched": 1}
        )
        return doc.get("lineups") if doc else None

    def lineups_available(self, match_id: str) -> bool:
        """Check if lineups are available for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"lineupsFetched": 1}
        )
        return doc.get("lineupsFetched", False) if doc else False

    # ============================================================
    # STATISTICS
    # ============================================================
    # NOTE: Rust's Game.statistics is Vec<StatisticsSnapshot>, each with a
    # TYPED `statistics: MatchStatistics { home: TeamStatistics, away:
    # TeamStatistics }` shape -- not an arbitrary dict. As with lineups,
    # the Rust API's own /games/statistics handlers (add_statistics_snapshot
    # / bulk_update_statistics in games.rs) build this shape correctly.
    # These Python methods write a generic `stats` dict directly and will
    # cause the same deserialization failure if that dict doesn't match
    # MatchStatistics's exact field names (possession, shots,
    # shotsOnTarget, etc. -- check TeamStatisticsPayload's snake_case
    # Deserialize impl specifically, since unlike Game/CommentaryEntry,
    # TeamStatisticsPayload has NO #[serde(rename)] attributes, meaning it
    # expects snake_case wire keys, not camelCase -- confirm against your
    # actual struct before relying on this path).

    def add_statistics_snapshot(self, match_id: str, stats: Dict, minute: int) -> None:
        """Add a statistics snapshot at a specific minute.
        CAUTION: see note above -- prefer forwarding to the Rust
        /games/statistics endpoint over writing this field directly."""
        snapshot = {
            "minute": minute,
            "statistics": stats,
            "timestamp": datetime.now(timezone.utc)
        }
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$push": {"statistics": snapshot},
                "$set": {
                    "lastStatisticsMinute": minute,
                    "scrapedAt": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    def get_statistics(self, match_id: str) -> List[Dict]:
        """Get all statistics snapshots for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"statistics": 1}
        )
        return doc.get("statistics", []) if doc else []

    def get_latest_statistics(self, match_id: str) -> Optional[Dict]:
        """Get the latest statistics snapshot for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"statistics": 1}
        )
        if doc and doc.get("statistics"):
            return doc["statistics"][-1]
        return None

    # ============================================================
    # EVENTS
    # ============================================================

    def get_forwarded_event_signatures(self, match_id: str) -> set:
        """Get the set of event signatures already forwarded."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"forwardedEventSignatures": 1}
        )
        if not doc:
            return set()
        return set(doc.get("forwardedEventSignatures", []))

    def add_forwarded_event_signature(self, match_id: str, signature: str) -> None:
        """Add a forwarded event signature."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$addToSet": {"forwardedEventSignatures": signature}},
            upsert=False,
        )

    def add_forwarded_event_signatures_bulk(self, match_id: str, signatures: List[str]) -> None:
        """Add multiple forwarded event signatures."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$addToSet": {"forwardedEventSignatures": {"$each": signatures}}},
            upsert=False,
        )

    # ============================================================
    # COMMENTARY
    # ============================================================
    # NOTE: Rust's CommentaryEntry struct requires minute: i32, type:
    # String (renamed from event_type), createdAt: BsonDateTime -- all
    # REQUIRED, no Option. The `entry` dict passed in here must already
    # contain "minute", "type", "createdAt" (or this write will cause the
    # same deserialization failure for this match's document once read
    # back by Rust). flashscore.py's _parse_commentary() already produces
    # this exact shape -- see that file's docstring.

    def add_commentary(self, match_id: str, entry: Dict) -> None:
        """Add a commentary entry. `entry` must already match
        CommentaryEntry's shape: minute (int), text (str), type (str),
        team (optional str), player (optional str), createdAt (RFC3339 str
        or compatible). createdAt is overwritten here to "now" regardless
        of what's passed in, matching the Rust add_commentary handler's
        own behavior (it does `entry.created_at = now` server-side too)."""
        now = datetime.now(timezone.utc)
        entry = dict(entry)
        entry["createdAt"] = now

        self._collection.update_one(
            {"matchId": match_id},
            {
                "$push": {"commentary": entry},
                "$inc": {"commentaryCount": 1},
                "$set": {"lastCommentaryAt": now, "scrapedAt": now},
            },
            upsert=True,
        )

    def add_commentary_bulk(self, match_id: str, entries: List[Dict]) -> None:
        """Add multiple commentary entries. Each entry must already match
        CommentaryEntry's shape (see add_commentary docstring)."""
        now = datetime.now(timezone.utc)
        entries = [dict(e) for e in entries]
        for entry in entries:
            entry["createdAt"] = now

        self._collection.update_one(
            {"matchId": match_id},
            {
                "$push": {"commentary": {"$each": entries}},
                "$inc": {"commentaryCount": len(entries)},
                "$set": {"lastCommentaryAt": now, "scrapedAt": now},
            },
            upsert=True,
        )

    def get_commentary(self, match_id: str, limit: int = 50) -> List[Dict]:
        """Get commentary for a match, sorted by minute."""
        pipeline = [
            {"$match": {"matchId": match_id}},
            {"$unwind": "$commentary"},
            {"$sort": {"commentary.minute": 1}},
            {"$limit": limit},
            {"$project": {"commentary": 1, "_id": 0}}
        ]
        result = list(self._collection.aggregate(pipeline))
        return [r["commentary"] for r in result]

    def get_latest_commentary(self, match_id: str, limit: int = 20) -> List[Dict]:
        """Get latest commentary for a match."""
        pipeline = [
            {"$match": {"matchId": match_id}},
            {"$unwind": "$commentary"},
            {"$sort": {"commentary.createdAt": -1}},
            {"$limit": limit},
            {"$project": {"commentary": 1, "_id": 0}}
        ]
        result = list(self._collection.aggregate(pipeline))
        return [r["commentary"] for r in result]

    # ============================================================
    # MATCH FINALIZATION
    # ============================================================

    def finalize_match(self, match_id: str, result: str, home_score: int, away_score: int) -> None:
        """Finalize a match with its result."""
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$set": {
                    "status": "completed",
                    "isLive": False,
                    "availableForVoting": False,
                    "homeScore": home_score,
                    "awayScore": away_score,
                    "result": result,
                    "completedAt": datetime.now(timezone.utc),
                    "scrapedAt": datetime.now(timezone.utc),
                }
            }
        )

    def move_to_history(self, match_id: str) -> None:
        """Mark a match as moved to history."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$set": {"movedToHistory": True}}
        )

    def archive_completed_fixtures(self, hours: int = 24) -> int:
        """Archive completed fixtures older than N hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = self._collection.update_many(
            {
                "status": "completed",
                "completedAt": {"$lt": cutoff},
                "movedToHistory": False,
            },
            {"$set": {"movedToHistory": True, "archivedAt": datetime.now(timezone.utc)}}
        )
        return result.modified_count

    # ============================================================
    # VOTERS & USER DATA
    # ============================================================
    # NOTE: Rust's Voter struct requires userId, userName, selection,
    # votedAt (camelCase, via #[serde(rename)]). The voter dict built here
    # must match that exactly or this field will fail deserialization too.

    def add_voter(self, match_id: str, user_id: str, user_name: str, selection: str) -> None:
        """Add a voter to a match. Matches Rust's Voter struct shape
        exactly: userId, userName, selection, votedAt."""
        voter = {
            "userId": user_id,
            "userName": user_name,
            "selection": selection,
            "votedAt": datetime.now(timezone.utc),
        }
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$push": {"voters": voter},
                "$inc": {"votes": 1},
            },
            upsert=True,
        )

    def get_voters(self, match_id: str) -> List[Dict]:
        """Get all voters for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"voters": 1}
        )
        return doc.get("voters", []) if doc else []

    def get_vote_count(self, match_id: str) -> int:
        """Get the vote count for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"votes": 1}
        )
        return doc.get("votes", 0) if doc else 0

    def user_has_voted(self, match_id: str, user_id: str) -> bool:
        """Check if a user has voted on a match."""
        doc = self._collection.find_one({
            "matchId": match_id,
            "voters.userId": user_id
        })
        return doc is not None

    # ============================================================
    # BULK OPERATIONS
    # ============================================================

    def upsert_fixtures_bulk(self, fixtures: List[Dict]) -> int:
        """Bulk upsert fixtures. CAUTION: each fixture dict is written
        as-is via replace_one -- callers must ensure dicts already use
        camelCase keys matching the Game struct (e.g. via upsert_fixture's
        doc-building logic), or this bypasses the schema entirely."""
        operations = []
        for fixture in fixtures:
            match_id = fixture.get("matchId")
            if match_id:
                operations.append(
                    {
                        "replace_one": {
                            "filter": {"matchId": match_id},
                            "replacement": fixture,
                            "upsert": True,
                        }
                    }
                )

        if operations:
            result = self._collection.bulk_write(operations)
            return result.upserted_count + result.modified_count
        return 0

    # ============================================================
    # CLEANUP
    # ============================================================

    def delete_old_fixtures(self, days: int = 30) -> int:
        """Delete fixtures older than N days (that are archived)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = self._collection.delete_many({
            "movedToHistory": True,
            "completedAt": {"$lt": cutoff},
        })
        return result.deleted_count

    def close(self) -> None:
        """Close the MongoDB connection."""
        self._client.close()

    # ============================================================
    # AGGREGATION HELPERS
    # ============================================================

    def get_fixture_counts_by_status(self) -> Dict[str, int]:
        """Get count of fixtures by status."""
        pipeline = [
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ]
        results = list(self._collection.aggregate(pipeline))
        return {r["_id"]: r["count"] for r in results}

    def get_upcoming_fixtures_with_lineups(self) -> List[Dict]:
        """Get upcoming fixtures that have lineups available."""
        return list(self._collection.find({
            "status": {"$in": ["upcoming", "soon"]},
            "lineupsFetched": True
        }))

    def get_live_fixtures_with_stats(self) -> List[Dict]:
        """Get live fixtures that have statistics."""
        return list(self._collection.find({
            "status": "live",
            "statistics": {"$exists": True, "$ne": []}
        }))


def create_store(mongo_uri: str = None) -> FixtureStore:
    """Create a FixtureStore instance with optional URI."""
    import os
    if mongo_uri is None:
        mongo_uri = os.environ.get("MONGO_URI")
        if not mongo_uri:
            raise ValueError("MONGO_URI environment variable is required")
    return FixtureStore(mongo_uri)