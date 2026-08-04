"""
Lightweight price-action pattern detection on recent swing highs/lows.
Mirrors the 'PA: BEAR | 1/5 score | DOUBLE TOP' line in the reference app.
"""

import pandas as pd
import numpy as np
from app.indicators.core import trend_direction

PA_PATTERNS = ("DOUBLE TOP", "DOUBLE BOTTOM", "NONE")


def _find_swings(close: pd.Series, order: int = 3):
    """Simple local-extrema swing detection without external TA libs."""
    highs, lows = [], []
    vals = close.values
    for i in range(order, len(vals) - order):
        window = vals[i - order : i + order + 1]
        if vals[i] == window.max():
            highs.append((i, vals[i]))
        if vals[i] == window.min():
            lows.append((i, vals[i]))
    return highs, lows


def detect_pattern(df: pd.DataFrame, tol_pct: float = 0.15) -> dict:
    """
    df: OHLCV with 'close' column, most recent row last.
    Returns pattern name, direction, and a 1-5 confidence score.
    """
    close = df["close"]
    highs, lows = _find_swings(close)

    pattern = "NONE"
    score = 1

    if len(highs) >= 2:
        h1, h2 = highs[-2][1], highs[-1][1]
        if abs(h1 - h2) / h1 * 100 <= tol_pct:
            pattern = "DOUBLE TOP"
            score = 4

    if len(lows) >= 2:
        l1, l2 = lows[-2][1], lows[-1][1]
        if abs(l1 - l2) / l1 * 100 <= tol_pct:
            # if both patterns fire, prefer the more recent swing pair
            if pattern == "NONE" or lows[-1][0] > highs[-1][0]:
                pattern = "DOUBLE BOTTOM"
                score = 4

    direction = trend_direction(close) if len(close) > 21 else "NEUT"

    # bump score if pattern direction agrees with trend (reversal confirmation)
    if pattern == "DOUBLE TOP" and direction == "BEAR":
        score = min(score + 1, 5)
    if pattern == "DOUBLE BOTTOM" and direction == "BULL":
        score = min(score + 1, 5)

    return {
        "pattern": pattern,
        "direction": direction,
        "score": score,          # out of 5
        "max_score": 5,
    }


def pa_bonus_points(pa_result: dict, proposed_side: str, max_bonus: int = 2) -> int:
    """
    proposed_side: 'CE' (bullish) or 'PE' (bearish)
    Converts PA confirmation into bonus points for the main 7-point scorer.
    """
    agrees = (
        (proposed_side == "PE" and pa_result["pattern"] == "DOUBLE TOP")
        or (proposed_side == "CE" and pa_result["pattern"] == "DOUBLE BOTTOM")
    )
    if not agrees:
        return 0
    # scale PA's own 1-5 score down to 0-max_bonus
    return round((pa_result["score"] / 5) * max_bonus)
