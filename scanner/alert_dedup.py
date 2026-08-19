"""
Persistent Telegram-alert dedup.

The previous approach (a plain in-memory `set()` per module, e.g.
swing_trade.py's old `_alerted_breakouts` / smart_money_pipeline.py's old
`_seen_signal_keys`) only dedups "within a process lifetime" - restarting
the app (`python app.py`) wipes that set back to empty, so the startup
cache-warm scan re-evaluates the same still-active signals and, since
nothing looks "seen" anymore, re-sends a Telegram alert for every one of
them even though they were already alerted before the restart. This was
the exact cause of "repeated telegram message with same stocks" on every
app start.

Fix: persist alerted keys to a small JSON file in cache/, so a restart
doesn't reset what's already been alerted. Callers should still prefix
their keys by source (e.g. "swing_trade:", "smart_money:") so different
scanners' dedup namespaces can't collide.
"""
import json
import os

import config

_DEDUP_FILE = os.path.join(config.CACHE_DIR, "alerted_keys.json")
_keys: set | None = None


def _load() -> set:
    global _keys
    if _keys is not None:
        return _keys
    if os.path.exists(_DEDUP_FILE):
        try:
            with open(_DEDUP_FILE, "r", encoding="utf-8") as f:
                _keys = set(json.load(f))
        except Exception as e:
            print(f"  [warn] alert_dedup: corrupt {_DEDUP_FILE} ({e}) - starting fresh")
            _keys = set()
    else:
        _keys = set()
    return _keys


def _save() -> None:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    tmp = _DEDUP_FILE + f".tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(_load()), f)
        os.replace(tmp, _DEDUP_FILE)
    except Exception as e:
        print(f"  [warn] alert_dedup: failed writing {_DEDUP_FILE}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def already_alerted(key: str) -> bool:
    return key in _load()


def mark_alerted(key: str) -> None:
    _load().add(key)
    _save()
