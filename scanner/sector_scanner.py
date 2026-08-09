"""
Sector Analysis / Breakout scanner: pulls real historical data for NSE's
sector indices (NIFTY BANK, NIFTY IT, etc. — these are themselves tradeable
instruments in Kite, not hand-averaged from constituents) and ranks them by
momentum, flagging sector-level breakouts.

"Breakout" here means the sector index itself just made a fresh
SECTOR_BREAKOUT_LOOKBACK-day high — a rising tide that's likely to lift
individual stocks in that sector too, useful as a pre-filter before drilling
into stock-level scanners.
"""
import pandas as pd

import config
import indicators as ind


def match_sector_indices(index_instruments: pd.DataFrame) -> dict:
    """Maps each configured sector name to the best-matching row in Kite's
    index instrument dump (case-insensitive substring match on `name`,
    since exact tradingsymbol formatting for indices isn't worth hardcoding
    blind). Returns {sector_name: {instrument_token, tradingsymbol}},
    skipping any that couldn't be matched (and printing a warning) rather
    than silently failing later."""
    matched = {}
    names_upper = index_instruments["name"].str.upper().fillna("")
    tsyms_upper = index_instruments["tradingsymbol"].str.upper().fillna("")

    for sector in config.SECTOR_INDEX_NAMES:
        target = sector.upper()
        mask = names_upper.str.contains(target, regex=False) | tsyms_upper.str.contains(target, regex=False)
        candidates = index_instruments[mask]
        if candidates.empty:
            print(f"  [warn] no Kite index instrument matched for sector '{sector}' — skipping. "
                  f"Check config.SECTOR_INDEX_NAMES against your Kite instrument dump if this "
                  f"sector matters to you.")
            continue
        # Prefer the shortest/closest name match (avoids e.g. "NIFTY BANK"
        # accidentally matching "NIFTY PSU BANK" first).
        row = candidates.iloc[(candidates["name"].str.len()).argsort()].iloc[0]
        matched[sector] = {
            "instrument_token": row["instrument_token"],
            "tradingsymbol": row["tradingsymbol"],
        }
    return matched


def compute_sector_metrics(daily: pd.DataFrame) -> dict:
    """daily: columns [date, open, high, low, close, volume] for one sector
    index, sorted ascending. Returns a dict of the latest metrics, or None
    if there isn't enough history yet."""
    df = daily.copy().reset_index(drop=True)
    df["ema20"] = ind.ema(df["close"], config.SECTOR_EMA_PERIOD)
    df["nday_high"] = df["high"].rolling(config.SECTOR_BREAKOUT_LOOKBACK).max().shift(1)

    if len(df) < config.SECTOR_BREAKOUT_LOOKBACK + 2:
        return None

    last = df.iloc[-1]
    prev_1d = df.iloc[-2] if len(df) >= 2 else None
    prev_5d = df.iloc[-6] if len(df) >= 6 else None
    prev_1m = df.iloc[-22] if len(df) >= 22 else None

    def pct_change(prev_row):
        if prev_row is None or prev_row["close"] == 0:
            return None
        return round((last["close"] - prev_row["close"]) / prev_row["close"] * 100, 2)

    breakout = bool(last["close"] > last["nday_high"]) if pd.notna(last["nday_high"]) else False
    above_ema = bool(last["close"] > last["ema20"]) if pd.notna(last["ema20"]) else False

    return {
        "close": round(float(last["close"]), 2),
        "pct_change_1d": pct_change(prev_1d),
        "pct_change_5d": pct_change(prev_5d),
        "pct_change_1m": pct_change(prev_1m),
        "above_ema20": above_ema,
        "distance_from_ema20_pct": (
            round(float((last["close"] - last["ema20"]) / last["ema20"] * 100), 2)
            if pd.notna(last["ema20"]) else None
        ),
        "breakout_20d": breakout,
        "date": str(last["date"].date()),
    }
