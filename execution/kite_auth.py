"""
Thin compatibility shim: execution/ used to own its Kite auth via
config.txt + configparser, now it delegates to shared/kite_auth.py so all
three apps in this repo share one daily login and one cached access token,
driven by trading-platform/.env instead of a committed config.txt. See
shared/kite_auth.py for the actual implementation.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shared.kite_auth import (  # noqa: E402,F401
    exchange_request_token,
    get_kite_client,
    get_manual_login_url,
    prompt_and_store_access_token,
)

if __name__ == "__main__":
    request_token = sys.argv[1] if len(sys.argv) > 1 else None
    prompt_and_store_access_token(request_token)
