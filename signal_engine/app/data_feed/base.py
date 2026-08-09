"""
Abstract data feed. Swap SimulatorFeed for KiteFeed in production without
touching any signal-engine code - this is the same abstraction pattern
you're already using for file-vs-Telegram signal sources.
"""

from abc import ABC, abstractmethod
import pandas as pd


class DataFeed(ABC):
    @abstractmethod
    def get_ohlcv_1m(self, symbol: str, lookback_minutes: int = 120) -> pd.DataFrame:
        """Return 1-min OHLCV with columns: timestamp, open, high, low, close, volume"""
        ...

    @abstractmethod
    def get_spot_price(self, symbol: str) -> float:
        ...

    @abstractmethod
    def get_vix(self) -> float:
        ...

    @abstractmethod
    def is_expiry_day(self, symbol: str) -> bool:
        ...
