"""
The 7-point scorer: EMA, MACD, 15M, 5M, 1M, VOL, RSI.
Each component votes CE / PE / NEUTRAL. Majority direction wins;
score = number of components agreeing with that direction (+ PA bonus).
"""

import pandas as pd
from app.indicators.core import ema, macd, rsi, volume_spike
from app.indicators.multi_tf import multi_timeframe_trend
from app.config import CONFIG


def _direction_from_trend(trend: str) -> str:
    return {"BULL": "CE", "BEAR": "PE"}.get(trend, "NEUTRAL")


def score_components(df_1m: pd.DataFrame) -> dict:
    """
    df_1m: 1-minute OHLCV, columns [timestamp, open, high, low, close, volume]
    Returns per-component votes + the raw component dict for the UI's
    colored EMA/MACD/15M/5M/1M/VOL/RSI row.
    """
    close = df_1m["close"]

    ema_fast, ema_slow = ema(close, 9), ema(close, 21)
    ema_vote = "CE" if ema_fast.iloc[-1] > ema_slow.iloc[-1] else "PE"

    macd_line, signal_line, hist = macd(close)
    macd_vote = "CE" if hist.iloc[-1] > 0 else "PE"

    tf_trend = multi_timeframe_trend(df_1m)
    tf_votes = {tf: _direction_from_trend(t) for tf, t in tf_trend.items()}

    vol_flag = volume_spike(df_1m).iloc[-1]
    vol_vote = ema_vote if vol_flag else "NEUTRAL"  # volume confirms prevailing move

    rsi_val = rsi(close).iloc[-1]
    if rsi_val > 60:
        rsi_vote = "CE"
    elif rsi_val < 40:
        rsi_vote = "PE"
    else:
        rsi_vote = "NEUTRAL"

    votes = {
        "EMA": ema_vote,
        "MACD": macd_vote,
        "15M": tf_votes["15M"],
        "5M": tf_votes["5M"],
        "1M": tf_votes["1M"],
        "VOL": vol_vote,
        "RSI": rsi_vote,
    }
    return {"votes": votes, "rsi_value": round(float(rsi_val), 1)}


def compute_score(votes: dict) -> dict:
    """Pick majority side, count agreeing components -> base score /7."""
    ce_count = sum(1 for v in votes.values() if v == "CE")
    pe_count = sum(1 for v in votes.values() if v == "PE")

    side = "CE" if ce_count >= pe_count else "PE"
    base_score = ce_count if side == "CE" else pe_count

    return {"side": side, "score": base_score, "max_score": len(votes)}


def verdict_label(total_score: int, side: str, max_score: int = 7) -> str:
    cfg = CONFIG.signal
    opt = "CE" if side == "CE" else "PE"
    if total_score >= cfg.strong_buy_threshold:
        return f"STRONG BUY {opt}"
    if total_score >= cfg.moderate_buy_threshold:
        return f"MODERATE BUY {opt}"
    if total_score >= 2:
        return f"BUY {opt}"
    return "WAIT - NO SETUP"
