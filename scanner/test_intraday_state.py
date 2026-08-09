"""
Validates intraday_state.SymbolState with simulated tick streams — no Kite
connection needed. Run this any time after touching intraday_state.py.

Covers:
  1. Opening range correctly locks in high/low/avg-volume from the first N minutes
  2. No trigger fires before the opening range is locked
  3. No trigger fires on breakout alone (needs volume surge + VWAP too)
  4. Trigger fires when all three conditions hold together
  5. Trigger fires at most once per symbol per day
"""
from datetime import datetime, timedelta

import config
from intraday_state import SymbolState

MARKET_OPEN = datetime(2026, 8, 3, 9, 15, 0)  # a Monday


def prime(state, market_open):
    """The very first tick a fresh SymbolState ever sees always contributes
    zero volume delta by design (correct in production — avoids counting
    pre-existing day volume as happening in that instant). Prime it here so
    subsequent test ticks aren't silently missing their first minute's worth."""
    state.process_tick({"last_price": 100, "volume_traded": 0}, market_open - timedelta(seconds=1))


def feed_minute(state, minute_start, price, total_volume, num_ticks=5):
    """Simulates `num_ticks` ticks spread across one minute at a roughly
    constant price, with `total_volume` traded during that minute."""
    per_tick_vol = total_volume / num_ticks
    cum_vol = state._last_cum_volume or 0
    for i in range(num_ticks):
        cum_vol += per_tick_vol
        t = minute_start + timedelta(seconds=int(i * 60 / num_ticks))
        tick = {"last_price": price, "volume_traded": cum_vol}
        state.process_tick(tick, t)


def test_opening_range_lock():
    print("1) Opening range locks correctly...")
    s = SymbolState("TEST", 1)
    prime(s, MARKET_OPEN)
    for i in range(15):
        minute = MARKET_OPEN + timedelta(minutes=i)
        price = 100 + (i % 3)
        feed_minute(s, minute, price, total_volume=1000)

    locked = s.lock_opening_range(MARKET_OPEN)
    assert locked, "should lock successfully with 15 minutes of data"
    assert s.opening_range_high == 102, f"expected OR high 102, got {s.opening_range_high}"
    assert s.opening_range_low == 100, f"expected OR low 100, got {s.opening_range_low}"
    assert abs(s.opening_range_avg_minute_volume - 1000) < 1, \
        f"expected avg vol ~1000, got {s.opening_range_avg_minute_volume}"
    print("   PASS")
    return s


def test_no_trigger_before_lock():
    print("2) No trigger before opening range is locked...")
    s = SymbolState("TEST2", 2)
    prime(s, MARKET_OPEN)
    feed_minute(s, MARKET_OPEN, 100, total_volume=5000)
    trigger = s.check_trigger()
    assert trigger is None, "should not trigger before opening_range_locked"
    print("   PASS")


def _locked_state(symbol="TESTX", token=99):
    s = SymbolState(symbol, token)
    prime(s, MARKET_OPEN)
    for i in range(15):
        minute = MARKET_OPEN + timedelta(minutes=i)
        price = 100 + (i % 3)
        feed_minute(s, minute, price, total_volume=1000)
    s.lock_opening_range(MARKET_OPEN)
    return s


def test_no_trigger_breakout_alone():
    print("3) Breakout alone (no volume surge) does not trigger...")
    s = _locked_state()
    minute16 = MARKET_OPEN + timedelta(minutes=15)
    feed_minute(s, minute16, 103, total_volume=1000)  # normal volume, no surge
    trigger = s.check_trigger()
    assert trigger is None, "should not trigger on breakout without volume surge"
    print("   PASS")


def test_full_trigger_fires():
    print("4) Breakout + volume surge + price above VWAP -> fires...")
    s = _locked_state()
    minute16 = MARKET_OPEN + timedelta(minutes=15)
    feed_minute(s, minute16, 105, total_volume=3000)  # 3x the 1000 baseline -> surge
    trigger = s.check_trigger()
    assert trigger is not None, "should trigger: breakout + surge + above VWAP"
    assert trigger["symbol"] == "TESTX"
    assert trigger["price"] == 105
    print(f"   PASS — trigger: {trigger}")
    return s


def test_fires_only_once():
    print("5) Trigger fires at most once per symbol per day...")
    s = test_full_trigger_fires()
    minute17 = MARKET_OPEN + timedelta(minutes=16)
    feed_minute(s, minute17, 108, total_volume=4000)
    trigger2 = s.check_trigger()
    assert trigger2 is None, "should not fire twice for the same symbol/day"
    print("   PASS")


if __name__ == "__main__":
    test_opening_range_lock()
    test_no_trigger_before_lock()
    test_no_trigger_breakout_alone()
    test_full_trigger_fires()
    test_fires_only_once()
    print("\nAll intraday state machine tests passed.")
