"""
Gamma Blast: expiry-day premium compression alert.
NIFTY expires Tuesday, SENSEX expires Thursday.
Fires when a separate 0-8 compression score hits 6+.

Premium compression = IV crush + time decay accelerating near expiry,
often preceding a sharp directional move as gamma exposure increases.
This is a simplified proxy score, not a full options-greeks model -
a proper version would pull live IV from the option chain rather than
inferring it from index volatility contraction.
"""

import pandas as pd
from zoneinfo import ZoneInfo
from app.indicators.core import atr, ema

IST = ZoneInfo("Asia/Kolkata")
EXPIRY_WEEKDAY = {"NIFTY": 1, "SENSEX": 3}  # Mon=0 ... Tue=1, Thu=3


def _now_ist():
    return pd.Timestamp.now(tz=IST)


def is_expiry_today(symbol: str) -> bool:
    weekday = EXPIRY_WEEKDAY.get(symbol)
    if weekday is None:
        return False
    return _now_ist().weekday() == weekday


def is_monthly_expiry_today(symbol: str) -> bool:
    """
    UNCERTAIN - current sources conflict on this exact rule. As of this
    writing, published sources disagree on whether NIFTY's monthly expiry
    falls on the last Tuesday or last Thursday of the month (exchange
    expiry-day rules changed in Sept 2025 and reporting on it is
    inconsistent). Defaulting to 'last Tuesday' per the more detailed/
    consistent sources, but VERIFY THIS against NSE's official circular
    or your broker's contract notes before relying on it - a wrong
    monthly-expiry flag could cause you to miss or misjudge real
    IV-crush risk on the actual expiry day.
    """
    weekday = EXPIRY_WEEKDAY.get(symbol)  # same weekday as weekly, per current sources
    if weekday is None:
        return False
    today = _now_ist()
    is_last_occurrence = (today + pd.Timedelta(days=7)).month != today.month
    return today.weekday() == weekday and is_last_occurrence


def gamma_blast_score(df_1m: pd.DataFrame, vix: float) -> dict:
    """
    Proxy scoring (0-8), components:
    - ATR contraction vs its own 30-period average (range compressing)
    - Volume declining into the session (typical pre-move quiet)
    - VIX below 15 (low IV environment, compression more likely)
    - Time proximity to market close (compression alerts most relevant late day)
    """
    score = 0
    reasons = []

    atr_series = atr(df_1m)
    if len(atr_series) >= 30:
        recent_atr = atr_series.iloc[-5:].mean()
        baseline_atr = atr_series.iloc[-30:-5].mean()
        if baseline_atr > 0 and recent_atr < baseline_atr * 0.7:
            score += 3
            reasons.append("ATR contracting vs 30-period baseline")

    if len(df_1m) >= 20:
        recent_vol = df_1m["volume"].iloc[-5:].mean()
        baseline_vol = df_1m["volume"].iloc[-20:-5].mean()
        if baseline_vol > 0 and recent_vol < baseline_vol * 0.8:
            score += 2
            reasons.append("Volume declining into session")

    if vix < 15:
        score += 2
        reasons.append(f"VIX {vix} - low IV environment")

    now = _now_ist().time()
    if now.hour >= 13:  # afternoon session, closer to expiry unwind
        score += 1
        reasons.append("Afternoon session - closer to expiry unwind")

    return {"score": score, "max_score": 8, "reasons": reasons}


def check_gamma_blast(symbol: str, df_1m: pd.DataFrame, vix: float, threshold: int = 6) -> dict:
    if not is_expiry_today(symbol):
        return {"active": False, "reason": f"{symbol} does not expire today"}

    result = gamma_blast_score(df_1m, vix)
    if result["score"] >= threshold:
        return {
            "active": True,
            "label": "GAMMA BLAST - premium compression",
            "score": result["score"],
            "max_score": result["max_score"],
            "reasons": result["reasons"],
        }
    return {"active": False, "reason": f"Score {result['score']}/{result['max_score']} below threshold {threshold}"}
