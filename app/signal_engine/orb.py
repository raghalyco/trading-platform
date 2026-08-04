"""
Opening Range Breakout: defines the 9:15-9:30 high/low range, then fires
when a 5-min CLOSED candle closes above/below that range. Only active
after 9:30 AM, matching the reference app's behavior.
"""

import pandas as pd
from datetime import time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _now_ist():
    return pd.Timestamp.now(tz=IST)


def _opening_range(df_1m: pd.DataFrame) -> dict | None:
    """
    Extract today's 9:15-9:30 high/low from 1m candles.
    Assumes df_1m['timestamp'] is IST-naive (this is what Kite Connect
    returns regardless of server timezone). If you're feeding this from
    a different source, make sure timestamps are IST before calling.
    """
    today = _now_ist().date()
    day_df = df_1m[df_1m["timestamp"].dt.date == today]
    if day_df.empty:
        return None

    window = day_df[
        (day_df["timestamp"].dt.time >= time(9, 15))
        & (day_df["timestamp"].dt.time < time(9, 30))
    ]
    if window.empty:
        return None

    return {"high": float(window["high"].max()), "low": float(window["low"].min())}


def check_orb_breakout(df_1m: pd.DataFrame, df_5m: pd.DataFrame) -> dict:
    """
    Returns a signal dict if a valid, closed 5m candle has broken the
    opening range after 9:30 AM IST. Otherwise returns {"active": False}.
    """
    now = _now_ist().time()
    if now < time(9, 30):
        return {"active": False, "reason": "Before 9:30 AM IST - ORB not yet valid"}

    orb = _opening_range(df_1m)
    if orb is None:
        return {"active": False, "reason": "No opening range data available"}

    if len(df_5m) < 1:
        return {"active": False, "reason": "No closed 5m candle yet"}

    last_closed = df_5m.iloc[-1]  # most recent fully closed 5m candle

    if last_closed["close"] > orb["high"]:
        side = "CE"
    elif last_closed["close"] < orb["low"]:
        side = "PE"
    else:
        return {"active": False, "reason": "Price inside opening range", "orb_range": orb}

    return {
        "active": True,
        "side": side,
        "orb_high": round(orb["high"], 2),
        "orb_low": round(orb["low"], 2),
        "breakout_close": round(float(last_closed["close"]), 2),
        "label": f"ORB BREAKOUT {side}",
    }
