"""
Run after generate_session.py to find the instrument tokens for NIFTY and
SENSEX indices, so you can fill INSTRUMENT_TOKENS in app/data_feed/kite_feed.py.

Usage:
    python scripts/fetch_instrument_tokens.py
"""

import json
import os
import sys

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("Missing dependency. Run: pip install kiteconnect --break-system-packages")
    sys.exit(1)

SESSION_FILE = os.path.join(os.path.dirname(__file__), "..", "app", "kite_session.json")


def main():
    if not os.path.exists(SESSION_FILE):
        print("No session found. Run scripts/generate_session.py first.")
        sys.exit(1)

    with open(SESSION_FILE) as f:
        session = json.load(f)

    kite = KiteConnect(api_key=session["api_key"])
    kite.set_access_token(session["access_token"])

    print("Fetching NSE instruments (this can take a few seconds)...")
    nse = kite.instruments("NSE")
    for row in nse:
        if row["tradingsymbol"] == "NIFTY 50":
            print(f"NIFTY 50  -> instrument_token: {row['instrument_token']}")

    print("Fetching BSE instruments...")
    bse = kite.instruments("BSE")
    for row in bse:
        if row["tradingsymbol"] == "SENSEX":
            print(f"SENSEX    -> instrument_token: {row['instrument_token']}")

    print("\nCopy these into INSTRUMENT_TOKENS in app/data_feed/kite_feed.py")


if __name__ == "__main__":
    main()
