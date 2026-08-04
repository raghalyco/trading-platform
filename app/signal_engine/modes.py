"""
SCALP mode: fast 1m in/out, exit at T1, max 5 min hold.
SMART TRADE mode: CO+FVG (Cover Order + Fair Value Gap) - enter on candle
close, hold for wider T1/T2, trail SL after T1 is hit.
"""

import pandas as pd
from datetime import timedelta
from app.indicators.core import atr
from app.signal_engine.order_block import detect_order_block
from app.config import CONFIG

EXPIRY_WEEKDAY = {"NIFTY": 1, "SENSEX": 3}  # Tue=1, Thu=3


def current_expiry_date(symbol: str) -> str:
    """Returns the nearest upcoming (or today's) weekly expiry as 'DDMMMYY' - for display."""
    weekday = EXPIRY_WEEKDAY.get(symbol)
    if weekday is None:
        return "--"
    today = pd.Timestamp.now(tz="Asia/Kolkata")
    days_ahead = (weekday - today.weekday()) % 7
    expiry = today + timedelta(days=days_ahead)
    return expiry.strftime("%d%b%y").upper()


def current_expiry_date_iso(symbol: str) -> str:
    """Same expiry, in 'YYYY-MM-DD' format - what Kite's instrument data uses."""
    weekday = EXPIRY_WEEKDAY.get(symbol)
    if weekday is None:
        return None
    today = pd.Timestamp.now(tz="Asia/Kolkata")
    days_ahead = (weekday - today.weekday()) % 7
    expiry = today + timedelta(days=days_ahead)
    return expiry.strftime("%Y-%m-%d")


def detect_fvg(df: pd.DataFrame, side: str) -> dict | None:
    """
    3-candle Fair Value Gap: gap between candle[0].high and candle[2].low
    (bullish) or candle[0].low and candle[2].high (bearish).
    Returns the most recent unfilled gap, if any.
    """
    if len(df) < 3:
        return None
    c0, c2 = df.iloc[-3], df.iloc[-1]
    if side == "CE" and c2["low"] > c0["high"]:
        return {"gap_low": float(c0["high"]), "gap_high": float(c2["low"])}
    if side == "PE" and c2["high"] < c0["low"]:
        return {"gap_low": float(c2["high"]), "gap_high": float(c0["low"])}
    return None


def detect_fvg_either(df: pd.DataFrame) -> dict | None:
    """Direction-agnostic version of detect_fvg - checks both bullish and
    bearish gaps and returns whichever is present, for use as a scoring
    component rather than a level-calculation input."""
    bullish = detect_fvg(df, "CE")
    if bullish:
        return {"side": "CE", **bullish}
    bearish = detect_fvg(df, "PE")
    if bearish:
        return {"side": "PE", **bearish}
    return None


def scalp_levels(entry_price: float, side: str, atr_value: float) -> dict:
    cfg = CONFIG.scalp
    sign = 1 if side == "CE" else -1
    t1 = entry_price + sign * atr_value * cfg.target1_atr_mult
    t2 = entry_price + sign * atr_value * cfg.target2_atr_mult
    sl = entry_price - sign * atr_value * cfg.stop_atr_mult

    risk = abs(entry_price - sl)
    reward = abs(t1 - entry_price)
    rr = round(reward / risk, 1) if risk else 0.0

    return {
        "mode": "SCALP",
        "timeframe": cfg.timeframe,
        "entry": round(entry_price, 2),
        "target1": round(t1, 2),
        "target2": round(t2, 2),
        "stop_loss": round(sl, 2),
        "rr": rr,
        "max_hold_minutes": cfg.max_hold_minutes,
        "note": "1M setup - quick in/out, exit at T1, max 5 min hold",
    }


def smart_trade_levels(df: pd.DataFrame, side: str, atr_value: float, symbol: str = "NIFTY") -> dict:
    cfg = CONFIG.smart
    entry_price = float(df.iloc[-1]["close"])
    sign = 1 if side == "CE" else -1

    fvg = detect_fvg(df, side)
    ob = detect_order_block(df)
    t1 = entry_price + sign * atr_value * cfg.target1_atr_mult
    t2 = entry_price + sign * atr_value * cfg.target2_atr_mult
    sl = entry_price - sign * atr_value * cfg.stop_atr_mult

    risk = abs(entry_price - sl)
    reward = abs(t1 - entry_price)
    rr = round(reward / risk, 1) if risk else 0.0

    note = "OB+FVG setup - enter on candle close, trail SL after T1"
    if fvg:
        note += f" | FVG zone {fvg['gap_low']}-{fvg['gap_high']}"

    return {
        "mode": "SMART TRADE",
        "timeframe": cfg.timeframe,
        "entry": round(entry_price, 2),
        "target1": round(t1, 2),
        "target2": round(t2, 2),
        "stop_loss": round(sl, 2),
        "rr": rr,
        "trail_after_t1": cfg.trail_after_t1,
        "trail_distance": round(atr_value * cfg.trail_atr_mult, 2),
        "fvg": fvg,
        "order_block": ob,
        "ob_label": ob.get("label", "No OB found"),
        "expiry": current_expiry_date(symbol),
        "note": note,
    }


def trailing_stop_update(position: dict, current_price: float) -> float:
    """
    Called on every tick for open SMART TRADE positions once T1 is hit.
    Ratchets SL in the direction of the trade, never loosens it.
    """
    cfg = CONFIG.smart
    side = position["side"]
    sign = 1 if side == "CE" else -1
    candidate_sl = current_price - sign * cfg.trail_atr_mult * position.get("atr", 0)

    old_sl = position["stop_loss"]
    if side == "CE":
        return max(old_sl, candidate_sl)
    return min(old_sl, candidate_sl)
