"""
Pure-function technical indicators. All take/return pandas Series or floats.
No I/O, no broker calls -> fully unit-testable.
"""

import pandas as pd
import numpy as np


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """df must have columns: high, low, close"""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def volume_spike(df: pd.DataFrame, period: int = 20, mult: float = 1.5) -> pd.Series:
    """True where current volume > mult * rolling average volume"""
    avg_vol = df["volume"].rolling(period, min_periods=1).mean()
    return df["volume"] > (avg_vol * mult)


def candle_range_pct(df: pd.DataFrame) -> pd.Series:
    """Latest candle range as % of close - kept for display/debugging."""
    return ((df["high"] - df["low"]) / df["close"]) * 100


def relative_choppiness(df: pd.DataFrame, lookback: int = 20) -> float:
    """
    Ratio of the latest candle's range to the average range of the
    preceding `lookback` candles. < 1.0 means below-average volatility
    (potentially choppy); this self-normalizes across instruments and
    price levels instead of using a fixed % of price, which breaks down
    between e.g. NIFTY (~24000) and SENSEX (~77000).
    """
    if len(df) < lookback + 1:
        return 1.0  # not enough history - assume normal, don't flag
    ranges = df["high"] - df["low"]
    latest = ranges.iloc[-1]
    baseline = ranges.iloc[-(lookback + 1):-1].mean()
    if baseline <= 0:
        return 1.0
    return float(latest / baseline)


def trend_direction(series: pd.Series, fast: int = 9, slow: int = 21) -> str:
    """BULL / BEAR / NEUT based on fast vs slow EMA"""
    f, s = ema(series, fast), ema(series, slow)
    if f.iloc[-1] > s.iloc[-1]:
        return "BULL"
    if f.iloc[-1] < s.iloc[-1]:
        return "BEAR"
    return "NEUT"
