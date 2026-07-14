"""
MongoDB access for the poller. Matches the existing data model:
database `clashdb`, collection `fixtures`, match_id format `wc26_{365id}`.

Post-pivot: no more sofascore_event_id, no preferred_commentary_source —
365Scores is the only live source. threesixtyfive_game_id is the only
external-id field this poller cares about. Sofascore_id still exists as
an Option<i64> on the Rust Game model (harmless, unused going forward).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import MongoClient
from pymongo.collection import Collection

import config

logger = logging.getLogger("worldcup_poller.mongo")


class FixtureStore:
    def __init__(self, mongo_uri: str):
        self._client = MongoClient(mongo_uri)
        self._collection: Collection = self._client[config.MONGO_DB][config.MONGO_COLLECTION]

    def get_in_progress_fixtures(self) -> list[dict[str, Any]]:
        """Fixtures currently live, needing score/event polling."""
        return list(self._collection.find({"status": "live"}))

    def upsert_fixture(
        self,
        match_id: str,
        threesixtyfive_game_id: str,
        home_team: str,
        away_team: str,
        kickoff_utc: datetime,
        status: str,
    ) -> None:
        """Used by scraper.py during fixture discovery. Upserts on
        match_id so re-running the scraper is safe and idempotent — it
        won't duplicate fixtures or clobber poller-owned fields like
        last_event_signature.
        """
        self._collection.update_one(
            {"match_id": match_id},
            {
                "$set": {
                    "match_id": match_id,
                    "threesixtyfive_game_id": threesixtyfive_game_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "kickoff_utc": kickoff_utc,
                    "status": status,
                    "last_scraped_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
            },
            upsert=True,
        )

    def get_threesixtyfive_game_id(self, match_id: str) -> Optional[str]:
        doc = self._collection.find_one(
            {"match_id": match_id}, {"threesixtyfive_game_id": 1}
        )
        return doc.get("threesixtyfive_game_id") if doc else None

    def set_threesixtyfive_game_id(self, match_id: str, game_id: str) -> None:
        self._collection.update_one(
            {"match_id": match_id},
            {"$set": {"threesixtyfive_game_id": game_id}},
            upsert=False,
        )

    def get_forwarded_event_signatures(self, match_id: str) -> set[str]:
        """Returns the set of event signatures already forwarded to the
        backend for this fixture, so the same goal/card/sub doesn't get
        pushed twice across poll cycles. Signature is built by the
        caller (poller.py) — typically f'{event_type}:{minute}:{team}'."""
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
