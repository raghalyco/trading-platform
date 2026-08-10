"""
One Kite Connect login shared by scanner/, signal_engine/, and execution/.

Manual login only: prints the login URL, you log in yourself in a normal
browser (entering your TOTP from your authenticator app as usual), then
paste back the request_token from the redirect URL. This avoids automating
a real login (Zerodha's bot detection can block that) and means your
password/TOTP secret never need to be stored anywhere.

Access tokens expire daily (around midnight IST), so this caches the token
with the date it was issued to shared/../cache/access_token.json and only
prompts you to log in again once it's stale. All three subsystems read
this same cache file, so you only log in once per day no matter which app
you start first.
"""
import json
import os
from datetime import date
from urllib.parse import urlparse, parse_qs

from kiteconnect import KiteConnect

from shared import config


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


def get_manual_login_url() -> str:
    if not config.KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY must be set in trading-platform/.env before logging in.")
    return KiteConnect(api_key=config.KITE_API_KEY).login_url()


def _login_manually(kite: KiteConnect) -> str:
    if not (config.KITE_API_KEY and config.KITE_API_SECRET):
        raise RuntimeError(
            "KITE_API_KEY and KITE_API_SECRET must be set in trading-platform/.env before logging in."
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


def _cache_token(access_token: str) -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.ACCESS_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": str(date.today()), "access_token": access_token}, f)


def exchange_request_token(request_token: str) -> str:
    """Exchanges a pasted request_token/redirect URL for an access_token,
    caches it, and returns it."""
    if not (config.KITE_API_KEY and config.KITE_API_SECRET):
        raise RuntimeError(
            "KITE_API_KEY and KITE_API_SECRET must be set in trading-platform/.env before logging in."
        )

    request_token = _extract_request_token(request_token)
    if len(request_token) < 10:
        raise ValueError("Request token looks too short.")

    kite = KiteConnect(api_key=config.KITE_API_KEY)
    try:
        session = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    except Exception as e:
        raise RuntimeError(
            f"generate_session failed ({e}). Common causes: the request_token "
            "was already used (they're single-use — re-do the login flow to get "
            "a fresh one), it expired (they're valid for only a couple of "
            "minutes), or KITE_API_SECRET doesn't match KITE_API_KEY."
        ) from e

    access_token = session["access_token"]
    _cache_token(access_token)
    return access_token


def prompt_and_store_access_token(request_token: str | None = None) -> None:
    if request_token is None:
        print("Open this URL in a browser, complete the Zerodha login, then paste the request_token or redirect URL here:")
        print(get_manual_login_url())
        request_token = input("Request token / redirect URL: ").strip()

    exchange_request_token(request_token)
    print("Access token updated successfully.")


def get_kite_session() -> KiteConnect:
    """Returns an authenticated KiteConnect instance, reusing today's cached
    access_token if one exists, otherwise walking you through the manual
    login flow once."""
    if not config.KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY must be set in trading-platform/.env before logging in.")

    kite = KiteConnect(api_key=config.KITE_API_KEY)

    cached = None
    if config.ACCESS_TOKEN_FILE.exists():
        with open(config.ACCESS_TOKEN_FILE, encoding="utf-8") as f:
            cached = json.load(f)

    if cached and cached.get("date") == str(date.today()) and cached.get("access_token"):
        kite.set_access_token(cached["access_token"])
        try:
            kite.profile()  # cheap check — catches revoked / mismatched tokens
            return kite
        except Exception as e:
            print(f"Cached access token is invalid ({e}). Re-login required.")
            try:
                os.remove(config.ACCESS_TOKEN_FILE)
            except OSError:
                pass

    request_token = _login_manually(kite)
    access_token = exchange_request_token(request_token)
    kite.set_access_token(access_token)
    print("Logged in and cached today's access token.")
    return kite


def get_kite_client() -> KiteConnect:
    """Same as get_kite_session() but validates the cached token against
    the API (raises with a clear message if it's missing/expired) instead
    of silently walking through login — used by callers that want a hard
    failure rather than an interactive prompt (e.g. a headless bot)."""
    if not config.KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY must be set in trading-platform/.env.")

    kite = KiteConnect(api_key=config.KITE_API_KEY)

    if not config.ACCESS_TOKEN_FILE.exists():
        raise RuntimeError(
            "No cached Zerodha access token found. Run `python -m shared.kite_auth` "
            "from the trading-platform root, complete the browser login, and paste "
            f"the request_token or redirect URL. Login URL: {get_manual_login_url()}"
        )

    with open(config.ACCESS_TOKEN_FILE, encoding="utf-8") as f:
        cached = json.load(f)

    if cached.get("date") != str(date.today()) or not cached.get("access_token"):
        raise RuntimeError(
            "Cached Zerodha access token is stale (not from today). Run "
            "`python -m shared.kite_auth` from the trading-platform root, complete "
            "the browser login, and paste the request_token or redirect URL. "
            f"Login URL: {get_manual_login_url()}"
        )

    kite.set_access_token(cached["access_token"])
    try:
        kite.profile()
    except Exception as exc:
        raise RuntimeError(
            "Cached Zerodha access token was rejected by Kite (likely expired "
            "early or revoked). Run `python -m shared.kite_auth` from the "
            "trading-platform root and log in again. "
            f"Login URL: {get_manual_login_url()}"
        ) from exc

    return kite


if __name__ == "__main__":
    k = get_kite_session()
    print("Profile:", k.profile()["user_name"])
