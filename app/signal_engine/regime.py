"""
Classifies the current market as TRENDING or RANGE-BOUND using ADX on the
5-min timeframe (smoother/less noisy than 1-min for this purpose).

This is used to gate ORB Breakout and Retest signals - both are momentum/
breakout style setups that produce far more false signals in range-bound
conditions. Rather than just showing a caution banner, we suppress those
signal types outright when the regime doesn't support them, and say why.
"""

import pandas as pd
from app.indicators.adx import adx


def classify_regime(df_5m: pd.DataFrame, trend_threshold: float = 20.0,
                     strong_trend_threshold: float = 30.0) -> dict:
    if len(df_5m) < 20:
        return {"regime": "UNKNOWN", "adx": None, "reason": "Not enough 5m history yet"}

    adx_value = float(adx(df_5m).iloc[-1])

    if adx_value >= strong_trend_threshold:
        regime = "STRONG_TREND"
    elif adx_value >= trend_threshold:
        regime = "TRENDING"
    else:
        regime = "RANGE"

    return {"regime": regime, "adx": round(adx_value, 1)}


def gate_breakout_signal(signal: dict, regime_info: dict) -> dict:
    """
    Suppresses an ORB/Retest signal (sets active=False with an explanatory
    reason) if the market regime is RANGE - these signal types rely on
    follow-through that range-bound markets don't reliably provide.
    Does not modify the signal if it wasn't active anyway.
    """
    if not signal.get("active"):
        return signal

    if regime_info["regime"] == "RANGE":
        suppressed = dict(signal)
        suppressed["active"] = False
        suppressed["reason"] = (
            f"Suppressed - range-bound regime (ADX {regime_info['adx']}), "
            f"breakout signals unreliable here"
        )
        suppressed["suppressed_by_regime"] = True
        return suppressed

    return signal
