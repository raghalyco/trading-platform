"""
Mirrors TradeBrahma's "Swing Spectrum" (10 Day BO / 50 Day BO): for a given
lookback N, finds stocks trading within NDAY_PROXIMITY_PCT of their N-day
high ("near high" — bullish, breakout-watch) or N-day low ("near low" —
bearish, breakdown-watch).

Unlike scanner.py/ema10_scanner.py this isn't a single boolean signal — a
stock can be near_high, near_low, or neither, so compute_signals returns
both flags plus the distance to each, and the caller decides how to
combine/display them (the dashboard shows both lists together, sorted by
recency, same as the reference).
"""
import pandas as pd

import config


def compute_signals(daily: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """daily: columns [date, open, high, low, close, volume], sorted ascending.
    Returns the same df with rolling N-day high/low, distance columns, and
    near_high / near_low boolean flags for the given lookback."""
    df = daily.copy().reset_index(drop=True)

    # Rolling high/low INCLUDING today's bar — "how close is today's close
    # to the extreme of the last N days including today", matching how a
    # live scanner would present it (today's own high/low counts).
    df["nday_high"] = df["high"].rolling(lookback).max()
    df["nday_low"] = df["low"].rolling(lookback).min()

    df["distance_from_high_pct"] = (df["nday_high"] - df["close"]) / df["nday_high"] * 100
    df["distance_from_low_pct"] = (df["close"] - df["nday_low"]) / df["nday_low"] * 100

    df["near_high"] = df["distance_from_high_pct"] <= config.NDAY_PROXIMITY_PCT
    df["near_low"] = df["distance_from_low_pct"] <= config.NDAY_PROXIMITY_PCT

    return df
