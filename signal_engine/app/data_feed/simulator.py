"""
Synthetic 1-min OHLCV generator so the whole engine + dashboard runs and can
be demoed/tested without a live Kite session. Swap for KiteFeed later.
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from app.data_feed.base import DataFeed


class SimulatorFeed(DataFeed):
    _BASE_PRICES = {
        "NIFTY": 24100.0,
        "SENSEX": 79800.0,
    }
    _DEFAULT_BASE_PRICE = 24100.0

    def __init__(self, seed: int = 42, base_price: float | None = None):
        self._rng = np.random.default_rng(seed)
        self._base_price_override = base_price
        self._cache: dict[str, pd.DataFrame] = {}

    def _base_price_for(self, symbol: str) -> float:
        if self._base_price_override is not None:
            return self._base_price_override
        return self._BASE_PRICES.get(symbol, self._DEFAULT_BASE_PRICE)

    def _generate(self, symbol: str, minutes: int) -> pd.DataFrame:
        rng = self._rng
        n = minutes
        base_price = self._base_price_for(symbol)
        drift = rng.normal(0, 1, n).cumsum() * (base_price / 12050.0)
        close = base_price + drift
        high = close + rng.uniform(1, 8, n)
        low = close - rng.uniform(1, 8, n)
        open_ = close + rng.normal(0, 3, n)
        volume = rng.integers(500, 5000, n)

        now = datetime.now()
        timestamps = [now - timedelta(minutes=(n - i)) for i in range(n)]

        df = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": open_,
                "high": np.maximum.reduce([high, open_, close]),
                "low": np.minimum.reduce([low, open_, close]),
                "close": close,
                "volume": volume,
            }
        )
        return df

    def get_ohlcv_1m(self, symbol: str, lookback_minutes: int = 120) -> pd.DataFrame:
        if symbol not in self._cache:
            self._cache[symbol] = self._generate(symbol, max(lookback_minutes, 60))
        # regenerate if caller asks for more history than cached
        if len(self._cache[symbol]) < lookback_minutes:
            self._cache[symbol] = self._generate(symbol, lookback_minutes)
        return self._cache[symbol].tail(lookback_minutes).reset_index(drop=True)

    def get_ohlcv_history(self, symbol: str, days: int = 90,
                          interval: str = "5minute",
                          end_date: datetime | None = None) -> pd.DataFrame:
        """Synthetic multi-day series for backtest UI when Kite is unavailable.

        end_date: override the window's end (defaults to now) - mirrors
        KiteFeed.get_ohlcv_history so the explicit from/to date picker works
        identically against the simulator."""
        bar_minutes = 5 if "5" in interval else (15 if "15" in interval else 1)
        bars_per_day = max(1, 75 // bar_minutes)
        n = max(bars_per_day * days, 200)
        now = end_date or datetime.now()
        key = f"{symbol}_{interval}_{days}_{now.strftime('%Y-%m-%d')}"
        if key not in self._cache:
            df = self._generate(symbol, n).copy()
            # Spread bars across the full calendar window so from/to ≈ `days`
            span_minutes = days * 24 * 60
            step = max(bar_minutes, span_minutes // n)
            df["timestamp"] = [
                now - timedelta(minutes=step * (n - i)) for i in range(n)
            ]
            self._cache[key] = df
        return self._cache[key].reset_index(drop=True)

    def get_spot_price(self, symbol: str) -> float:
        return float(self.get_ohlcv_1m(symbol, 5)["close"].iloc[-1])

    def get_vix(self) -> float:
        return round(float(self._rng.uniform(11, 18)), 2)

    def is_expiry_day(self, symbol: str) -> bool:
        return datetime.now().weekday() == 1  # Tuesday, matches your NIFTY screenshot

    def get_option_ohlcv_1m(
        self,
        underlying: str,
        expiry_date: str,
        strike: int,
        side: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> pd.DataFrame:
        """Synthetic option premium path between from_dt and to_dt."""
        minutes = max(5, int((to_dt - from_dt).total_seconds() / 60) + 5)
        base = 100.0 + (strike % 50)
        rng = self._rng
        n = minutes
        drift = rng.normal(0.05 if side == "CE" else -0.02, 0.4, n).cumsum()
        close = np.maximum(1.0, base + drift)
        high = close + rng.uniform(0.2, 1.5, n)
        low = np.maximum(0.5, close - rng.uniform(0.2, 1.5, n))
        open_ = close + rng.normal(0, 0.4, n)
        volume = rng.integers(50, 800, n)
        timestamps = [from_dt + timedelta(minutes=i) for i in range(n)]
        return pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": open_,
                "high": np.maximum.reduce([high, open_, close]),
                "low": np.minimum.reduce([low, open_, close]),
                "close": close,
                "volume": volume,
            }
        )
