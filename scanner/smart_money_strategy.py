"""
Smart Money / GainzAlgo-style signal engine (Pine v5 port for NSE equities).

Evaluates closed candles only. Entry requires ALL of:
  - ATR-scaled bar momentum
  - Higher-TF EMA20 + session VWAP trend alignment
  - Lower-TF not opposing (and not neutral)
  - Volume above long SMA with rising short SMA
  - Close break of prior N-bar high/low
  - Recent CHoCH or BOS in the signal direction (structure confirmation)
  - Min bars since last signal (caller can also de-dupe)

Unlike the original Pine overlay, CHoCH/BOS are required for entries here.
TP/SL use ATR multiples (points don't translate across NSE price levels).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np
import pandas as pd

import config
import indicators as ind


TIMEFRAME_RULES = {
    "5minute": "5min",
    "15minute": "15min",
    "30minute": "30min",
    "60minute": "60min",
}


@dataclass
class SmartMoneySignal:
    symbol: str
    signal: str  # BUY | SELL
    entry_price: float
    stop_loss: float
    target: float
    risk_reward: float
    timestamp: str
    confidence: float
    sector: str
    conditions: dict
    atr: float
    chart_url: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d.get("chart_url"):
            d["chart_url"] = f"https://www.tradingview.com/chart/?symbol=NSE:{self.symbol}"
        return d


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    x = df.copy()
    x = x.set_index(pd.to_datetime(x["date"]))
    out = x.resample(rule, label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open", "close"]).reset_index()
    out = out.rename(columns={"index": "date"})
    if "date" not in out.columns:
        out.columns = ["date", "open", "high", "low", "close", "volume"]
    return out


def _trend_series(df: pd.DataFrame) -> pd.Series:
    """+1 / -1 / 0 from EMA20 + session VWAP (Pine multi-TF trend)."""
    if len(df) < 25:
        return pd.Series(0, index=df.index)
    ema20 = ind.ema(df["close"], 20)
    vwap = ind.session_vwap(df)
    bull = (df["close"] > ema20) & (df["close"] > vwap)
    bear = (df["close"] < ema20) & (df["close"] < vwap)
    return pd.Series(np.where(bull, 1, np.where(bear, -1, 0)), index=df.index)


def _pivot_levels(high: pd.Series, low: pd.Series, length: int):
    """Confirmed pivots (lag = length). Returns last_high / last_low series
    forward-filled after each confirmed pivot."""
    ph = high.shift(length)
    pl = low.shift(length)
    is_ph = pd.Series(True, index=high.index)
    is_pl = pd.Series(True, index=low.index)
    for i in range(0, length + 1):
        if i == length:
            continue
        is_ph &= ph >= high.shift(i)
        is_pl &= pl <= low.shift(i)
    for i in range(length + 1, 2 * length + 1):
        is_ph &= ph > high.shift(i)
        is_pl &= pl < low.shift(i)

    last_high = pd.Series(np.nan, index=high.index, dtype=float)
    last_low = pd.Series(np.nan, index=low.index, dtype=float)
    last_high = last_high.mask(is_ph.fillna(False), ph).ffill()
    last_low = last_low.mask(is_pl.fillna(False), pl).ffill()
    return last_high, last_low


def _structure_flags(df: pd.DataFrame, last_high: pd.Series, last_low: pd.Series):
    """CHoCH / BOS boolean series (aligned with Pine definitions)."""
    prev_last_low = last_low.shift(1)
    prev_last_high = last_high.shift(1)

    choch_sell = (
        (df["low"] < last_high) & (df["low"].shift(1) >= last_high.shift(1)) & (df["close"] < df["open"])
    )
    choch_buy = (
        (df["high"] > last_low) & (df["high"].shift(1) <= last_low.shift(1)) & (df["close"] > df["open"])
    )
    bos_sell = (
        (df["low"] < prev_last_low) & (df["low"].shift(1) >= prev_last_low)
        & (df["close"] < df["open"])
    )
    bos_buy = (
        (df["high"] > prev_last_high) & (df["high"].shift(1) <= prev_last_high)
        & (df["close"] > df["open"])
    )
    return choch_buy, choch_sell, bos_buy, bos_sell


def _confidence_from_trends(trend_votes: list[int]) -> float:
    raw = sum(trend_votes)
    n = max(len(trend_votes), 1)
    if abs(raw) == n:
        return 90.0
    if abs(raw) >= max(n * 0.55, 2):
        return 75.0
    if abs(raw) >= max(n * 0.3, 1):
        return 60.0
    return 50.0


def evaluate_symbol(
    symbol: str,
    signal_df: pd.DataFrame,
    sector: str = "",
    htf_df: Optional[pd.DataFrame] = None,
    ltf_df: Optional[pd.DataFrame] = None,
    daily_df: Optional[pd.DataFrame] = None,
) -> Optional[SmartMoneySignal]:
    """Return a signal for the latest *closed* bar if all conditions pass."""
    df = signal_df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    min_bars = max(
        config.SMART_MONEY_PIVOT_LENGTH * 2 + 5,
        config.SMART_MONEY_VOLUME_LONG + 5,
        30,
    )
    if len(df) < min_bars:
        return None

    length = config.SMART_MONEY_PIVOT_LENGTH
    atr_s = ind.atr(df["high"], df["low"], df["close"], 14)
    atr_val = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else float(df["high"].iloc[-1] - df["low"].iloc[-1])
    close = df["close"].astype(float)
    volatility_factor = atr_val / float(close.iloc[-1]) if close.iloc[-1] else 0.0
    momentum_threshold = config.SMART_MONEY_MOMENTUM_THRESHOLD_PCT * (1 + volatility_factor * 2)

    price_change = ((close - close.shift(1)) / close.shift(1).replace(0, np.nan) * 100)
    last_pc = float(price_change.iloc[-1]) if pd.notna(price_change.iloc[-1]) else 0.0

    last_high, last_low = _pivot_levels(df["high"], df["low"], length)
    choch_buy, choch_sell, bos_buy, bos_sell = _structure_flags(df, last_high, last_low)

    lookback = config.SMART_MONEY_STRUCTURE_LOOKBACK
    structure_buy = bool((choch_buy | bos_buy).iloc[-lookback:].any())
    structure_sell = bool((choch_sell | bos_sell).iloc[-lookback:].any())

    # Multi-TF trends (use provided frames or resample from signal TF)
    frames = {}
    frames["signal"] = df
    if htf_df is not None and not htf_df.empty:
        frames["htf"] = htf_df.reset_index(drop=True)
    else:
        rule = TIMEFRAME_RULES.get(config.SMART_MONEY_HTF_INTERVAL)
        frames["htf"] = _resample_ohlcv(df, rule) if rule else df
    if ltf_df is not None and not ltf_df.empty:
        frames["ltf"] = ltf_df.reset_index(drop=True)
    else:
        frames["ltf"] = df
    if daily_df is not None and not daily_df.empty:
        frames["daily"] = daily_df.reset_index(drop=True)

    trend_htf = int(_trend_series(frames["htf"]).iloc[-1]) if len(frames["htf"]) else 0
    trend_ltf = int(_trend_series(frames["ltf"]).iloc[-1]) if len(frames["ltf"]) else 0
    trend_signal = int(_trend_series(df).iloc[-1])
    trend_daily = int(_trend_series(frames["daily"]).iloc[-1]) if "daily" in frames and len(frames["daily"]) else 0

    votes = [t for t in (trend_ltf, trend_signal, trend_htf, trend_daily) if True]
    confidence = _confidence_from_trends(votes)

    vol_avg = ind.sma(df["volume"].astype(float), config.SMART_MONEY_VOLUME_LONG)
    vol_short = ind.sma(df["volume"].astype(float), config.SMART_MONEY_VOLUME_SHORT)
    vol_ok = bool(
        df["volume"].iloc[-1] > vol_avg.iloc[-1]
        and (vol_short.iloc[-1] - vol_short.iloc[-2]) > 0
    ) if pd.notna(vol_avg.iloc[-1]) and pd.notna(vol_short.iloc[-1]) and pd.notna(vol_short.iloc[-2]) else False

    highest = df["high"].rolling(config.SMART_MONEY_BREAKOUT_PERIOD).max().shift(1)
    lowest = df["low"].rolling(config.SMART_MONEY_BREAKOUT_PERIOD).min().shift(1)
    breakout_buy = bool(close.iloc[-1] > highest.iloc[-1]) if pd.notna(highest.iloc[-1]) else False
    breakout_sell = bool(close.iloc[-1] < lowest.iloc[-1]) if pd.notna(lowest.iloc[-1]) else False

    momentum_buy = last_pc > momentum_threshold
    momentum_sell = last_pc < -momentum_threshold
    htf_buy = trend_htf == 1
    htf_sell = trend_htf == -1
    ltf_buy = trend_ltf == 1  # not bearish and not neutral
    ltf_sell = trend_ltf == -1

    buy_ok = (
        momentum_buy and htf_buy and ltf_buy and vol_ok
        and breakout_buy and structure_buy
    )
    sell_ok = (
        momentum_sell and htf_sell and ltf_sell and vol_ok
        and breakout_sell and structure_sell
    )

    if not buy_ok and not sell_ok:
        return None
    if buy_ok and sell_ok:
        # Prefer the side matching HTF if both somehow fire
        side = "BUY" if trend_htf >= 0 else "SELL"
    else:
        side = "BUY" if buy_ok else "SELL"

    entry = float(close.iloc[-1])
    if side == "BUY":
        stop = entry - config.SMART_MONEY_SL_ATR_MULT * atr_val
        target = entry + config.SMART_MONEY_TP_ATR_MULT * atr_val
    else:
        stop = entry + config.SMART_MONEY_SL_ATR_MULT * atr_val
        target = entry - config.SMART_MONEY_TP_ATR_MULT * atr_val

    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    conditions = {
        "momentum": True,
        "htf_trend": True,
        "ltf_trend": True,
        "volume": vol_ok,
        "breakout": breakout_buy if side == "BUY" else breakout_sell,
        "structure_choch_bos": structure_buy if side == "BUY" else structure_sell,
        "price_change_pct": round(last_pc, 4),
        "momentum_threshold_pct": round(momentum_threshold, 4),
        "trend_htf": trend_htf,
        "trend_ltf": trend_ltf,
        "trend_signal": trend_signal,
        "trend_daily": trend_daily,
    }

    ts = pd.to_datetime(df["date"].iloc[-1])
    return SmartMoneySignal(
        symbol=symbol,
        signal=side,
        entry_price=round(entry, 2),
        stop_loss=round(stop, 2),
        target=round(target, 2),
        risk_reward=rr,
        timestamp=ts.isoformat(),
        confidence=confidence,
        sector=sector,
        conditions=conditions,
        atr=round(atr_val, 2),
        chart_url=f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}",
    )
