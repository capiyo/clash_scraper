"""
MongoDB access for the poller. Matches the existing data model:
database `clashdb`, collection `fixtures`, match_id format `wc26_{365id}`.

Documents match the Rust Game struct exactly:
- match_id: String
- home_team: String
- away_team: String
- league: String
- date: String (YYYY-MM-DD)
- time: String (HH:MM)
- date_iso: String (YYYY-MM-DDTHH:MM:SSZ)
- home_score: Option<i32>
- away_score: Option<i32>
- status: String (upcoming/live/completed)
- is_live: bool
- available_for_voting: bool
- home_win: Option<f64>
- away_win: Option<f64>
- draw: Option<f64>
- votes: i32
- comments: i32
- voters: Vec<Voter>
- commentary: Vec<CommentaryEntry>
- commentary_count: i32
- last_commentary_at: Option<BsonDateTime>
- scraped_at: BsonDateTime
- source: String
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

import config

logger = logging.getLogger("worldcup_poller.mongo")


class FixtureStore:
    def __init__(self, mongo_uri: str):
        self._client = MongoClient(mongo_uri)
        self._collection: Collection = self._client[config.MONGO_DB][config.MONGO_COLLECTION]
        
        # Ensure indexes
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create indexes for fast queries."""
        try:
            self._collection.create_index("match_id", unique=True)
            self._collection.create_index("status")
            self._collection.create_index("date_iso")
            self._collection.create_index([("status", 1), ("is_live", 1)])
            self._collection.create_index("scraped_at")
            logger.info("MongoDB indexes ensured")
        except Exception as e:
            logger.warning(f"Index creation issue: {e}")

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
        Upserts a fixture matching the Rust Game struct.
        Uses $setOnInsert for fields that should never be overwritten (votes, comments, etc.)
        """
        # Parse date/time for Rust struct
        date_str = kickoff_utc.strftime("%Y-%m-%d")
        time_str = kickoff_utc.strftime("%H:%M")
        date_iso = kickoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Extract odds
        home_win = None
        away_win = None
        draw = None
        if odds:
            home_win = odds.get("homeWin")
            away_win = odds.get("awayWin")
            draw = odds.get("draw")

        # Determine is_live and available_for_voting
        is_live = status == "live"
        available_for_voting = status in ("upcoming", "soon")

        # Build the document matching Rust Game struct
        doc = {
            "match_id": match_id,
            "threesixtyfive_game_id": threesixtyfive_game_id,
            "home_team": home_team,
            "away_team": away_team,
            "league": competition_name,
            "date": date_str,
            "time": time_str,
            "date_iso": date_iso,
            "home_score": None,
            "away_score": None,
            "status": status,
            "is_live": is_live,
            "available_for_voting": available_for_voting,
            "home_win": home_win,
            "away_win": away_win,
            "draw": draw,
            "votes": 0,
            "comments": 0,
            "voters": [],
            "commentary": [],
            "commentary_count": 0,
            "last_commentary_at": None,
            "scraped_at": datetime.now(timezone.utc),
            "source": "365scores",
            "last_scraped_at": datetime.now(timezone.utc),
        }

        # Fields that should NEVER be overwritten (user-generated data)
        set_on_insert = {
            "votes": 0,
            "comments": 0,
            "voters": [],
            "commentary": [],
            "commentary_count": 0,
            "last_commentary_at": None,
        }

        # Update operation
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$set": doc,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

    def get_in_progress_fixtures(self) -> list[dict[str, Any]]:
        """Fixtures currently live, needing score/event polling."""
        return list(self._collection.find({"status": "live"}))

    def get_upcoming_fixtures(self) -> list[dict[str, Any]]:
        """Upcoming fixtures (available for voting)."""
        return list(self._collection.find({"status": "upcoming"}))

    def get_threesixtyfive_game_id(self, match_id: str) -> Optional[str]:
        doc = self._collection.find_one(
            {"match_id": match_id}, {"threesixtyfive_game_id": 1}
        )
        return doc.get("threesixtyfive_game_id") if doc else None

    def get_game(self, match_id: str) -> Optional[dict[str, Any]]:
        """Get full game document by match_id."""
        return self._collection.find_one({"match_id": match_id})

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

    def update_status(self, match_id: str, status: str) -> None:
        """Update match status."""
        is_live = status == "live"
        available_for_voting = status in ("upcoming", "soon")
        
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$set": {
                    "status": status,
                    "is_live": is_live,
                    "available_for_voting": available_for_voting,
                    "scraped_at": datetime.now(timezone.utc),
                }
            }
        )

    def add_commentary(self, match_id: str, entry: dict) -> None:
        """Add a commentary entry."""
        now = datetime.now(timezone.utc)
        entry["created_at"] = now
        
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$push": {"commentary": entry},
                "$inc": {"commentary_count": 1},
                "$set": {"last_commentary_at": now},
            }
        )

    def get_forwarded_event_signatures(self, match_id: str) -> set[str]:
        """Returns the set of event signatures already forwarded."""
        doc = self._collection.find_one(
            {"match_id": match_id}, {"forwarded_event_signatures": 1}
        )
        if not doc:
            return set()
        return set(doc.get("forwarded_event_signatures", []))

    def add_forwarded_event_signature(self, match_id: str, signature: str) -> None:
        self._collection.update_one(
            {"match_id": match_id},
            {"$addToSet": {"forwarded_event_signatures": signature}},
            upsert=False,
        )

    def record_last_poll(self, match_id: str) -> None:
        self._collection.update_one(
            {"match_id": match_id},
            {"$set": {"last_polled_at": datetime.now(timezone.utc)}},
        )

    def close(self) -> None:
        self._client.close()