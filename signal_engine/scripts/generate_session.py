"""
Run this once each morning before market open (Kite access_token expires daily).

Usage:
    python scripts/generate_session.py

What it does:
1. Prints the Kite login URL for your api_key.
2. You open it in a browser, log in manually (user_id/password/TOTP).
3. After login, Kite redirects to your registered redirect URL with a
   `request_token` in the query string, e.g.:
       https://your-redirect-url/?request_token=AbCdEf123&action=login&status=success
4. Paste that full redirect URL (or just the token) back into this script.
5. It exchanges request_token -> access_token and saves both to
   app/kite_session.json, which app/api/main.py reads on startup.

This intentionally has ZERO automation of the login step - you are the one
typing your password/TOTP into Kite's real login page, in your own browser.
Nothing here touches your credentials.
"""

import json
import os
import sys
from urllib.parse import urlparse, parse_qs

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("Missing dependency. Run: pip install kiteconnect --break-system-packages")
    sys.exit(1)

SESSION_FILE = os.path.join(os.path.dirname(__file__), "..", "app", "kite_session.json")


def extract_request_token(raw: str) -> str:
    raw = raw.strip()
    if "request_token=" in raw:
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        if "request_token" in qs:
            return qs["request_token"][0]
    return raw  # assume they pasted just the token


def main():
    api_key = input("Kite API key: ").strip()
    api_secret = input("Kite API secret: ").strip()

    kite = KiteConnect(api_key=api_key)
    print("\nOpen this URL in your browser and log in:\n")
    print(kite.login_url())
    print()

    raw = input("Paste the FULL redirect URL (or just the request_token) after login: ")
    request_token = extract_request_token(raw)

    session = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session["access_token"]

    with open(SESSION_FILE, "w") as f:
        json.dump({"api_key": api_key, "access_token": access_token}, f, indent=2)

    print(f"\nSaved session to {SESSION_FILE}")
    print("You can now start the server: uvicorn app.api.main:app --reload --port 8000")


if __name__ == "__main__":
    main()
