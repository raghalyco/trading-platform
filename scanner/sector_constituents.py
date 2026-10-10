"""
Fetches each sector's constituent stock list from NSE's free archives —
same pattern as the Nifty 500 list already used in universe.py. URLs are
NSE's standard naming convention, but a few of the less common sectors'
exact filenames aren't 100% verified (this sandbox can't reach NSE's
servers to confirm). Each sector is fetched independently and failures are
skipped with a warning rather than crashing the whole feature — if one
sector's URL is wrong, you still get trending stocks for the other 14.
"""
import os
import time
from typing import Optional

import pandas as pd
import requests

import config

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch_constituents(sector_name: str) -> list:
    """Returns a list of tradingsymbols for the given sector, or [] if the
    fetch failed (prints a warning in that case rather than raising)."""
    url = config.SECTOR_CONSTITUENT_URLS.get(sector_name)
    if not url:
        return []

    safe_name = sector_name.replace(" ", "_").lower()
    cache_file = os.path.join(config.CACHE_DIR, f"sector_constituents_{safe_name}.csv")

    if os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file)) / 3600 < 24:
        try:
            df = pd.read_csv(cache_file)
            return df["Symbol"].astype(str).str.upper().tolist()
        except Exception:
            pass  # fall through and re-fetch

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        with open(cache_file, "wb") as f:
            f.write(resp.content)
        df = pd.read_csv(cache_file)
        df.columns = [c.strip() for c in df.columns]
        return df["Symbol"].astype(str).str.upper().tolist()
    except Exception as e:
        print(f"  [warn] couldn't fetch constituents for '{sector_name}' ({url}): {e} — "
              f"skipping this sector's trending list. If this persists, check/update the "
              f"URL in config.SECTOR_CONSTITUENT_URLS.")
        return []


def build_symbol_sector_map(sector_names: Optional[list] = None) -> dict:
    """{tradingsymbol: sector_name}, built from the same per-sector
    constituent lists (and 24h file cache) as fetch_constituents() above.
    Used by Breakout Radar's "Top Picks" shortlist to avoid picking two
    names out of the same sector move (e.g. GAIL + OIL are both "NIFTY OIL
    AND GAS") — a symbol listed under more than one sector (rare, a few
    thematic indices overlap) keeps whichever sector was fetched first.
    Not meant as an authoritative sector classification, just good enough
    for that one dedupe step."""
    names = sector_names or list(config.SECTOR_CONSTITUENT_URLS.keys())
    out: dict = {}
    for sector_name in names:
        for sym in fetch_constituents(sector_name):
            out.setdefault(sym, sector_name)
    return out
