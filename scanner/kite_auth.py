"""
Thin compatibility shim: scanner/ used to own its Kite auth outright, now
it delegates to shared/kite_auth.py so all three apps in this repo share
one daily login and one cached access token. See shared/kite_auth.py for
the actual implementation.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shared.kite_auth import get_kite_session  # noqa: E402,F401

if __name__ == "__main__":
    k = get_kite_session()
    print("Profile:", k.profile()["user_name"])
