"""
Retest entry alerts: price breaks a level then comes back to retest it.
4 retest types: ORB level, S/R zone, EMA9, VWAP.
Only fires on 'strong' candles - body >= 65% of the candle's total range,
matching the reference app's filter.
"""

import pandas as pd
from app.indicators.core import ema


def candle_body_pct(row: pd.Series) -> float:
    total_range = row["high"] - row["low"]
    if total_range <= 0:
        return 0.0
    body = abs(row["close"] - row["open"])
    return (body / total_range) * 100


def _vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_vol_price = (typical_price * df["volume"]).cumsum()
    return cum_vol_price / cum_vol.replace(0, pd.NA)


def check_retest(df_1m: pd.DataFrame, orb_range: dict | None,
                  sr_zones: list[float] | None = None,
                  tolerance_pct: float = 0.1, min_body_pct: float = 65.0) -> dict:
    """
    Checks the most recent candle against each retest level type.
    Returns the first matching retest, or {"active": False}.
    """
    if len(df_1m) < 22:
        return {"active": False, "reason": "Not enough data for EMA9/VWAP retest"}

    last = df_1m.iloc[-1]
    body_pct = candle_body_pct(last)
    if body_pct < min_body_pct:
        return {"active": False, "reason": f"Candle body {body_pct:.0f}% < {min_body_pct}% required"}

    close = df_1m["close"]
    ema9 = ema(close, 9).iloc[-1]
    vwap_val = _vwap(df_1m).iloc[-1]
    price = float(last["close"])

    def near(level: float) -> bool:
        return abs(price - level) / level * 100 <= tolerance_pct

    checks = []
    if orb_range:
        checks.append(("ORB level", orb_range.get("high")))
        checks.append(("ORB level", orb_range.get("low")))
    for zone in (sr_zones or []):
        checks.append(("S/R zone", zone))
    checks.append(("EMA9", float(ema9)))
    if pd.notna(vwap_val):
        checks.append(("VWAP", float(vwap_val)))

    for label, level in checks:
        if level is None:
            continue
        if near(level):
            side = "CE" if last["close"] > last["open"] else "PE"
            return {
                "active": True,
                "retest_type": label,
                "level": round(level, 2),
                "price": round(price, 2),
                "body_pct": round(body_pct, 1),
                "side": side,
                "label": f"RETEST CONFIRMED - {label}",
            }

    return {"active": False, "reason": "No level within retest tolerance", "body_pct": round(body_pct, 1)}
