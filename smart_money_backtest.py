"""
Smart Money Stock-for-day backtest over the last N months (default 3).

For each symbol in STOCK_FOR_DAY_UNIVERSE (Nifty 50/100):
  1. Build historical BUY signals with vectorized Smart Money gates
  2. Simulate trades with the shared backtest engine (next-open entry,
     TARGET_MAX_PCT / time-stop / optional stop-loss)
  3. Return the same summary + trade-log shape as /api/backtest
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta
from tqdm import tqdm

import backtest
import config
import indicators as ind
import smart_money_strategy as sms
import universe as universe_mod


def _weekly_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    if daily is None or daily.empty:
        return daily
    return sms._resample_ohlcv(daily, "W-FRI")


def _align_weekly_trend_to_daily(daily: pd.DataFrame, weekly: pd.DataFrame) -> pd.Series:
    """Map weekly EMA20+VWAP trend onto each daily bar (as-of prior week close)."""
    if weekly is None or weekly.empty or len(weekly) < 25:
        return pd.Series(0, index=daily.index, dtype=int)
    w = weekly.copy().reset_index(drop=True)
    w_trend = sms._trend_series(w)
    w_dates = pd.to_datetime(w["date"])
    d_dates = pd.to_datetime(daily["date"])
    # asof merge: each daily date gets the latest weekly trend on/before it
    left = pd.DataFrame({"date": d_dates, "_i": np.arange(len(daily))}).sort_values("date")
    right = pd.DataFrame({"date": w_dates, "trend": w_trend.values}).sort_values("date")
    merged = pd.merge_asof(left, right, on="date", direction="backward")
    out = pd.Series(0, index=daily.index, dtype=int)
    valid = merged["trend"].notna()
    out.iloc[merged.loc[valid, "_i"].astype(int)] = merged.loc[valid, "trend"].astype(int).values
    return out


def build_smart_money_signal_frame(daily: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Vectorized BUY signal series for bars inside [start, end]."""
    df = daily.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    n = len(df)
    signals = pd.Series(False, index=df.index)

    min_bars = max(
        config.SMART_MONEY_PIVOT_LENGTH * 2 + 5,
        config.SMART_MONEY_VOLUME_LONG + 5,
        60,
    )
    if n < min_bars:
        out = df.copy()
        out["signal"] = signals
        return out

    close = df["close"].astype(float)
    atr_s = ind.atr(df["high"], df["low"], df["close"], 14)
    atr_s = atr_s.fillna((df["high"] - df["low"]).astype(float))
    vol_factor = (atr_s / close.replace(0, np.nan)).fillna(0)
    mom_thr = config.SMART_MONEY_MOMENTUM_THRESHOLD_PCT * (1 + vol_factor * 2)
    price_change = ((close - close.shift(1)) / close.shift(1).replace(0, np.nan) * 100)

    last_high, last_low = sms._pivot_levels(df["high"], df["low"], config.SMART_MONEY_PIVOT_LENGTH)
    choch_buy, _choch_sell, bos_buy, _bos_sell = sms._structure_flags(df, last_high, last_low)
    lookback = config.SMART_MONEY_STRUCTURE_LOOKBACK
    structure_buy = (choch_buy | bos_buy).rolling(lookback, min_periods=1).max().astype(bool)

    trend_daily = sms._trend_series(df)
    weekly = _weekly_from_daily(df)
    trend_htf = _align_weekly_trend_to_daily(df, weekly)

    vol_avg = ind.sma(df["volume"].astype(float), config.SMART_MONEY_VOLUME_LONG)
    vol_short = ind.sma(df["volume"].astype(float), config.SMART_MONEY_VOLUME_SHORT)
    vol_ok = (df["volume"].astype(float) > vol_avg) & (vol_short.diff() > 0)

    highest = df["high"].rolling(config.SMART_MONEY_BREAKOUT_PERIOD).max().shift(1)
    breakout_buy = close > highest

    momentum_buy = price_change > mom_thr
    htf_buy = trend_htf == 1
    ltf_buy = trend_daily == 1  # daily is both signal + LTF in this backtest

    buy = momentum_buy & htf_buy & ltf_buy & vol_ok & breakout_buy & structure_buy

    dates = pd.to_datetime(df["date"]).dt.date
    in_window = (dates >= start) & (dates <= end)
    idx_ok = pd.Series(df.index >= min_bars, index=df.index)
    signals = (buy & in_window & idx_ok).fillna(False)

    out = df.copy()
    out["signal"] = signals.astype(bool)
    return out


def run_stock_for_day_backtest(kite_client, months: Optional[int] = None) -> dict:
    months = months if months is not None else config.STOCK_FOR_DAY_BACKTEST_MONTHS
    end = date.today()
    start = end - relativedelta(months=months)

    mode = (config.STOCK_FOR_DAY_UNIVERSE or "nifty100").strip().lower()
    scan_df = universe_mod.build_nifty_index_universe(kite_client, mode)
    label = {"nifty50": "Nifty 50", "nifty100": "Nifty 100", "nifty500": "Nifty 500"}.get(mode, mode)

    from_date = start - timedelta(days=365)
    to_date = end

    all_trades = []
    print(f"Stock for day backtest: {label}, {start} -> {end} ({months}m)...")

    for _, row in tqdm(scan_df.iterrows(), total=len(scan_df),
                        desc=f"SM backtest ({label}, {months}m)"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, to_date)
            if daily.empty or len(daily) < 60:
                continue
            sig_df = build_smart_money_signal_frame(daily, start, end)
            trades = backtest.simulate_trades(sig_df, symbol, start=start, end=end)
            all_trades.extend(trades)
        except Exception as e:
            print(f"  [warn] SM backtest skipped {symbol}: {e}")
            continue

    trades_df = pd.DataFrame(all_trades)
    summary = backtest.summarize(trades_df)
    trades = [] if trades_df.empty else trades_df.to_dict(orient="records")
    trades.sort(key=lambda t: t["signal_date"], reverse=True)

    return {
        "window_start": str(start),
        "window_end": str(end),
        "months": months,
        "universe_mode": mode,
        "universe_label": label,
        "universe_size": int(len(scan_df)),
        "summary": summary,
        "trades": trades,
    }
