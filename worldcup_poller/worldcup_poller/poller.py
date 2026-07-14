"""
Core poller: runs an async polling loop per live fixture, pulling score +
events from 365Scores and forwarding to fanclash-api.

No identity/curl_cffi layer anymore — 365Scores works fine with plain
`requests`, and there's no MongoClient-vs-curl_cffi construction-order
hazard to worry about (that hazard was specific to curl_cffi's IPv6
resolution path, which this architecture no longer touches).

Event dedup: 365Scores' game/detail endpoint returns the full events
list every time, not deltas. Without dedup, the same goal would get
re-forwarded every 15s for as long as the match is live, re-triggering
goal notifications repeatedly. FixtureStore.get_forwarded_event_signatures
/ add_forwarded_event_signature handles this — see _event_signature().
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import config
import forwarder
from mongo_store import FixtureStore
from sources import threesixtyfive

logger = logging.getLogger("worldcup_poller.poller")


def _event_signature(match_id: str, event: dict[str, Any]) -> str:
    """Stable identity for a single event so repeated polls of the same
    match don't re-forward (and re-notify) the same goal/card/sub.
    365Scores doesn't give events a stable id in what we've inspected,
    so this is built from (event_type, minute, team) — good enough
    since two goals by the same team in the same minute are rare, and
    if it does happen we'd rather risk a missed forward than a
    duplicate notification storm."""
    return f"{match_id}:{event['event_type']}:{event.get('minute')}:{event.get('team')}"


class WorldCupPoller:
    def __init__(
        self,
        store: FixtureStore,
        poller_config: config.PollerConfig | None = None,
    ) -> None:
        self._store = store
        self._config = poller_config or config.DEFAULT_CONFIG

        self._running = False
        self._last_request_at: float = 0.0
        self._request_lock = asyncio.Lock()
        self._live_semaphore = asyncio.Semaphore(self._config.live_concurrency_limit)

    def stop(self) -> None:
        self._running = False

    async def _respect_global_spacing(self) -> None:
        async with self._request_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            wait = config.MIN_REQUEST_SPACING_SECONDS - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def _poll_fixture_once(self, fixture: dict[str, Any]) -> None:
        match_id = fixture["match_id"]
        game_id = fixture.get("threesixtyfive_game_id")

        if not game_id:
            game_id = await asyncio.to_thread(
                threesixtyfive.resolve_game_id,
                fixture.get("home_team", ""), fixture.get("away_team", ""),
            )
            if game_id:
                self._store.set_threesixtyfive_game_id(match_id, game_id)
            else:
                logger.warning("Could not resolve 365Scores game_id for %s", match_id)
                return

        async with self._live_semaphore:
            await self._respect_global_spacing()
            game = await asyncio.to_thread(threesixtyfive.fetch_game_detail, game_id)

        if game is None:
            logger.warning("No game detail returned for %s (game_id=%s)", match_id, game_id)
            return

        score_status = threesixtyfive.extract_score_and_status(game)
        events = threesixtyfive.extract_events(game)

        already_forwarded = self._store.get_forwarded_event_signatures(match_id)
        new_events = [
            e for e in events
            if _event_signature(match_id, e) not in already_forwarded
        ]

        if new_events:
            for event in new_events:
                ok = forwarder.forward_live_update(
                    match_id=match_id,
                    event_type=event["event_type"],
                    home_score=score_status["home_score"],
                    away_score=score_status["away_score"],
                    minute=int(event.get("minute") or 0),
                    minute_display=event.get("minute_display", ""),
                    team=event.get("team"),
                    player=event.get("player"),
                )
                if ok:
                    self._store.add_forwarded_event_signature(
                        match_id, _event_signature(match_id, event)
                    )
                    logger.info(
                        "Forwarded %s for %s at %s' (team=%s)",
                        event["event_type"], match_id,
                        event.get("minute_display"), event.get("team"),
                    )
        else:
            # No new discrete events this cycle — still push a plain
            # score/status sync so the UI's clock/score stays current
            # even during quiet stretches of play.
            forwarder.forward_score_only(
                match_id=match_id,
                home_score=score_status["home_score"],
                away_score=score_status["away_score"],
                time_elapsed=score_status["minute"],
            )

        normalized_status = score_status["normalized_status"]
        if normalized_status == "match_end":
            forwarder.forward_live_update(
                match_id=match_id,
                event_type="match_end",
                home_score=score_status["home_score"],
                away_score=score_status["away_score"],
                minute=int(score_status["minute"] or 90),
                minute_display=score_status.get("minute_display", "FT"),
            )

        self._store.record_last_poll(match_id)

    async def _fixture_loop(self, fixture: dict[str, Any]) -> None:
        interval = self._config.live.poll_interval_seconds
        match_id = fixture["match_id"]
        while self._running:
            try:
                await self._poll_fixture_once(fixture)
            except Exception:
                logger.exception("Poll loop error for %s", match_id)
            await asyncio.sleep(interval)

    async def run_forever(self) -> None:
        self._running = True
        logger.info("WorldCupPoller starting (365Scores-only)")

        active_loops: dict[str, asyncio.Task] = {}

        while self._running:
            fixtures = await asyncio.to_thread(self._store.get_in_progress_fixtures)

            for fixture in fixtures:
                match_id = fixture["match_id"]
                if match_id not in active_loops:
                    logger.info("Starting poll loop for %s", match_id)
                    active_loops[match_id] = asyncio.create_task(self._fixture_loop(fixture))

            current_ids = {f["match_id"] for f in fixtures}
            for match_id in list(active_loops.keys()):
                if match_id not in current_ids:
                    logger.info("Stopping poll loop for %s (no longer live)", match_id)
                    active_loops[match_id].cancel()
                    del active_loops[match_id]

            await asyncio.sleep(self._config.fixtures.poll_interval_seconds)

        for task in active_loops.values():
            task.cancel()
