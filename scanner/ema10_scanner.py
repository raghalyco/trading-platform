"""
A second, simpler scanner: Close > EMA(10), and not extended more than
EMA10_MAX_DISTANCE_PCT above it — i.e. the stock is trading above its
short-term trend line but hasn't run away from it yet.

Kept as its own module (rather than folded into scanner.py) since it's a
genuinely different, independent screen with its own menu tab in the
dashboard — same pattern you'd see running "10 Day BO" and "50 Day BO" as
separate scanners side by side.
"""
import pandas as pd

import config
import indicators as ind


def compute_signals(daily: pd.DataFrame) -> pd.DataFrame:
    """daily: columns [date, open, high, low, close, volume], sorted ascending.
    Returns the same df with an `ema10` column, `distance_from_ema10_pct`,
    and a `signal` boolean column."""
    df = daily.copy().reset_index(drop=True)

    df["ema10"] = ind.ema(df["close"], config.EMA10_PERIOD)
    df["distance_from_ema10_pct"] = (df["close"] - df["ema10"]) / df["ema10"] * 100

    above_ema = df["close"] > df["ema10"]
    not_extended = df["distance_from_ema10_pct"] <= config.EMA10_MAX_DISTANCE_PCT

    df["signal"] = above_ema & not_extended
    return df
