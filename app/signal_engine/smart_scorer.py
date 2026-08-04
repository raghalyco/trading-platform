"""
SMART TRADE uses a different 6-point score than SCALP: EMA, ADX, OB, FVG,
VOL, OI - reflecting its different style (structure/smart-money-concept
based, held longer, wider stops) versus SCALP's momentum/multi-timeframe
7-point score.
"""

import pandas as pd
from app.indicators.core import ema, volume_spike
from app.indicators.adx import adx, directional_indicators
from app.signal_engine.order_block import detect_order_block
from app.signal_engine.modes import detect_fvg_either
from app.signal_engine.options_data import pcr_vote

SMART_TRADE_COMPONENTS = ("EMA", "ADX", "OB", "FVG", "VOL", "OI")


def score_smart_trade(df: pd.DataFrame, pcr: float | None = None) -> dict:
    close = df["close"]

    # EMA vote - same trend-following idea as SCALP's EMA component
    ema_fast, ema_slow = ema(close, 9), ema(close, 21)
    ema_vote = "CE" if ema_fast.iloc[-1] > ema_slow.iloc[-1] else "PE"

    # ADX vote - uses DI+/DI- crossover for direction, ADX value for whether
    # the trend is strong enough to trust at all
    plus_di, minus_di = directional_indicators(df)
    adx_value = float(adx(df).iloc[-1])
    di_plus, di_minus = float(plus_di.iloc[-1]), float(minus_di.iloc[-1])
    if adx_value < 15:
        adx_vote = "NEUTRAL"  # too weak a trend to trust direction
    else:
        adx_vote = "CE" if di_plus > di_minus else "PE"

    # OB vote
    ob_result = detect_order_block(df)
    ob_vote = ob_result["side"] if ob_result.get("found") else "NEUTRAL"

    # FVG vote
    fvg_result = detect_fvg_either(df)
    fvg_vote = fvg_result["side"] if fvg_result else "NEUTRAL"

    # VOL vote - confirms whichever direction EMA currently favors
    vol_flag = volume_spike(df).iloc[-1]
    vol_vote = ema_vote if vol_flag else "NEUTRAL"

    # OI/PCR vote - NEUTRAL until live option-chain OI is wired in
    # (see options_data.py for exactly what's needed to make this live)
    oi_vote = pcr_vote(pcr)

    votes = {
        "EMA": ema_vote,
        "ADX": adx_vote,
        "OB": ob_vote,
        "FVG": fvg_vote,
        "VOL": vol_vote,
        "OI": oi_vote,
    }

    return {
        "votes": votes,
        "adx_value": round(adx_value, 1),
        "di_plus": round(di_plus, 1),
        "di_minus": round(di_minus, 1),
        "order_block": ob_result,
        "fvg": fvg_result,
        "pcr": pcr,
        "oi_live": pcr is not None,
    }


def compute_smart_score(votes: dict) -> dict:
    ce_count = sum(1 for v in votes.values() if v == "CE")
    pe_count = sum(1 for v in votes.values() if v == "PE")
    side = "CE" if ce_count >= pe_count else "PE"
    score = ce_count if side == "CE" else pe_count
    return {"side": side, "score": score, "max_score": len(votes)}
