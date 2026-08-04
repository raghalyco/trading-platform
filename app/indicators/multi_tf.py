"""
Resamples 1-minute OHLCV into 5m / 15m and checks whether each timeframe's
trend agrees with the proposed trade direction. Feeds the '15M / 5M / 1M'
rows of the 7-point scorer.
"""

import pandas as pd
from app.indicators.core import trend_direction


def resample_ohlcv(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    out = df_1m.resample(rule, on="timestamp").agg(agg).dropna()
    return out


def multi_timeframe_trend(df_1m: pd.DataFrame) -> dict:
    """Returns trend label for 1m, 5m, 15m timeframes."""
    df_5m = resample_ohlcv(df_1m, "5min")
    df_15m = resample_ohlcv(df_1m, "15min")

    return {
        "1M": trend_direction(df_1m.set_index("timestamp")["close"]),
        "5M": trend_direction(df_5m["close"]) if len(df_5m) > 21 else "NEUT",
        "15M": trend_direction(df_15m["close"]) if len(df_15m) > 21 else "NEUT",
    }
