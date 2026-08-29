"""
Entry confidence meter (0-100%, AVOID / CAUTION / ENTER) plus the
caution-list generator (choppy market, expiry day, high SL risk, etc).
"""

import pandas as pd
from app.indicators.core import candle_range_pct
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


def _partial_tf_cautions(df_1m: pd.DataFrame) -> list:
    """A 5m/15m candle is only "closed" on its last constituent 1-min bar
    (minute % 5 == 4, minute % 15 == 14) - any other minute means that
    higher-TF vote in the 7-point scorer is still being built and can
    still flip before the candle closes."""
    if df_1m is None or df_1m.empty:
        return []
    minute = pd.Timestamp(df_1m["timestamp"].iloc[-1]).minute
    cautions = []
    if minute % 5 != 4:
        cautions.append("5M candle still forming - 5M signal may change")
    if minute % 15 != 14:
        cautions.append("15M candle still forming - trend may shift")
    return cautions


def build_cautions(df_1m: pd.DataFrame, is_expiry_day: bool, sl_points: float,
                    spot: float, mode: str = "SCALP", gbb_result: dict | None = None) -> list:
    cautions = []
    cfg = CONFIG.signal

    if mode == "GBB":
        # GBB has its OWN, more complete chop detection (VWAP whipsaw +
        # EMA compression + low ATR percentile - see gbb_setup.py's
        # chop_filter) - the generic single-candle range check below was
        # built for the 7-point scorer and doesn't reflect GBB's actual
        # logic, so it's skipped here in favour of GBB's real state.
        if gbb_result and gbb_result.get("state") == "CHOP":
            cautions.append("Choppy - VWAP whipsaw / EMA compression detected")
        if gbb_result and gbb_result.get("state") == "EXTENDED":
            cautions.append("Extended move - avoid chasing, wait for pullback/retest")
        # GBB's setup timeframe is 5M, not 15M - the 15M-still-forming
        # caution doesn't apply to a mode that never looks at a 15M bar.
        if df_1m is not None and not df_1m.empty:
            minute = pd.Timestamp(df_1m["timestamp"].iloc[-1]).minute
            if minute % 5 != 4:
                cautions.append("5M candle still forming - GBB setup may change")
    else:
        latest_range_pct = candle_range_pct(df_1m).iloc[-1]
        if latest_range_pct < cfg.caution_range_pct:
            cautions.append(f"Choppy - range <{cfg.caution_range_pct}%")
        cautions.extend(_partial_tf_cautions(df_1m))

    if is_expiry_day:
        cautions.append("Expiry day - trade with caution")

    sl_pct_of_spot = (sl_points / spot) * 100
    if sl_pct_of_spot > 0.5:
        cautions.append("High SL risk - wide stop relative to spot")

    return cautions
