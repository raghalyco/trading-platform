"""
Entry confidence meter (0-100%, AVOID / CAUTION / ENTER) plus the
caution-list generator (choppy market, expiry day, high SL risk, etc).
"""

import pandas as pd
from app.indicators.core import candle_range_pct, relative_choppiness
from app.config import CONFIG


def confidence_pct(total_score: int, max_score: int = 9) -> int:
    """max_score = 7 base + up to 2 PA bonus"""
    return round((total_score / max_score) * 100)


def confidence_label(pct: int) -> str:
    if pct < 35:
        return "VERY LOW - SKIP"
    if pct < 55:
        return "LOW - CAUTION"
    if pct < 75:
        return "MODERATE"
    return "HIGH - ENTER"


def build_cautions(df_1m: pd.DataFrame, is_expiry_day: bool, sl_points: float,
                    spot: float) -> list:
    cautions = []

    choppiness_ratio = relative_choppiness(df_1m)
    if choppiness_ratio < 0.5:
        cautions.append(f"Choppy - candle range {round(choppiness_ratio*100)}% of recent average")

    if is_expiry_day:
        cautions.append("Expiry day - trade with caution")

    sl_pct_of_spot = (sl_points / spot) * 100
    if sl_pct_of_spot > 0.5:
        cautions.append("High SL risk - wide stop relative to spot")

    return cautions
