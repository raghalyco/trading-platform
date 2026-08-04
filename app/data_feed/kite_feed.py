"""
Production data feed backed by Zerodha Kite Connect.
Fill in your TOTP/OAuth headless-login flow where marked - this class only
needs to satisfy the DataFeed interface, everything upstream (indicators,
scorer, modes) is already broker-agnostic.

pip install kiteconnect --break-system-packages
"""

import json
import os
from datetime import datetime, timedelta
import pandas as pd
from app.data_feed.base import DataFeed

try:
    from kiteconnect import KiteConnect
except ImportError:
    KiteConnect = None  # allows import on machines without the package yet


SESSION_FILE = os.path.join(os.path.dirname(__file__), "..", "kite_session.json")

INSTRUMENT_TOKENS = {
    # Fill with actual instrument tokens - run scripts/fetch_instrument_tokens.py
    "NIFTY": 256265,
    "SENSEX": 265,
}


class KiteFeed(DataFeed):
    def __init__(self, api_key: str, access_token: str):
        if KiteConnect is None:
            raise RuntimeError("kiteconnect not installed - pip install kiteconnect")
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)
        self._nfo_instruments = None  # cached lazily on first get_option_ltp() call

    @classmethod
    def from_session_file(cls, path: str = SESSION_FILE) -> "KiteFeed":
        """
        Loads {"api_key": ..., "access_token": ...} written by
        scripts/generate_session.py. Raises FileNotFoundError if no session
        exists yet (e.g. you haven't run the morning login script today).
        """
        with open(path) as f:
            session = json.load(f)
        return cls(api_key=session["api_key"], access_token=session["access_token"])

    @classmethod
    def login_headless(cls, api_key: str, api_secret: str, user_id: str,
                        password: str, totp_secret: str):
        """
        Placeholder for a FUTURE fully-automated TOTP/OAuth flow. Not used
        yet - you're currently doing the manual login via
        scripts/generate_session.py instead. When you're ready to automate:
        1. Launch Selenium/Playwright against kite.zerodha.com/connect/login
        2. Fill user_id/password, generate TOTP with pyotp.TOTP(totp_secret).now()
        3. Capture request_token from the redirect URL
        4. kite.generate_session(request_token, api_secret=api_secret) -> access_token
        Return a KiteFeed instance once you have the access_token.
        """
        raise NotImplementedError("Not wired up yet - using manual session file for now")

    def get_ohlcv_1m(self, symbol: str, lookback_minutes: int = 120) -> pd.DataFrame:
        token = INSTRUMENT_TOKENS.get(symbol)
        if token is None:
            raise ValueError(f"Set instrument token for {symbol} in INSTRUMENT_TOKENS")

        to_dt = datetime.now()
        from_dt = to_dt - timedelta(minutes=lookback_minutes + 5)
        candles = self.kite.historical_data(token, from_dt, to_dt, "minute")

        if not candles:
            raise RuntimeError(
                f"Kite returned 0 candles for {symbol} (token {token}), "
                f"from {from_dt} to {to_dt}. This is NOT a code bug - Kite "
                f"genuinely sent back no data. Most likely causes: "
                f"(1) market is closed right now (NSE hours: 9:15-15:30 IST, Mon-Fri), "
                f"(2) instrument token is wrong/stale - re-run scripts/fetch_instrument_tokens.py, "
                f"(3) access_token has expired - re-run scripts/generate_session.py."
            )

        df = pd.DataFrame(candles).rename(columns={"date": "timestamp"})

        # Use the timezone Kite's own data already carries, rather than
        # assuming server-local time - avoids IST/UTC mismatches on
        # servers (e.g. EC2) not configured for IST.
        tz = df["timestamp"].dt.tz
        if tz is not None:
            cutoff = pd.Timestamp.now(tz=tz) - pd.Timedelta(minutes=lookback_minutes)
            df = df[df["timestamp"] >= cutoff]

        return df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

    def get_spot_price(self, symbol: str) -> float:
        token = INSTRUMENT_TOKENS.get(symbol)
        quote = self.kite.quote([token])
        return float(quote[str(token)]["last_price"])

    def get_vix(self) -> float:
        quote = self.kite.quote(["NSE:INDIA VIX"])
        return float(quote["NSE:INDIA VIX"]["last_price"])

    def is_expiry_day(self, symbol: str) -> bool:
        # NIFTY weekly expiry = Tuesday, SENSEX weekly expiry = Tuesday (as of 2026 cycle)
        # Recommend pulling actual expiry dates from kite.instruments() instead of
        # hardcoding weekdays, since exchanges have changed expiry days before.
        return datetime.now().weekday() == 1

    def get_option_ltp(self, underlying: str, expiry_date: str, strike: int, side: str) -> dict:
        """
        Fetches the live last-traded-price of ONE specific option contract.
        expiry_date: 'YYYY-MM-DD' string matching Kite's instrument expiry field.
        side: 'CE' or 'PE'.

        Uses kite.instruments("NFO") to find the exact instrument_token
        (matched on underlying name + expiry + strike + type), then
        kite.quote() for the live price. instruments("NFO") is a large,
        slow call (~thousands of rows) so it's cached per-process rather
        than re-fetched every call - restart the server if today's
        contracts aren't showing up (e.g. right after a new expiry began).
        """
        if self._nfo_instruments is None:
            self._nfo_instruments = self.kite.instruments("NFO")

        matches = [
            i for i in self._nfo_instruments
            if i["name"] == underlying
            and str(i["expiry"]) == expiry_date
            and int(i["strike"]) == int(strike)
            and i["instrument_type"] == side
        ]
        if not matches:
            raise ValueError(
                f"No matching option contract found for {underlying} {expiry_date} "
                f"{strike}{side}. Check the expiry date format matches Kite's "
                f"instrument data (YYYY-MM-DD) and that strike/expiry are currently listed."
            )

        instrument = matches[0]
        token = instrument["instrument_token"]
        tradingsymbol = instrument["tradingsymbol"]
        quote = self.kite.quote([token])
        ltp = quote[str(token)]["last_price"]

        return {
            "tradingsymbol": tradingsymbol,
            "instrument_token": token,
            "ltp": float(ltp),
        }
