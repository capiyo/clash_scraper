"""
Forwards updates from poller to Rust backend API.
"""
from __future__ import annotations

import logging
import requests
from typing import Dict, Any, Optional
from datetime import datetime, timezone

logger = logging.getLogger("worldcup_poller.forwarder")


class Forwarder:
    def __init__(self, api_url: str):
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def forward_event(self, event: Dict[str, Any]) -> bool:
        """Forward a live event to the Rust API."""
        url = f"{self.api_url}/games/events"
        try:
            response = self.session.post(url, json=event, timeout=10)
            response.raise_for_status()
            logger.debug(f"Event forwarded: {event.get('event_type')} at {event.get('minute')}'")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to forward event: {e}")
            return False

    def forward_commentary(self, commentary: Dict[str, Any]) -> bool:
        """Forward commentary to the Rust API."""
        url = f"{self.api_url}/games/commentary"
        try:
            response = self.session.post(url, json=commentary, timeout=10)
            response.raise_for_status()
            logger.debug(f"Commentary forwarded for {commentary.get('match_id')}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to forward commentary: {e}")
            return False

    def forward_live_update(self, update: Dict[str, Any]) -> bool:
        """Forward a live match update to the Rust API."""
        url = f"{self.api_url}/games/live-update"
        try:
            response = self.session.post(url, json=update, timeout=10)
            response.raise_for_status()
            logger.debug(f"Live update forwarded for {update.get('fixture_id')}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to forward live update: {e}")
            return False

    def forward_lineups(self, lineups: Dict[str, Any]) -> bool:
        """Forward lineups to the Rust API."""
        url = f"{self.api_url}/games/lineups"
        try:
            response = self.session.post(url, json=lineups, timeout=10)
            response.raise_for_status()
            logger.debug(f"Lineups forwarded for {lineups.get('fixture_id')}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to forward lineups: {e}")
            return False

    def forward_statistics(self, statistics: Dict[str, Any]) -> bool:
        """Forward match statistics to the Rust API."""
        url = f"{self.api_url}/games/statistics"
        try:
            response = self.session.post(url, json=statistics, timeout=10)
            response.raise_for_status()
            logger.debug(f"Statistics forwarded for {statistics.get('fixture_id')}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to forward statistics: {e}")
            return False