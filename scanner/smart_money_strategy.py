"""
Smart Money / GainzAlgo-style signal engine (Pine v5 port for NSE equities).

Matches smart-money-structure.txt BUY / SELL / READY labels:
  - Momentum (ATR-scaled % bar change)
  - Higher-TF EMA20 + session VWAP trend
  - Lower-TF not opposing and not neutral
  - Volume above long SMA with rising short SMA
  - Close break of prior N-bar high/low
  - Optional CHoCH/BOS confirmation (off by default — Pine draws structure separately)

TP/SL use ATR multiples (points don't translate across NSE price levels).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np
import pandas as pd

import config
import indicators as ind
from charts import tradingview_chart_url


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
    structure: str = "Neutral"

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d.get("chart_url"):
            d["chart_url"] = tradingview_chart_url(self.symbol)
        d["smart_money_signal"] = d.get("signal")
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


def _structure_label(
    choch_buy: pd.Series,
    choch_sell: pd.Series,
    bos_buy: pd.Series,
    bos_sell: pd.Series,
) -> str:
    lookback = config.SMART_MONEY_STRUCTURE_LOOKBACK
    if bool(choch_buy.iloc[-lookback:].any()):
        return "Bullish CHoCH"
    if bool(bos_buy.iloc[-lookback:].any()):
        return "Bullish BOS"
    if bool(choch_sell.iloc[-lookback:].any()):
        return "Bearish CHoCH"
    if bool(bos_sell.iloc[-lookback:].any()):
        return "Bearish BOS"
    return "Neutral"


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


def _compute_gates(
    signal_df: pd.DataFrame,
    htf_df: Optional[pd.DataFrame] = None,
    ltf_df: Optional[pd.DataFrame] = None,
    daily_df: Optional[pd.DataFrame] = None,
) -> Optional[dict]:
    """Pine-style gate snapshot for the latest closed bar."""
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
    atr_val = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else float(
        df["high"].iloc[-1] - df["low"].iloc[-1]
    )
    close = df["close"].astype(float)
    volatility_factor = atr_val / float(close.iloc[-1]) if close.iloc[-1] else 0.0
    momentum_threshold = config.SMART_MONEY_MOMENTUM_THRESHOLD_PCT * (1 + volatility_factor * 2)
    pre_factor = getattr(config, "SMART_MONEY_PRE_MOMENTUM_FACTOR", 0.5) * (1 - volatility_factor * 0.5)
    pre_momentum_threshold = momentum_threshold * max(pre_factor, 0.05)

    price_change = ((close - close.shift(1)) / close.shift(1).replace(0, np.nan) * 100)
    last_pc = float(price_change.iloc[-1]) if pd.notna(price_change.iloc[-1]) else 0.0

    last_high, last_low = _pivot_levels(df["high"], df["low"], length)
    choch_buy, choch_sell, bos_buy, bos_sell = _structure_flags(df, last_high, last_low)
    lookback = config.SMART_MONEY_STRUCTURE_LOOKBACK
    structure_buy = bool((choch_buy | bos_buy).iloc[-lookback:].any())
    structure_sell = bool((choch_sell | bos_sell).iloc[-lookback:].any())
    structure = _structure_label(choch_buy, choch_sell, bos_buy, bos_sell)

    frames = {"signal": df}
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
    trend_daily = (
        int(_trend_series(frames["daily"]).iloc[-1])
        if "daily" in frames and len(frames["daily"]) else 0
    )

    votes = [trend_ltf, trend_signal, trend_htf, trend_daily]
    confidence = _confidence_from_trends(votes)

    vol_avg = ind.sma(df["volume"].astype(float), config.SMART_MONEY_VOLUME_LONG)
    vol_short = ind.sma(df["volume"].astype(float), config.SMART_MONEY_VOLUME_SHORT)
    vol_ok = False
    if (
        pd.notna(vol_avg.iloc[-1]) and pd.notna(vol_short.iloc[-1])
        and pd.notna(vol_short.iloc[-2])
    ):
        vol_ok = bool(
            df["volume"].iloc[-1] > vol_avg.iloc[-1]
            and (vol_short.iloc[-1] - vol_short.iloc[-2]) > 0
        )

    highest = df["high"].rolling(config.SMART_MONEY_BREAKOUT_PERIOD).max().shift(1)
    lowest = df["low"].rolling(config.SMART_MONEY_BREAKOUT_PERIOD).min().shift(1)
    breakout_buy = bool(close.iloc[-1] > highest.iloc[-1]) if pd.notna(highest.iloc[-1]) else False
    breakout_sell = bool(close.iloc[-1] < lowest.iloc[-1]) if pd.notna(lowest.iloc[-1]) else False

    # Pine: buy_lower_tf_ok = not bearish AND not neutral → must be bullish
    #       sell_lower_tf_ok = not bullish AND not neutral → must be bearish
    momentum_buy = last_pc > momentum_threshold
    momentum_sell = last_pc < -momentum_threshold
    htf_buy = trend_htf == 1
    htf_sell = trend_htf == -1
    ltf_buy = trend_ltf == 1
    ltf_sell = trend_ltf == -1

    require_structure = bool(getattr(config, "SMART_MONEY_REQUIRE_STRUCTURE", False))
    buy_core = momentum_buy and htf_buy and ltf_buy and vol_ok and breakout_buy
    sell_core = momentum_sell and htf_sell and ltf_sell and vol_ok and breakout_sell
    buy_ok = buy_core and (structure_buy if require_structure else True)
    sell_ok = sell_core and (structure_sell if require_structure else True)

    # Pine get_ready: between pre-momentum and full momentum, other filters OK.
    ready_buy = (
        getattr(config, "SMART_MONEY_SHOW_GET_READY", True)
        and (last_pc > pre_momentum_threshold) and (last_pc < momentum_threshold)
        and htf_buy and ltf_buy and vol_ok and breakout_buy
        and not buy_ok
    )
    ready_sell = (
        getattr(config, "SMART_MONEY_SHOW_GET_READY", True)
        and (last_pc < -pre_momentum_threshold) and (last_pc > -momentum_threshold)
        and htf_sell and ltf_sell and vol_ok and breakout_sell
        and not sell_ok
    )

    entry = float(close.iloc[-1])
    return {
        "df": df,
        "atr": atr_val,
        "entry": entry,
        "last_pc": last_pc,
        "momentum_threshold": momentum_threshold,
        "buy_ok": buy_ok,
        "sell_ok": sell_ok,
        "ready_buy": ready_buy,
        "ready_sell": ready_sell,
        "structure_buy": structure_buy,
        "structure_sell": structure_sell,
        "structure": structure,
        "vol_ok": vol_ok,
        "breakout_buy": breakout_buy,
        "breakout_sell": breakout_sell,
        "trend_htf": trend_htf,
        "trend_ltf": trend_ltf,
        "trend_signal": trend_signal,
        "trend_daily": trend_daily,
        "confidence": confidence,
    }


def classify_symbol(
    symbol: str,
    signal_df: pd.DataFrame,
    sector: str = "",
    htf_df: Optional[pd.DataFrame] = None,
    ltf_df: Optional[pd.DataFrame] = None,
    daily_df: Optional[pd.DataFrame] = None,
) -> dict:
    """Always return a Pine-style label for the latest bar.

    smart_money_signal: BUY | SELL | READY | NEUTRAL
    """
    gates = _compute_gates(signal_df, htf_df=htf_df, ltf_df=ltf_df, daily_df=daily_df)
    base = {
        "symbol": symbol,
        "sector": sector,
        "smart_money_signal": "NEUTRAL",
        "smart_money_side": None,
        "signal": None,
        "confidence": 0.0,
        "structure": "Neutral",
        "structure_buy": False,
        "structure_sell": False,
        "entry_price": None,
        "stop_loss": None,
        "target": None,
        "risk_reward": None,
        "atr": None,
        "timestamp": None,
        "conditions": {},
        "chart_url": tradingview_chart_url(symbol),
    }
    if gates is None:
        return base

    atr_val = gates["atr"]
    entry = gates["entry"]
    if gates["buy_ok"] and gates["sell_ok"]:
        side = "BUY" if gates["trend_htf"] >= 0 else "SELL"
        label = side
    elif gates["buy_ok"]:
        side = label = "BUY"
    elif gates["sell_ok"]:
        side = label = "SELL"
    elif gates["ready_buy"]:
        side, label = "BUY", "READY"
    elif gates["ready_sell"]:
        side, label = "SELL", "READY"
    else:
        side, label = None, "NEUTRAL"

    stop = target = rr = None
    if side == "BUY":
        stop = entry - config.SMART_MONEY_SL_ATR_MULT * atr_val
        target = entry + config.SMART_MONEY_TP_ATR_MULT * atr_val
    elif side == "SELL":
        stop = entry + config.SMART_MONEY_SL_ATR_MULT * atr_val
        target = entry - config.SMART_MONEY_TP_ATR_MULT * atr_val
    if stop is not None and target is not None:
        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = round(reward / risk, 2) if risk > 0 else 0.0

    ts = pd.to_datetime(gates["df"]["date"].iloc[-1]).isoformat()
    conditions = {
        "momentum": gates["buy_ok"] or gates["sell_ok"] or gates["ready_buy"] or gates["ready_sell"],
        "htf_trend": gates["trend_htf"],
        "ltf_trend": gates["trend_ltf"],
        "volume": gates["vol_ok"],
        "breakout_buy": gates["breakout_buy"],
        "breakout_sell": gates["breakout_sell"],
        "structure_buy": gates["structure_buy"],
        "structure_sell": gates["structure_sell"],
        "price_change_pct": round(gates["last_pc"], 4),
        "momentum_threshold_pct": round(gates["momentum_threshold"], 4),
        "trend_htf": gates["trend_htf"],
        "trend_ltf": gates["trend_ltf"],
        "trend_signal": gates["trend_signal"],
        "trend_daily": gates["trend_daily"],
        "ready_buy": gates["ready_buy"],
        "ready_sell": gates["ready_sell"],
    }
    return {
        "symbol": symbol,
        "sector": sector,
        "smart_money_signal": label,
        "smart_money_side": side,
        "signal": label if label in ("BUY", "SELL") else None,
        "confidence": float(gates["confidence"]),
        "structure": gates["structure"],
        "structure_buy": gates["structure_buy"],
        "structure_sell": gates["structure_sell"],
        "entry_price": round(entry, 2),
        "stop_loss": round(stop, 2) if stop is not None else None,
        "target": round(target, 2) if target is not None else None,
        "risk_reward": rr,
        "atr": round(atr_val, 2),
        "timestamp": ts,
        "conditions": conditions,
        "chart_url": tradingview_chart_url(symbol),
    }


def evaluate_symbol(
    symbol: str,
    signal_df: pd.DataFrame,
    sector: str = "",
    htf_df: Optional[pd.DataFrame] = None,
    ltf_df: Optional[pd.DataFrame] = None,
    daily_df: Optional[pd.DataFrame] = None,
) -> Optional[SmartMoneySignal]:
    """Return a signal for the latest *closed* bar if BUY or SELL gates pass."""
    label = classify_symbol(
        symbol=symbol,
        signal_df=signal_df,
        sector=sector,
        htf_df=htf_df,
        ltf_df=ltf_df,
        daily_df=daily_df,
    )
    side = label.get("smart_money_signal")
    if side not in ("BUY", "SELL"):
        return None
    if label.get("entry_price") is None or label.get("stop_loss") is None:
        return None

    return SmartMoneySignal(
        symbol=symbol,
        signal=side,
        entry_price=label["entry_price"],
        stop_loss=label["stop_loss"],
        target=label["target"],
        risk_reward=label["risk_reward"] or 0.0,
        timestamp=label["timestamp"] or "",
        confidence=label["confidence"],
        sector=sector,
        conditions=label.get("conditions") or {},
        atr=label["atr"] or 0.0,
        chart_url=label.get("chart_url") or tradingview_chart_url(symbol),
        structure=label.get("structure") or "Neutral",
    )


def attach_smart_money_label(
    symbol: str,
    daily: pd.DataFrame,
    result: Optional[dict] = None,
    htf_df: Optional[pd.DataFrame] = None,
) -> dict:
    """Stamp Pine BUY/SELL/READY/NEUTRAL fields onto a scanner result row."""
    out = result if result is not None else {}
    weekly = htf_df
    if weekly is None and daily is not None and not daily.empty:
        weekly = _resample_ohlcv(daily, "W-FRI")
    label = classify_symbol(
        symbol=symbol,
        signal_df=daily,
        htf_df=weekly,
        ltf_df=daily,
        daily_df=daily,
    )
    out["smart_money_signal"] = label.get("smart_money_signal") or "NEUTRAL"
    out["smart_money_side"] = label.get("smart_money_side")
    out["structure"] = label.get("structure")
    out["market_structure"] = label.get("structure")
    out["confidence"] = label.get("confidence")
    # Don't overwrite scanner-specific entry/SL when already set from a full signal.
    if out.get("entry_price") is None and label.get("entry_price") is not None:
        out["entry_price"] = label["entry_price"]
    if out.get("stop_loss") is None and label.get("stop_loss") is not None:
        out["stop_loss"] = label["stop_loss"]
    if out.get("target") is None and label.get("target") is not None:
        out["target"] = label["target"]
    if out.get("risk_reward") is None and label.get("risk_reward") is not None:
        out["risk_reward"] = label["risk_reward"]
    out["sm_conditions"] = label.get("conditions") or {}
    return out
