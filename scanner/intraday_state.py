"""
Per-symbol state machine that consumes raw Kite WebSocket ticks and
implements: opening-range breakout + volume surge + VWAP reclaim.

Kept deliberately separate from the WebSocket plumbing (intraday_monitor.py)
so this logic can be unit-tested with synthetic ticks — the actual live
Kite tick schema (field names for cumulative volume / average price) can
vary slightly by API version, and this sandbox has no way to verify that
against a real live feed. See the NOTE in process_tick().

Kite tick fields this expects (from the documented KiteTicker "full" mode
payload — VERIFY against your own first live ticks, see intraday_monitor.py's
debug logging):
  - last_price
  - volume_traded (cumulative volume for the day, NOT a per-tick delta)
  - average_traded_price (Kite's own running day VWAP, if present — used
    directly when available; falls back to manual VWAP computation if not)
"""
from datetime import datetime, timedelta

import config


class SymbolState:
    def __init__(self, symbol: str, instrument_token: int, sources: list = None):
        self.symbol = symbol
        self.instrument_token = instrument_token
        self.sources = sources or []

        self.minute_candles = {}      # {minute_start: {open, high, low, close, volume}}
        self.current_minute = None
        self.current_candle = None

        self._last_cum_volume = None  # for computing per-tick volume deltas
        self._cum_price_volume = 0.0  # manual VWAP fallback accumulators
        self._cum_volume_for_vwap = 0.0
        self.vwap = None

        self.opening_range_high = None
        self.opening_range_low = None
        self.opening_range_avg_minute_volume = None
        self.opening_range_locked = False

        self.alerted_today = False

    def process_tick(self, tick: dict, now: datetime):
        price = tick.get("last_price")
        if price is None:
            return

        # NOTE: 'volume_traded' is Kite's CUMULATIVE volume for the day as of
        # this tick, not a per-tick trade size — we diff it ourselves to get
        # a per-tick delta. If your live payload uses a different field name
        # (check intraday_monitor.py's debug print of the first raw tick),
        # update this line.
        cum_volume = tick.get("volume_traded", tick.get("volume"))
        if cum_volume is not None:
            if self._last_cum_volume is None:
                self._last_cum_volume = cum_volume
            delta_vol = max(0, cum_volume - self._last_cum_volume)
            self._last_cum_volume = cum_volume
        else:
            delta_vol = 0

        # Prefer Kite's own running VWAP field if the payload has it;
        # otherwise accumulate manually from price * volume deltas.
        kite_vwap = tick.get("average_traded_price", tick.get("average_price"))
        if kite_vwap:
            self.vwap = kite_vwap
        else:
            self._cum_price_volume += price * delta_vol
            self._cum_volume_for_vwap += delta_vol
            self.vwap = (
                self._cum_price_volume / self._cum_volume_for_vwap
                if self._cum_volume_for_vwap > 0 else price
            )

        minute = now.replace(second=0, microsecond=0)
        if self.current_minute != minute:
            if self.current_candle is not None:
                self.minute_candles[self.current_minute] = self.current_candle
            self.current_minute = minute
            self.current_candle = {"open": price, "high": price, "low": price, "close": price, "volume": 0.0}

        c = self.current_candle
        c["high"] = max(c["high"], price)
        c["low"] = min(c["low"], price)
        c["close"] = price
        c["volume"] += delta_vol

    def lock_opening_range(self, market_open_dt: datetime) -> bool:
        """Call once, after INTRADAY_OPENING_RANGE_MINUTES have elapsed.
        Returns False if there's no candle data yet for the range (e.g.
        this symbol hasn't traded)."""
        range_end = market_open_dt + timedelta(minutes=config.INTRADAY_OPENING_RANGE_MINUTES)
        relevant = {m: c for m, c in self.minute_candles.items() if market_open_dt <= m < range_end}

        # The last minute of the opening range is very likely still the
        # "currently forming" candle (not yet finalized into minute_candles)
        # at the moment this is called — without this, the range would
        # silently be missing its final minute of data every single day.
        if self.current_minute is not None and market_open_dt <= self.current_minute < range_end:
            relevant = {**relevant, self.current_minute: self.current_candle}

        if not relevant:
            return False
        self.opening_range_high = max(c["high"] for c in relevant.values())
        self.opening_range_low = min(c["low"] for c in relevant.values())
        self.opening_range_avg_minute_volume = sum(c["volume"] for c in relevant.values()) / len(relevant)
        self.opening_range_locked = True
        return True

    def check_trigger(self) -> dict:
        """Returns a trigger dict if opening-range breakout + volume surge +
        VWAP-above all hold right now, else None. Fires at most once per
        symbol per day (self.alerted_today)."""
        if not self.opening_range_locked or self.alerted_today or self.current_candle is None:
            return None
        if not self.opening_range_avg_minute_volume:
            return None

        price = self.current_candle["close"]
        cur_minute_volume = self.current_candle["volume"]

        breakout = price > self.opening_range_high
        vol_surge = cur_minute_volume > config.INTRADAY_VOLUME_SURGE_MULTIPLIER * self.opening_range_avg_minute_volume
        vwap_ok = self.vwap is not None and price > self.vwap

        if breakout and vol_surge and vwap_ok:
            self.alerted_today = True
            return {
                "symbol": self.symbol,
                "price": price,
                "opening_range_high": self.opening_range_high,
                "vwap": self.vwap,
                "minute_volume": cur_minute_volume,
                "avg_minute_volume": self.opening_range_avg_minute_volume,
                "time": self.current_minute.strftime("%H:%M:%S") if self.current_minute else "",
                "sources": self.sources,
                "chart_url": f"https://www.tradingview.com/chart/?symbol=NSE:{self.symbol}",
            }
        return None
