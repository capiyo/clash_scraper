"""
Per-endpoint circuit breaker.

Each (fixture, endpoint) pair gets its own breaker entry so one source
going down (e.g. Sofascore commentary getting banned) doesn't take down
unrelated polling (e.g. API-Football fixtures, which has nothing to do
with Sofascore's health).

States: closed (normal) -> open (failing, stop calling) -> half_open
(trial requests allowed) -> closed (recovered) or back to open.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("worldcup_poller.breaker")


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerEntry:
    failure_threshold: int
    cooldown_seconds: float
    half_open_trial_count: int

    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    half_open_trials_used: int = 0

    def allow_request(self) -> bool:
        now = time.monotonic()
        if self.state == BreakerState.CLOSED:
            return True
        if self.state == BreakerState.OPEN:
            if now - self.opened_at >= self.cooldown_seconds:
                self.state = BreakerState.HALF_OPEN
                self.half_open_trials_used = 0
                logger.info("Breaker moving to half-open after cooldown")
                return True
            return False
        if self.state == BreakerState.HALF_OPEN:
            if self.half_open_trials_used < self.half_open_trial_count:
                self.half_open_trials_used += 1
                return True
            return False
        return False

    def record_success(self) -> None:
        if self.state != BreakerState.CLOSED:
            logger.info("Breaker closing after successful trial")
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self.half_open_trials_used = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.state == BreakerState.HALF_OPEN:
            self.state = BreakerState.OPEN
            self.opened_at = time.monotonic()
            logger.warning("Breaker re-opening after failed half-open trial")
            return
        if self.consecutive_failures >= self.failure_threshold:
            self.state = BreakerState.OPEN
            self.opened_at = time.monotonic()
            logger.warning(
                "Breaker opening after %d consecutive failures",
                self.consecutive_failures,
            )


class CircuitBreakerRegistry:
    """Keyed by an arbitrary string, e.g. f'{source}:{endpoint}:{fixture_id}'
    or just f'{source}:{endpoint}' if you want it shared across fixtures."""

    def __init__(self) -> None:
        self._entries: dict[str, BreakerEntry] = {}

    def get_or_create(
        self,
        key: str,
        failure_threshold: int,
        cooldown_seconds: float,
        half_open_trial_count: int = 1,
    ) -> BreakerEntry:
        if key not in self._entries:
            self._entries[key] = BreakerEntry(
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
                half_open_trial_count=half_open_trial_count,
            )
        return self._entries[key]

    def is_open(self, key: str) -> bool:
        entry = self._entries.get(key)
        return entry is not None and entry.state == BreakerState.OPEN


REGISTRY = CircuitBreakerRegistry()
