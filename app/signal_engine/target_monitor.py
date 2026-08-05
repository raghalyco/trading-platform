"""
Watch open journal trades and fire a one-shot Telegram alert when Target 1
is touched.

Prefer live option premium vs t1_premium when available; otherwise fall back
to index spot vs target1 (index points).
"""

from __future__ import annotations

from app.signal_engine import journal
from app.signal_engine.modes import current_expiry_date_iso
from app.telegram_alerts import format_t1_hit_message, send_telegram_message, telegram_configured


def _index_t1_hit(side: str, spot: float, high: float | None, low: float | None, t1: float) -> bool:
    hi = high if high is not None else spot
    lo = low if low is not None else spot
    if side == "CE":
        return hi >= t1 or spot >= t1
    if side == "PE":
        return lo <= t1 or spot <= t1
    return False


def _premium_t1_hit(ltp: float, t1_premium: float) -> bool:
    # Long option: T1 is above entry premium
    return ltp >= t1_premium


def check_and_alert_t1(feed, dry_run: bool = False) -> list[dict]:
    pending = journal.open_trades_awaiting_t1()
    if not pending:
        return []

    results = []
    cache: dict[str, dict] = {}

    for trade in pending:
        symbol = trade["symbol"]
        side = trade["side"]
        index_t1 = trade.get("target1")
        prem_t1 = trade.get("t1_premium")
        hit = False
        spot_now = None
        mode_used = None

        # 1) Prefer live option LTP vs premium T1
        if prem_t1 is not None and trade.get("strike") is not None:
            expiry_iso = current_expiry_date_iso(symbol)
            getter = getattr(feed, "get_option_ltp", None)
            if getter and expiry_iso:
                try:
                    quote = getter(symbol, expiry_iso, int(trade["strike"]), side)
                    ltp = float(quote["ltp"])
                    spot_now = ltp
                    if _premium_t1_hit(ltp, float(prem_t1)):
                        hit = True
                        mode_used = "premium"
                except Exception:
                    pass

        # 2) Fallback: index levels
        if not hit and index_t1 is not None:
            if symbol not in cache:
                try:
                    spot = float(feed.get_spot_price(symbol))
                    df = feed.get_ohlcv_1m(symbol, lookback_minutes=5)
                    last = df.iloc[-1] if len(df) else None
                    cache[symbol] = {
                        "spot": spot,
                        "high": float(last["high"]) if last is not None else spot,
                        "low": float(last["low"]) if last is not None else spot,
                    }
                except Exception as e:
                    results.append({
                        "trade_id": trade["id"],
                        "ok": False,
                        "error": f"feed failed: {e}",
                    })
                    continue
            px = cache[symbol]
            spot_now = px["spot"]
            if _index_t1_hit(side, px["spot"], px["high"], px["low"], float(index_t1)):
                hit = True
                mode_used = "index"

        if not hit:
            results.append({
                "trade_id": trade["id"],
                "ok": True,
                "hit": False,
                "spot": spot_now,
                "target1": prem_t1 or index_t1,
            })
            continue

        payload = {**trade, "spot_now": spot_now}
        text = format_t1_hit_message(payload)
        dry = dry_run or not telegram_configured()
        send_result = send_telegram_message(text, dry_run=dry)
        journal.mark_t1_hit(trade["id"], spot_now)

        results.append({
            "trade_id": trade["id"],
            "ok": send_result.get("ok", False),
            "hit": True,
            "mode": mode_used,
            "dry_run": send_result.get("dry_run", dry),
            "spot": spot_now,
            "target1": prem_t1 or index_t1,
            "message": text,
            "telegram": send_result,
        })

    return results
