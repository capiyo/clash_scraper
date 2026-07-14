"""
Viewer gate.

Gates the most expensive endpoint (commentary) behind "is anyone actually
watching this fixture right now". Without this, you'd poll commentary
for every in-progress fixture at full cadence even if zero users have
the match open — wasteful and the fastest way to burn through breaker
thresholds for no payoff.

Two implementations:

  ViewerGateStub — always says "yes, actively viewed" for every fixture.
  This is the default until the backend endpoint exists. Behaves
  identically to having no gate at all.

  CachedBackendViewerGate — calls a fetch function (you provide it,
  typically a GET to your own Rust API) to get the real set of
  currently-viewed match ids, caches it for a short TTL so you're not
  hitting your own backend on every single poll tick across every
  fixture, and falls back to "treat as active" if the fetch fails
  (fail open, not closed — better to over-poll briefly than to go dark
  on a fixture someone is actually watching because your backend blipped).

NOTE: GET /api/games/active-viewers does NOT currently exist in the
Axum router (routes/games.rs) — this is one of the two remaining
integration points mentioned in project notes. Add that route + handler
before switching main.py over to CachedBackendViewerGate.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Protocol

logger = logging.getLogger("worldcup_poller.viewer_gate")


class ViewerGate(Protocol):
    def is_actively_viewed(self, match_id: str) -> bool:
        ...


class ViewerGateStub:
    def is_actively_viewed(self, match_id: str) -> bool:
        return True


class CachedBackendViewerGate:
    def __init__(
        self,
        fetch_active_fixture_ids: Callable[[], set[str]],
        cache_ttl_seconds: float = 15.0,
    ) -> None:
        self._fetch = fetch_active_fixture_ids
        self._ttl = cache_ttl_seconds
        self._cached_ids: Optional[set[str]] = None
        self._cached_at: float = 0.0

    def _refresh_if_stale(self) -> None:
        now = time.monotonic()
        if self._cached_ids is not None and (now - self._cached_at) < self._ttl:
            return
        try:
            self._cached_ids = self._fetch()
            self._cached_at = now
        except Exception as exc:
            logger.warning(
                "Failed to fetch active viewer ids, failing open (treat all as active): %s",
                exc,
            )
            # Fail open: if we can't reach the backend, don't silently
            # stop polling fixtures someone might actually be watching.
            self._cached_ids = None

    def is_actively_viewed(self, match_id: str) -> bool:
        self._refresh_if_stale()
        if self._cached_ids is None:
            return True
        return match_id in self._cached_ids
