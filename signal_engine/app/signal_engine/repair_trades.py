"""
Repair closed premium trades whose P&L used index-vs-premium math,
and/or whose exit price never traded on the candle tape.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.signal_engine import journal
from app.signal_engine.live_capture import LOT_SIZES
from app.signal_engine.trade_chart import _expiry_iso, _parse_dt

IST = ZoneInfo("Asia/Kolkata")


def _apply(trade_id: int, exit_px: float, exit_time_iso: str | None, lot: int) -> dict:
    trade = journal.get_trade(trade_id)
    entry = float(trade["entry_premium"])
    pnl = round(exit_px - entry, 2)
    rupees = round(pnl * lot, 2)
    result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
    conn = journal._connect()
    if exit_time_iso:
        conn.execute(
            """UPDATE trades SET exit_price=?, exit_time=?, pnl_points=?, pnl_rupees=?, result=?
               WHERE id=?""",
            (exit_px, exit_time_iso, pnl, rupees, result, trade_id),
        )
    else:
        conn.execute(
            """UPDATE trades SET exit_price=?, pnl_points=?, pnl_rupees=?, result=?
               WHERE id=?""",
            (exit_px, pnl, rupees, result, trade_id),
        )
    conn.commit()
    conn.close()
    return {
        "id": trade_id,
        "entry_premium": entry,
        "exit_premium": exit_px,
        "pnl_points": pnl,
        "pnl_rupees": rupees,
        "result": result,
    }


def repair_premium_trade(feed, trade_id: int) -> dict:
    trade = journal.get_trade(trade_id)
    if not trade:
        return {"ok": False, "error": "not found"}
    if trade.get("entry_premium") is None:
        return {"ok": False, "error": "not a premium trade", "id": trade_id}
    if trade.get("result") == "OPEN":
        return {"ok": False, "error": "still open", "id": trade_id}

    entry = float(trade["entry_premium"])
    sl = trade.get("sl_premium")
    t1 = trade.get("t1_premium")
    lot = LOT_SIZES.get(trade.get("symbol") or "NIFTY", 65)
    recorded_exit = trade.get("exit_price")
    entry_dt = _parse_dt(trade.get("entry_time"))
    exit_dt = _parse_dt(trade.get("exit_time")) or entry_dt

    # Fetch candles around the hold
    candles = []
    getter = getattr(feed, "get_option_ohlcv_1m", None)
    strike = trade.get("strike")
    expiry_iso = _expiry_iso(trade)
    if getter and strike and expiry_iso and entry_dt:
        try:
            from_dt = (entry_dt - timedelta(minutes=5)).replace(tzinfo=None)
            to_dt = (exit_dt + timedelta(minutes=10)).replace(tzinfo=None)
            df = getter(
                trade["symbol"], expiry_iso, int(strike), trade["side"], from_dt, to_dt,
            )
            if df is not None and len(df):
                for _, row in df.iterrows():
                    ts = row["timestamp"]
                    if hasattr(ts, "to_pydatetime"):
                        ts = ts.to_pydatetime()
                    if getattr(ts, "tzinfo", None) is None:
                        ts = ts.replace(tzinfo=IST)
                    else:
                        ts = ts.astimezone(IST)
                    candles.append({
                        "ts": ts,
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                    })
        except Exception as e:
            return {
                "ok": False,
                "id": trade_id,
                "error": f"candle fetch failed: {e}",
                "fallback": _apply(trade_id, float(recorded_exit), None, lot)
                if recorded_exit and float(recorded_exit) < entry * 5
                else None,
            }

    if not candles:
        # At least fix index-style P&L if exit looks like premium
        if recorded_exit is not None and float(recorded_exit) < entry * 5:
            fixed = _apply(trade_id, float(recorded_exit), None, lot)
            return {"ok": True, "mode": "pnl_only", **fixed}
        return {"ok": False, "id": trade_id, "error": "no candles and exit looks invalid"}

    # Walk from entry: first T1 or SL hit wins; else close at recorded exit time close
    hit_px = None
    hit_ts = None
    hit_why = None
    for c in candles:
        if c["ts"] < entry_dt:
            continue
        if t1 is not None and c["high"] >= float(t1):
            hit_px, hit_ts, hit_why = float(t1), c["ts"], "T1"
            break
        if sl is not None and c["low"] <= float(sl):
            hit_px, hit_ts, hit_why = float(sl), c["ts"], "SL"
            break

    if hit_px is None:
        # Use candle close nearest recorded exit time
        target_ts = exit_dt
        nearest = min(candles, key=lambda c: abs((c["ts"] - target_ts).total_seconds()))
        # If recorded exit never traded (far above max high), trust tape
        max_high = max(c["high"] for c in candles if c["ts"] >= entry_dt)
        if recorded_exit and float(recorded_exit) > max_high * 1.02:
            hit_px = nearest["close"]
            hit_ts = nearest["ts"]
            hit_why = "tape_close_invalid_recorded_exit"
        else:
            hit_px = float(recorded_exit) if recorded_exit and float(recorded_exit) < entry * 5 else nearest["close"]
            hit_ts = nearest["ts"]
            hit_why = "recorded_or_tape"

    fixed = _apply(trade_id, hit_px, hit_ts.isoformat(), lot)
    return {"ok": True, "mode": hit_why, "max_high_after_entry": max(
        (c["high"] for c in candles if c["ts"] >= entry_dt), default=None
    ), **fixed}


def repair_all_premium_trades(feed) -> list[dict]:
    out = []
    for t in journal.list_trades():
        if t.get("entry_premium") is None or t.get("result") == "OPEN":
            continue
        out.append(repair_premium_trade(feed, t["id"]))
    return out
