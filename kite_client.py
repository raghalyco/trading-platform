"""
Thin wrapper around KiteConnect for:
  - pulling the NSE equity instrument dump
  - fetching daily historical candles with disk caching + rate limiting
  - bulk live quotes (LTP / %change) for the daily scanner's table

Caching matters a lot here: scanning the full NSE universe means ~2000+
symbols x years of daily candles. Kite's historical API is rate-limited
(~3 req/sec) and each request is capped at ~2000 daily candles, so a full
run without caching takes hours and re-running it for a parameter tweak
would be painful. Every symbol's data is cached to a parquet file and only
the missing tail is re-fetched on subsequent runs.
"""
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd

import config


class RateLimiter:
    def __init__(self, requests_per_second: float):
        self.min_interval = 1.0 / requests_per_second
        self._last = 0.0

    def wait(self):
        elapsed = time.time() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.time()


class KiteDataClient:
    def __init__(self, kite):
        self.kite = kite
        self.limiter = RateLimiter(config.REQUESTS_PER_SECOND)
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        self._instruments_df = None

    # ---------------------------------------------------------------- #
    # Instruments / universe
    # ---------------------------------------------------------------- #
    def get_nse_equity_instruments(self) -> pd.DataFrame:
        """All tradeable NSE cash-segment equities (excludes ETFs/indices)."""
        cache_file = os.path.join(config.CACHE_DIR, "instruments_nse.parquet")
        if os.path.exists(cache_file):
            age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
            if age_hours < 24:
                return pd.read_parquet(cache_file)

        self.limiter.wait()
        instruments = self.kite.instruments("NSE")
        df = pd.DataFrame(instruments)
        df = df[(df["segment"] == "NSE") & (df["instrument_type"] == "EQ")]
        df = df[~df["name"].str.contains("ETF", case=False, na=False)]
        df = df[["instrument_token", "tradingsymbol", "name"]].reset_index(drop=True)
        df.to_parquet(cache_file)
        return df

    def get_nse_index_instruments(self) -> pd.DataFrame:
        """All NSE index instruments (NIFTY 50, NIFTY BANK, NIFTY IT, etc.) —
        these are directly tradeable-for-data-purposes instruments in Kite's
        dump, segment 'INDICES'."""
        cache_file = os.path.join(config.CACHE_DIR, "instruments_nse_indices.parquet")
        if os.path.exists(cache_file):
            age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
            if age_hours < 24:
                return pd.read_parquet(cache_file)

        self.limiter.wait()
        instruments = self.kite.instruments("NSE")
        df = pd.DataFrame(instruments)
        df = df[df["segment"] == "INDICES"]
        df = df[["instrument_token", "tradingsymbol", "name"]].reset_index(drop=True)
        df.to_parquet(cache_file)
        return df

    # ---------------------------------------------------------------- #
    # Historical candles
    # ---------------------------------------------------------------- #
    def get_daily_history(self, instrument_token, tradingsymbol, from_date, to_date) -> pd.DataFrame:
        cache_file = os.path.join(config.CACHE_DIR, f"daily_{tradingsymbol}.parquet")

        cached_df = self._read_parquet_safe(cache_file, tradingsymbol)
        if cached_df is not None:
            have_from = cached_df["date"].min().date()
            have_to = cached_df["date"].max().date()
            if have_from <= from_date and have_to >= to_date:
                mask = (cached_df["date"].dt.date >= from_date) & (cached_df["date"].dt.date <= to_date)
                return cached_df.loc[mask].reset_index(drop=True)

        # Fetch only what's actually missing, then merge with the cache —
        # this is what makes a daily scan cheap: after the first full run,
        # each subsequent day only needs a 1-2 day fetch per symbol instead
        # of re-pulling years of history.
        fetch_ranges = []
        if cached_df is not None and cached_df["date"].min().date() <= from_date:
            have_to = cached_df["date"].max().date()
            if to_date > have_to:
                fetch_ranges.append((have_to + timedelta(days=1), to_date))
        else:
            fetch_ranges.append((from_date, to_date))

        new_chunks = []
        for range_from, range_to in fetch_ranges:
            # Kite historical API caps daily requests at ~2000 candles per call;
            # chunk by ~5 years to stay safely under that.
            cur = range_from
            while cur <= range_to:
                chunk_end = min(cur + timedelta(days=365 * 5), range_to)
                self.limiter.wait()
                try:
                    data = self.kite.historical_data(
                        instrument_token, cur, chunk_end, "day", oi=False
                    )
                except Exception as e:
                    print(f"  [warn] historical_data failed for {tradingsymbol} "
                          f"({cur}..{chunk_end}): {e}")
                    data = []
                new_chunks.extend(data)
                cur = chunk_end + timedelta(days=1)

        if new_chunks:
            new_df = pd.DataFrame(new_chunks)
            new_df["date"] = pd.to_datetime(new_df["date"]).dt.tz_localize(None)
            df = pd.concat([cached_df, new_df]) if cached_df is not None else new_df
        elif cached_df is not None:
            df = cached_df
        else:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
        self._write_parquet_atomic(df, cache_file)

        mask = (df["date"].dt.date >= from_date) & (df["date"].dt.date <= to_date)
        return df.loc[mask].reset_index(drop=True)

    def _read_parquet_safe(self, cache_file: str, label: str):
        """Read a parquet cache; on corruption delete it and return None."""
        if not os.path.exists(cache_file):
            return None
        try:
            df = pd.read_parquet(cache_file)
            df["date"] = pd.to_datetime(df["date"])
            return df
        except Exception as e:
            print(f"  [warn] corrupt cache for {label} ({e}) — re-fetching")
            try:
                os.remove(cache_file)
            except OSError:
                pass
            return None

    def _write_parquet_atomic(self, df: pd.DataFrame, cache_file: str):
        """Write via a temp file then replace — avoids half-written parquet
        when two requests hit the same symbol concurrently."""
        tmp_file = cache_file + f".tmp.{os.getpid()}"
        try:
            df.to_parquet(tmp_file)
            os.replace(tmp_file, cache_file)
        except Exception as e:
            print(f"  [warn] failed writing cache {cache_file}: {e}")
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except OSError:
                pass

    def get_history(self, instrument_token, tradingsymbol, interval: str,
                    from_dt: datetime, to_dt: datetime) -> pd.DataFrame:
        """Fetch OHLC for a Kite interval (e.g. '5minute', '15minute', '60minute',
        'day'). Minute data is cached per symbol+interval for the day so repeated
        Smart Money scans don't re-hit the API for every loop."""
        if interval == "day":
            return self.get_daily_history(
                instrument_token, tradingsymbol, from_dt.date(), to_dt.date()
            )

        safe_interval = interval.replace(" ", "_")
        cache_file = os.path.join(
            config.CACHE_DIR, f"{safe_interval}_{tradingsymbol}.parquet"
        )

        if os.path.exists(cache_file):
            age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
            # Intraday cache is short-lived — reuse within ~10 minutes.
            if age_hours < (10 / 60):
                cached_df = self._read_parquet_safe(cache_file, f"{interval}:{tradingsymbol}")
                if cached_df is not None:
                    mask = (cached_df["date"] >= from_dt) & (cached_df["date"] <= to_dt)
                    hit = cached_df.loc[mask].reset_index(drop=True)
                    if not hit.empty:
                        return hit

        self.limiter.wait()
        try:
            data = self.kite.historical_data(
                instrument_token, from_dt, to_dt, interval, oi=False
            )
        except Exception as e:
            print(f"  [warn] historical_data({interval}) failed for {tradingsymbol}: {e}")
            data = []

        if not data:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
        self._write_parquet_atomic(df, cache_file)
        mask = (df["date"] >= from_dt) & (df["date"] <= to_dt)
        return df.loc[mask].reset_index(drop=True)

    # ---------------------------------------------------------------- #
    # Live quotes (for the daily scanner's LTP / %change columns)
    # ---------------------------------------------------------------- #
    def get_quotes(self, tradingsymbols: list) -> dict:
        """Returns {tradingsymbol: {last_price, prev_close, pct_change}}.
        Kite's quote endpoint accepts at most ~500 instruments per call, so
        this batches automatically. Only call this for the (small) list of
        symbols that already passed the scanner filters — not the whole
        universe, that would be wasteful."""
        out = {}
        batch_size = 200
        for i in range(0, len(tradingsymbols), batch_size):
            batch = tradingsymbols[i:i + batch_size]
            keys = [f"NSE:{s}" for s in batch]
            self.limiter.wait()
            try:
                quotes = self.kite.quote(keys)
            except Exception as e:
                print(f"  [warn] quote() failed for batch starting {batch[0]}: {e}")
                continue
            for key, q in quotes.items():
                sym = key.split(":", 1)[1]
                last_price = q.get("last_price")
                prev_close = q.get("ohlc", {}).get("close")
                pct_change = (
                    (last_price - prev_close) / prev_close * 100
                    if last_price is not None and prev_close else None
                )
                out[sym] = {
                    "last_price": last_price,
                    "prev_close": prev_close,
                    "pct_change": pct_change,
                }
        return out
