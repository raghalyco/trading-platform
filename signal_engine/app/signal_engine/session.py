"""
Time-of-day session classification - a different dimension from the
ADX-based TRENDING/RANGE regime. Indian index markets have well-known
intraday volume/volatility patterns: an active opening, a "dead zone"
midday lull, and a "power hour" close.

These boundaries are a reasonable approximation, not an exact science -
worth tuning against your own observed volume patterns over time.
"""

from datetime import time
import pandas as pd
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

SESSIONS = [
    (time(9, 15), time(9, 30), "OPENING RANGE"),
    (time(9, 30), time(11, 30), "MORNING TREND"),
    (time(11, 30), time(13, 30), "DEAD ZONE"),
    (time(13, 30), time(14, 45), "AFTERNOON"),
    (time(14, 45), time(15, 30), "POWER HOUR"),
]


def get_session_label(now: pd.Timestamp | None = None) -> str:
    now = now or pd.Timestamp.now(tz=IST)
    t = now.time()

    if t < time(9, 15) or t >= time(15, 30):
        return "MARKET CLOSED"

    for start, end, label in SESSIONS:
        if start <= t < end:
            return label

    return "UNKNOWN"
