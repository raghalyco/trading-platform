"""
Reimplements the Chartink screen exactly, condition by condition, as a
vectorized boolean series over the full daily history — so the backtest can
check "did this stock signal on day X" for every day in one pass instead of
re-running the scanner per day.

Conditions (all ANDed, cash segment):
 1. Daily Close > Daily EMA(Close, 20)
 2. Daily EMA(Close, 20) > Daily EMA(Close, 50)
 3. Daily EMA(Close, 50) > Daily EMA(Close, 200)
 4. Weekly Close > Weekly EMA(Weekly Close, 20)
 5. Daily Close > [1 day ago Max(20, Daily High)]   (20-day breakout)
 6. Daily Volume > 2.5 * SMA(Volume, 20)
 7. Daily Close >= 0.98 * Daily High
 8. RSI(14) > 60
 9. RSI(14) < 75
10. ADX(14) > 25
11. Market Cap > 2000 Cr            <- applied once per symbol in universe.py
12. Daily Close < 1.15 * EMA(Close, 20)
13. Weekly RSI(14) > 50
"""
import pandas as pd

import config
import indicators as ind


def _weekly_resample(daily: pd.DataFrame) -> pd.DataFrame:
    w = daily.set_index("date").resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    return w.reset_index()


def compute_signals(daily: pd.DataFrame) -> pd.DataFrame:
    """daily: columns [date, open, high, low, close, volume], sorted ascending.
    Returns the same df with indicator columns + a `signal` boolean column.
    Market cap is NOT checked here (handled once per symbol upstream)."""
    df = daily.copy().reset_index(drop=True)

    df["ema20"] = ind.ema(df["close"], config.EMA_FAST)
    df["ema50"] = ind.ema(df["close"], config.EMA_MID)
    df["ema200"] = ind.ema(df["close"], config.EMA_SLOW)
    df["rsi14"] = ind.rsi(df["close"], config.RSI_PERIOD)
    df["adx14"] = ind.adx(df["high"], df["low"], df["close"], config.ADX_PERIOD)
    df["vol_sma20"] = ind.sma(df["volume"], config.VOLUME_SMA_PERIOD)
    df["breakout_level"] = ind.rolling_max_shifted(df["high"], config.BREAKOUT_LOOKBACK, shift=1)

    # Weekly indicators, forward-filled back onto the daily index using the
    # completed week (avoids look-ahead: a given daily row only "sees" the
    # most recently CLOSED weekly candle).
    weekly = _weekly_resample(df[["date", "open", "high", "low", "close", "volume"]])
    weekly["w_ema20"] = ind.ema(weekly["close"], config.WEEKLY_EMA)
    weekly["w_rsi14"] = ind.rsi(weekly["close"], config.RSI_PERIOD)
    weekly = weekly.rename(columns={"close": "w_close", "date": "week_end"})

    df = pd.merge_asof(
        df.sort_values("date"),
        weekly[["week_end", "w_close", "w_ema20", "w_rsi14"]].sort_values("week_end"),
        left_on="date", right_on="week_end",
        direction="backward",
    )

    c1 = df["close"] > df["ema20"]
    c2 = df["ema20"] > df["ema50"]
    c3 = df["ema50"] > df["ema200"]
    c4 = df["w_close"] > df["w_ema20"]
    c5 = df["close"] > df["breakout_level"]
    c6 = df["volume"] > config.VOLUME_MULTIPLIER * df["vol_sma20"]
    c7 = df["close"] >= config.CLOSE_NEAR_HIGH_PCT * df["high"]
    c8 = df["rsi14"] > config.RSI_MIN
    c9 = df["rsi14"] < config.RSI_MAX
    c10 = df["adx14"] > config.ADX_MIN
    c12 = df["close"] < config.EXTENSION_CAP * df["ema20"]
    c13 = df["w_rsi14"] > config.WEEKLY_RSI_MIN

    # Stored individually (not just ANDed into `signal`) so a specific date
    # can be inspected condition-by-condition — see explain_signal.py.
    df["c1_close_gt_ema20"] = c1
    df["c2_ema20_gt_ema50"] = c2
    df["c3_ema50_gt_ema200"] = c3
    df["c4_weekly_close_gt_wema20"] = c4
    df["c5_breakout_20d"] = c5
    df["c6_volume_surge"] = c6
    df["c7_close_near_high"] = c7
    df["c8_rsi_gt_60"] = c8
    df["c9_rsi_lt_75"] = c9
    df["c10_adx_gt_25"] = c10
    df["c12_not_overextended"] = c12
    df["c13_weekly_rsi_gt_50"] = c13

    df["signal"] = c1 & c2 & c3 & c4 & c5 & c6 & c7 & c8 & c9 & c10 & c12 & c13
    return df
