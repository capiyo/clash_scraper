"""
MongoDB access for the poller. Matches the Rust Game struct exactly.
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
        """Create indexes for fast queries."""
        try:
            self._collection.create_index("match_id", unique=True)
            self._collection.create_index("threesixtyfive_game_id")
            self._collection.create_index("status")
            self._collection.create_index([("status", 1), ("is_live", 1)])
            self._collection.create_index("kickoff_utc")
            self._collection.create_index("scraped_at")
            self._collection.create_index("forwarded_event_signatures")
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
        competition_name: str = "FIFA World Cup 2026",
        odds: dict = None,
    ) -> None:
        """
        Upsert a fixture matching the Rust Game struct.
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

        # Build the document
        doc = {
            "match_id": match_id,
            "threesixtyfive_game_id": threesixtyfive_game_id,
            "home_team": home_team,
            "away_team": away_team,
            "league": competition_name,
            "date": date_str,
            "time": time_str,
            "date_iso": date_iso,
            "kickoff_utc": kickoff_utc,
            "home_score": None,
            "away_score": None,
            "status": status,
            "is_live": is_live,
            "available_for_voting": available_for_voting,
            "home_win": home_win,
            "away_win": away_win,
            "draw": draw,
            "scraped_at": datetime.now(timezone.utc),
            "source": "365scores",
            "last_scraped_at": datetime.now(timezone.utc),
        }

        # Fields that should ONLY be set on insert (user-generated data preserved)
        set_on_insert = {
            "votes": 0,
            "comments": 0,
            "voters": [],
            "commentary": [],
            "commentary_count": 0,
            "last_commentary_at": None,
            "lineups": None,
            "lineups_fetched": False,
            "lineups_fetched_at": None,
            "statistics": [],
            "last_statistics_minute": None,
            "forwarded_event_signatures": [],
            "last_polled_at": None,
            "completed_at": None,
            "moved_to_history": False,
            "created_at": datetime.now(timezone.utc),
        }

        self._collection.update_one(
            {"match_id": match_id},
            {
                "$set": doc,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

    def get_fixture(self, match_id: str) -> Optional[Dict[str, Any]]:
        """Get a single fixture by match_id."""
        return self._collection.find_one({"match_id": match_id})

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
            "kickoff_utc": {"$gte": now, "$lte": cutoff}
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
            "completed_at": {"$lt": cutoff}
        }))

    def get_threesixtyfive_game_id(self, match_id: str) -> Optional[str]:
        """Get the 365Scores game ID for a match."""
        doc = self._collection.find_one(
            {"match_id": match_id},
            {"threesixtyfive_game_id": 1}
        )
        return doc.get("threesixtyfive_game_id") if doc else None

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
            "is_live": is_live,
            "available_for_voting": available_for_voting,
            "scraped_at": datetime.now(timezone.utc),
        }
        
        if status == "completed":
            update["completed_at"] = datetime.now(timezone.utc)
        
        self._collection.update_one(
            {"match_id": match_id},
            {"$set": update}
        )

    def update_score(self, match_id: str, home_score: int, away_score: int) -> None:
        """Update score for a match."""
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$set": {
                    "home_score": home_score,
                    "away_score": away_score,
                    "scraped_at": datetime.now(timezone.utc),
                }
            }
        )

    def update_time_elapsed(self, match_id: str, time_elapsed: int) -> None:
        """Update the elapsed time for a match."""
        self._collection.update_one(
            {"match_id": match_id},
            {"$set": {"time_elapsed": time_elapsed}}
        )

    def mark_live(self, match_id: str) -> None:
        """Mark a match as live."""
        self.update_status(match_id, "live")

    def mark_completed(self, match_id: str) -> None:
        """Mark a match as completed."""
        self.update_status(match_id, "completed")

    def record_last_poll(self, match_id: str) -> None:
        """Record last poll time."""
        self._collection.update_one(
            {"match_id": match_id},
            {"$set": {"last_polled_at": datetime.now(timezone.utc)}}
        )

    # ============================================================
    # LINEUPS
    # ============================================================

    def store_lineups(self, match_id: str, lineups: Dict) -> None:
        """Store lineups and mark as fetched."""
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$set": {
                    "lineups": lineups,
                    "lineups_fetched": True,
                    "lineups_fetched_at": datetime.now(timezone.utc),
                    "scraped_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    def mark_lineups_fetched(self, match_id: str) -> None:
        """Mark that lineups have been fetched."""
        self._collection.update_one(
            {"match_id": match_id},
            {"$set": {"lineups_fetched": True, "lineups_fetched_at": datetime.now(timezone.utc)}}
        )

    def get_lineups(self, match_id: str) -> Optional[Dict]:
        """Get stored lineups for a match."""
        doc = self._collection.find_one(
            {"match_id": match_id},
            {"lineups": 1, "lineups_fetched": 1}
        )
        return doc.get("lineups") if doc else None

    def lineups_available(self, match_id: str) -> bool:
        """Check if lineups are available for a match."""
        doc = self._collection.find_one(
            {"match_id": match_id},
            {"lineups_fetched": 1}
        )
        return doc.get("lineups_fetched", False) if doc else False

    # ============================================================
    # STATISTICS
    # ============================================================

    def add_statistics_snapshot(self, match_id: str, stats: Dict, minute: int) -> None:
        """Add a statistics snapshot at a specific minute."""
        snapshot = {
            "minute": minute,
            "statistics": stats,
            "timestamp": datetime.now(timezone.utc)
        }
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$push": {"statistics": snapshot},
                "$set": {
                    "last_statistics_minute": minute,
                    "scraped_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    def get_statistics(self, match_id: str) -> List[Dict]:
        """Get all statistics snapshots for a match."""
        doc = self._collection.find_one(
            {"match_id": match_id},
            {"statistics": 1}
        )
        return doc.get("statistics", []) if doc else []

    def get_latest_statistics(self, match_id: str) -> Optional[Dict]:
        """Get the latest statistics snapshot for a match."""
        doc = self._collection.find_one(
            {"match_id": match_id},
            {"statistics": 1}
        )
        if doc and doc.get("statistics"):
            return doc["statistics"][-1]
        return None

    # ============================================================
    # EVENTS
    # ============================================================

    def get_forwarded_event_signatures(self, match_id: str) -> set[str]:
        """Get the set of event signatures already forwarded."""
        doc = self._collection.find_one(
            {"match_id": match_id},
            {"forwarded_event_signatures": 1}
        )
        if not doc:
            return set()
        return set(doc.get("forwarded_event_signatures", []))

    def add_forwarded_event_signature(self, match_id: str, signature: str) -> None:
        """Add a forwarded event signature."""
        self._collection.update_one(
            {"match_id": match_id},
            {"$addToSet": {"forwarded_event_signatures": signature}},
            upsert=False,
        )

    def add_forwarded_event_signatures_bulk(self, match_id: str, signatures: List[str]) -> None:
        """Add multiple forwarded event signatures."""
        self._collection.update_one(
            {"match_id": match_id},
            {"$addToSet": {"forwarded_event_signatures": {"$each": signatures}}},
            upsert=False,
        )

    # ============================================================
    # COMMENTARY
    # ============================================================

    def add_commentary(self, match_id: str, entry: Dict) -> None:
        """Add a commentary entry."""
        now = datetime.now(timezone.utc)
        entry["created_at"] = now
        
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$push": {"commentary": entry},
                "$inc": {"commentary_count": 1},
                "$set": {"last_commentary_at": now, "scraped_at": now},
            },
            upsert=True,
        )

    def add_commentary_bulk(self, match_id: str, entries: List[Dict]) -> None:
        """Add multiple commentary entries."""
        now = datetime.now(timezone.utc)
        for entry in entries:
            entry["created_at"] = now
        
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$push": {"commentary": {"$each": entries}},
                "$inc": {"commentary_count": len(entries)},
                "$set": {"last_commentary_at": now, "scraped_at": now},
            },
            upsert=True,
        )

    def get_commentary(self, match_id: str, limit: int = 50) -> List[Dict]:
        """Get commentary for a match, sorted by minute."""
        pipeline = [
            {"$match": {"match_id": match_id}},
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
            {"$match": {"match_id": match_id}},
            {"$unwind": "$commentary"},
            {"$sort": {"commentary.created_at": -1}},
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
            {"match_id": match_id},
            {
                "$set": {
                    "status": "completed",
                    "is_live": False,
                    "available_for_voting": False,
                    "home_score": home_score,
                    "away_score": away_score,
                    "result": result,
                    "completed_at": datetime.now(timezone.utc),
                    "scraped_at": datetime.now(timezone.utc),
                }
            }
        )

    def move_to_history(self, match_id: str) -> None:
        """Mark a match as moved to history."""
        self._collection.update_one(
            {"match_id": match_id},
            {"$set": {"moved_to_history": True}}
        )

    def archive_completed_fixtures(self, hours: int = 24) -> int:
        """Archive completed fixtures older than N hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = self._collection.update_many(
            {
                "status": "completed",
                "completed_at": {"$lt": cutoff},
                "moved_to_history": False,
            },
            {"$set": {"moved_to_history": True, "archived_at": datetime.now(timezone.utc)}}
        )
        return result.modified_count

    # ============================================================
    # VOTERS & USER DATA
    # ============================================================

    def add_voter(self, match_id: str, user_id: str, user_name: str, selection: str) -> None:
        """Add a voter to a match."""
        voter = {
            "user_id": user_id,
            "user_name": user_name,
            "selection": selection,
            "voted_at": datetime.now(timezone.utc),
        }
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$push": {"voters": voter},
                "$inc": {"votes": 1},
            },
            upsert=True,
        )

    def get_voters(self, match_id: str) -> List[Dict]:
        """Get all voters for a match."""
        doc = self._collection.find_one(
            {"match_id": match_id},
            {"voters": 1}
        )
        return doc.get("voters", []) if doc else []

    def get_vote_count(self, match_id: str) -> int:
        """Get the vote count for a match."""
        doc = self._collection.find_one(
            {"match_id": match_id},
            {"votes": 1}
        )
        return doc.get("votes", 0) if doc else 0

    def user_has_voted(self, match_id: str, user_id: str) -> bool:
        """Check if a user has voted on a match."""
        doc = self._collection.find_one({
            "match_id": match_id,
            "voters.user_id": user_id
        })
        return doc is not None

    # ============================================================
    # BULK OPERATIONS
    # ============================================================

    def upsert_fixtures_bulk(self, fixtures: List[Dict]) -> int:
        """Bulk upsert fixtures."""
        operations = []
        for fixture in fixtures:
            match_id = fixture.get("match_id")
            if match_id:
                operations.append(
                    {
                        "replace_one": {
                            "filter": {"match_id": match_id},
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
            "moved_to_history": True,
            "completed_at": {"$lt": cutoff},
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
            "lineups_fetched": True
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