"""
Background Telegram alerting for Trending Stocks by Sector.

Requirement: recheck every 15 minutes during market hours, and only send an
UPDATED alert when the set of trending stocks actually changed (a stock
newly qualifies for a sector's top list, or one drops out) - not on every
recheck, so an unchanged list doesn't spam the same message repeatedly.

State (which symbols were trending per sector as of the last alert) is
persisted to disk, same reasoning as alert_dedup.py: an in-memory-only set
would reset on every restart and cause the app to think everything is
"new" again, re-sending an alert for a trending list that hasn't actually
changed since before the restart.
"""
import json
import os

import config
import market_hours
import telegram_alerts

_STATE_FILE = os.path.join(config.CACHE_DIR, "trending_alert_state.json")


def _load_state() -> dict:
    if not os.path.exists(_STATE_FILE):
        return {}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [warn] trending_alerts: corrupt {_STATE_FILE} ({e}) - starting fresh")
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    tmp = _STATE_FILE + f".tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        print(f"  [warn] trending_alerts: failed writing {_STATE_FILE}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _format_alert(changed: dict) -> str:
    lines = ["📈 TRENDING STOCKS UPDATE"]
    for sector, info in changed.items():
        lines.append(f"\n{sector}:")
        if info["newly_in"]:
            lines.append(f"  🆕 New: {', '.join(info['newly_in'])}")
        if info["dropped_out"]:
            lines.append(f"  ⬇️ Dropped: {', '.join(info['dropped_out'])}")
        top = ", ".join(
            f"{s['symbol']} ({s.get('pct_change_1d')}%)" for s in info["current"][:5]
        )
        lines.append(f"  Current top: {top}")
    return "\n".join(lines)


def check_and_alert(sectors: dict) -> bool:
    """sectors: {sector_name: [{symbol, pct_change_1d, ...}, ...]} - the
    same shape trending_scanner.build_trending_by_sector() returns.
    Sends ONE Telegram summary covering every sector whose trending symbol
    set changed since the last alert. Returns True if it sent one."""
    if not config.TRENDING_SEND_TELEGRAM or not market_hours.is_market_open():
        return False

    prev_state = _load_state()
    new_state = {
        sector: sorted(s["symbol"] for s in stocks if s.get("symbol"))
        for sector, stocks in sectors.items()
    }

    changed = {}
    for sector, symbols in new_state.items():
        prev_symbols = set(prev_state.get(sector, []))
        cur_symbols = set(symbols)
        newly_in = cur_symbols - prev_symbols
        dropped_out = prev_symbols - cur_symbols
        if newly_in or dropped_out:
            changed[sector] = {
                "newly_in": sorted(newly_in),
                "dropped_out": sorted(dropped_out),
                "current": sectors[sector],
            }

    # Always persist the latest snapshot, even if nothing "changed" enough
    # to alert on (e.g. a sector disappeared from the scan entirely) - keeps
    # state honest for the next comparison regardless of alert outcome.
    _save_state(new_state)

    if not changed:
        return False

    try:
        telegram_alerts.send_telegram_message(_format_alert(changed))
    except Exception as e:
        print(f"  [warn] trending_alerts: telegram send failed: {e}")
        return False
    return True
