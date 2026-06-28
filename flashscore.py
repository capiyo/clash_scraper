"""
Flashscore live commentary fetcher.
"""
from __future__ import annotations

import logging
import re
import requests
from typing import List, Dict, Any, Optional

logger = logging.getLogger("worldcup_poller.flashscore")

# Cache match IDs in memory
_flashscore_cache = {}


def get_flashscore_id(home_team: str, away_team: str) -> Optional[str]:
    """
    Get Flashscore match ID with caching.
    First checks cache, then searches if not found.
    """
    cache_key = f"{home_team}_{away_team}"
    
    # Check cache first
    if cache_key in _flashscore_cache:
        cached_id = _flashscore_cache[cache_key]
        logger.debug(f"Flashscore ID for {home_team} vs {away_team}: {cached_id} (cached)")
        return cached_id
    
    # Not in cache, search for it
    match_id = _find_flashscore_match_id(home_team, away_team)
    
    # Store in cache (even if None, to avoid repeated failed searches)
    _flashscore_cache[cache_key] = match_id
    logger.debug(f"Flashscore ID for {home_team} vs {away_team}: {match_id} (new)")
    return match_id


def _find_flashscore_match_id(home_team: str, away_team: str) -> Optional[str]:
    """Find Flashscore match ID using team names."""
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36",
        "Referer": "https://www.flashscore.co.ke/"
    }
    
    # Search for the match
    search_url = f"https://www.flashscore.co.ke/search/{home_team} {away_team}/"
    
    try:
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Look for match ID pattern in the response
        # Pattern examples:
        # /match/football/colombia-G02s4PCS/portugal-WvJrjFVN/
        # data-id="vL2qotaK"
        # href="/match/football/.../.../summary/"
        
        # Try different patterns
        patterns = [
            r'data-id="([A-Za-z0-9_]+)"',
            r'/match/football/[^/]+-[A-Za-z0-9]+/[^/]+-([A-Za-z0-9]+)/',
            r'feed/df_lcpo_1_([A-Za-z0-9_]+)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, response.text)
            if matches:
                match_id = matches[0]
                logger.info(f"Found Flashscore match ID: {match_id}")
                return match_id
        
        logger.warning(f"No Flashscore match found for {home_team} vs {away_team}")
        return None
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to search Flashscore: {e}")
        return None


def fetch_live_commentary(home_team: str, away_team: str) -> List[Dict[str, Any]]:
    """Fetch live commentary using team names."""
    
    # Get match ID (with caching)
    match_id = get_flashscore_id(home_team, away_team)
    if not match_id:
        logger.warning(f"No Flashscore match ID for {home_team} vs {away_team}")
        return []
    
    # Fetch commentary
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36",
        "Referer": "https://www.flashscore.co.ke/",
        "x-fsign": "SW9D1eZo",
        "Accept": "application/json",
        "Origin": "https://www.flashscore.co.ke"
    }
    
    url = f"https://43.flashscore.ninja/43/x/feed/df_lcpo_1_{match_id}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        entries = _parse_commentary(response.text)
        logger.debug(f"Fetched {len(entries)} commentary entries for {match_id}")
        return entries
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch commentary for {match_id}: {e}")
        return []


def _parse_commentary(raw: str) -> List[Dict[str, Any]]:
    """Parse Flashscore commentary format."""
    entries = []
    
    # Split by ¬~MB÷ which starts each commentary entry
    parts = raw.split("¬~MB÷")
    
    for part in parts:
        if not part.strip():
            continue
        
        # Extract time (¬MK÷)
        time_match = re.search(r"¬MK÷([^¬]+)", part)
        time = time_match.group(1) if time_match else ""
        
        # Extract commentary text (¬MD÷)
        text_match = re.search(r"¬MD÷([^¬]+)", part)
        text = text_match.group(1) if text_match else ""
        
        # Clean up formatting
        text = text.replace("¬", "").strip()
        
        if text:
            entries.append({
                "time": time.strip(),
                "text": text,
                "source": "flashscore"
            })
    
    return entries


def clear_cache():
    """Clear the Flashscore cache (useful for testing)."""
    global _flashscore_cache
    _flashscore_cache = {}
    logger.info("Flashscore cache cleared")


def get_cache_size() -> int:
    """Get the number of cached entries."""
    return len(_flashscore_cache)