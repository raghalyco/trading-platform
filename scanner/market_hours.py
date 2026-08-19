"""
Single source of truth for "is NSE market open right now" - used to gate
BOTH data refreshes (app.py's _should_refresh) and every Telegram alert
send site (swing_trade.py, smart_money_pipeline.py, trending_alerts.py),
so alerts can never fire outside market hours even from an edge-case code
path (e.g. the one-time cache bootstrap fetch when the app happens to
start before market open with an empty cache).

A standalone module (not part of app.py) so the scanner submodules can
import it without a circular import back into app.py.
"""
from datetime import datetime, time as dtime


def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)
