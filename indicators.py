"""
Technical indicators, matching Chartink's conventions:
  - EMA: standard exponential moving average
  - RSI: Wilder's smoothing (RMA), the industry-standard RSI, matches Chartink
  - ADX: Wilder's DX/ADX, 14-period, matches Chartink
  - rolling max shifted by 1 day, for the breakout condition
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder's smoothing == EWM with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out = out.where(avg_loss != 0, 100.0)  # if no losses at all, RSI = 100
    return out


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx_val


def rolling_max_shifted(series: pd.Series, period: int, shift: int = 1) -> pd.Series:
    """Highest value over `period` bars, as of `shift` bars ago (excludes
    the current bar when shift=1) — matches Chartink's `N day(s) ago Max(...)`."""
    return series.rolling(period).max().shift(shift)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Per-calendar-day VWAP from HLC3 * volume (resets each session)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    day = pd.to_datetime(df["date"]).dt.date
    pv = typical * df["volume"].astype(float)
    cum_pv = pv.groupby(day).cumsum()
    cum_vol = df["volume"].astype(float).groupby(day).cumsum().replace(0, np.nan)
    return cum_pv / cum_vol
