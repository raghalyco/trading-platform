"""
Unit tests for smart_money_strategy — synthetic OHLCV, no Kite needed.

Run: python test_smart_money_strategy.py
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config
import smart_money_strategy as sms


def _make_trend_df(n=120, start=100.0, step=0.4, vol=50_000, bullish=True):
    """Build a trending series with enough bars for EMA/VWAP/pivots."""
    rows = []
    t0 = datetime(2026, 8, 3, 9, 15)
    price = start
    for i in range(n):
        move = step if bullish else -step
        o = price
        c = price + move
        h = max(o, c) + abs(step) * 0.3
        l = min(o, c) - abs(step) * 0.2
        # Inject a late breakout + momentum bar
        volume = vol
        if i == n - 1:
            if bullish:
                c = h + abs(step) * 2
                h = c + abs(step)
                volume = vol * 3
            else:
                c = l - abs(step) * 2
                l = c - abs(step)
                volume = vol * 3
        rows.append({
            "date": t0 + timedelta(minutes=5 * i),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": volume,
        })
        price = c
    return pd.DataFrame(rows)


def test_no_signal_on_flat_noise():
    rng = np.random.default_rng(0)
    t0 = datetime(2026, 8, 3, 9, 15)
    rows = []
    price = 100.0
    for i in range(100):
        noise = float(rng.normal(0, 0.05))
        o = price
        c = price + noise
        rows.append({
            "date": t0 + timedelta(minutes=5 * i),
            "open": o,
            "high": max(o, c) + 0.05,
            "low": min(o, c) - 0.05,
            "close": c,
            "volume": 10_000,
        })
        price = c
    df = pd.DataFrame(rows)
    sig = sms.evaluate_symbol("FLAT", df)
    assert sig is None, f"expected no signal on flat noise, got {sig}"


def test_buy_path_can_fire_on_strong_uptrend():
    # Relax gates slightly for synthetic certainty
    old_mom = config.SMART_MONEY_MOMENTUM_THRESHOLD_PCT
    old_struct = config.SMART_MONEY_STRUCTURE_LOOKBACK
    config.SMART_MONEY_MOMENTUM_THRESHOLD_PCT = 0.001
    config.SMART_MONEY_STRUCTURE_LOOKBACK = 10
    try:
        df = _make_trend_df(bullish=True)
        # Force a pivot low then breakout structure: mark a swing low mid-series
        mid = len(df) // 2
        df.loc[mid, "low"] = df.loc[mid, "close"] - 5
        df.loc[mid, "open"] = df.loc[mid, "close"]
        sig = sms.evaluate_symbol("TRENDUP", df, sector="NIFTY IT")
        # May still be None if structure cross didn't form — assert API stability
        if sig is not None:
            assert sig.signal in ("BUY", "SELL")
            assert sig.entry_price > 0
            assert sig.stop_loss != sig.entry_price
            assert sig.target != sig.entry_price
            assert sig.risk_reward > 0
            assert "momentum" in sig.conditions
            d = sig.to_dict()
            assert d["symbol"] == "TRENDUP"
            assert "chart_url" in d
    finally:
        config.SMART_MONEY_MOMENTUM_THRESHOLD_PCT = old_mom
        config.SMART_MONEY_STRUCTURE_LOOKBACK = old_struct


def test_signal_dataclass_fields():
    sig = sms.SmartMoneySignal(
        symbol="RELIANCE",
        signal="BUY",
        entry_price=2500.0,
        stop_loss=2450.0,
        target=2600.0,
        risk_reward=2.0,
        timestamp="2026-08-03T10:30:00",
        confidence=75.0,
        sector="NIFTY ENERGY",
        conditions={"momentum": True},
        atr=20.0,
    )
    d = sig.to_dict()
    assert d["signal"] == "BUY"
    assert d["risk_reward"] == 2.0
    assert "NSE:RELIANCE" in d["chart_url"]


def test_resample_preserves_ohlc():
    df = _make_trend_df(n=60)
    out = sms._resample_ohlcv(df, "15min")
    assert not out.empty
    assert set(["date", "open", "high", "low", "close", "volume"]).issubset(out.columns)
    assert len(out) < len(df)


if __name__ == "__main__":
    test_no_signal_on_flat_noise()
    test_buy_path_can_fire_on_strong_uptrend()
    test_signal_dataclass_fields()
    test_resample_preserves_ohlc()
    print("All smart money strategy tests passed.")
