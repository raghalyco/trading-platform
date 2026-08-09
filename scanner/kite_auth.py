"""
Manual Kite Connect login: prints the login URL, you log in yourself in a
normal browser (entering your TOTP from your authenticator app as usual),
then paste back the request_token from the redirect URL. This avoids
automating a real login (which Zerodha's bot detection can block) and means
your password/TOTP secret never need to be stored anywhere.

Access tokens expire daily (around midnight IST), so this caches the token
with the date it was issued and only prompts you to log in again once it's
stale.
"""
import json
import os
from datetime import date
from urllib.parse import urlparse, parse_qs

from kiteconnect import KiteConnect

import config


def _extract_request_token(pasted: str) -> str:
    """Accepts either the raw request_token, or the full redirect URL —
    whichever is easier to copy."""
    pasted = pasted.strip()
    if "request_token=" in pasted:
        parsed = urlparse(pasted)
        token = parse_qs(parsed.query).get("request_token", [None])[0]
        if not token:
            raise ValueError("Couldn't find request_token in the pasted URL.")
        return token
    return pasted


def _login_manually(kite: KiteConnect) -> str:
    if not (config.KITE_API_KEY and config.KITE_API_SECRET):
        raise RuntimeError(
            "KITE_API_KEY and KITE_API_SECRET must be set in .env before logging in."
        )

    login_url = kite.login_url()
    print("\n" + "=" * 70)
    print("Kite login required. Steps:")
    print("  1. Open this URL in your browser:")
    print(f"     {login_url}")
    print("  2. Log in with your Zerodha user ID, password, and TOTP as usual.")
    print("  3. After login you'll be redirected to your app's redirect URL —")
    print("     it will look like: https://your-redirect-url/?request_token=XXXX&...")
    print("     (The page itself may show an error/blank screen — that's fine,")
    print("     you only need the URL from the browser's address bar.)")
    print("=" * 70)

    while True:
        pasted = input("\nPaste the request_token (or the full redirect URL) here: ").strip()
        try:
            return _extract_request_token(pasted)
        except ValueError as e:
            print(f"  {e} Try again.")


def get_kite_session() -> KiteConnect:
    """Returns an authenticated KiteConnect instance, reusing a cached
    access_token for today if one exists, otherwise walking you through the
    manual login flow once."""
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    kite = KiteConnect(api_key=config.KITE_API_KEY)

    cached = None
    if os.path.exists(config.ACCESS_TOKEN_FILE):
        with open(config.ACCESS_TOKEN_FILE) as f:
            cached = json.load(f)

    if cached and cached.get("date") == str(date.today()):
        kite.set_access_token(cached["access_token"])
        return kite

    request_token = _login_manually(kite)
    try:
        session = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    except Exception as e:
        raise RuntimeError(
            f"generate_session failed ({e}). Common causes: the request_token "
            "was already used (they're single-use — re-do the login flow to get "
            "a fresh one), it expired (they're valid for only a couple of "
            "minutes), or KITE_API_SECRET in .env doesn't match KITE_API_KEY."
        )

    access_token = session["access_token"]
    kite.set_access_token(access_token)

    with open(config.ACCESS_TOKEN_FILE, "w") as f:
        json.dump({"date": str(date.today()), "access_token": access_token}, f)

    print("Logged in and cached today's access token.")
    return kite


if __name__ == "__main__":
    k = get_kite_session()
    print("Profile:", k.profile()["user_name"])
