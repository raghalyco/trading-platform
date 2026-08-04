"""
Wilder's ADX (Average Directional Index) - measures trend STRENGTH,
not direction. Used to classify the market as TRENDING or RANGE-BOUND,
which then gates which signal types should even be considered.

ADX > ~20-25 is conventionally read as "trending enough to trust
breakout/momentum signals." Below that, breakouts are more likely to be
false and mean-reversion/caution is more appropriate - this is exactly
the regime split ORB and Retest signals need.
"""

import pandas as pd
import numpy as np


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def directional_indicators(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series]:
    """Returns (+DI, -DI) series - exposed separately so callers can use
    the DI+/DI- crossover as a directional vote, not just ADX's strength."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = _true_range(df)
    atr_smooth = tr.ewm(alpha=1 / period, adjust=False).mean()

    plus_dm_smooth = pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean()
    minus_dm_smooth = pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean()

    plus_di = 100 * (plus_dm_smooth / atr_smooth.replace(0, np.nan))
    minus_di = 100 * (minus_dm_smooth / atr_smooth.replace(0, np.nan))
    return plus_di.fillna(0), minus_di.fillna(0)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Returns the ADX series (0-100). Higher = stronger trend (either direction)."""
    plus_di, minus_di = directional_indicators(df, period)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_series = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx_series.fillna(0)
