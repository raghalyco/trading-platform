"""
Build journal trade chart payload: candles + entry/exit/target/stop overlays.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.signal_engine import journal
from app.signal_engine.modes import current_expiry_date_iso

IST = ZoneInfo("Asia/Kolkata")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Journal stores local wall time; treat as IST for display/chart.
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _expiry_iso(trade: dict) -> str | None:
    exp = trade.get("expiry")
    if not exp:
        return current_expiry_date_iso(trade.get("symbol") or "NIFTY")
    e = str(exp).strip().upper().replace("-", "").replace(" ", "")
    # Already ISO
    if len(e) == 10 and e[4] == "-":
        return str(exp)
    # DDMMMYY e.g. 11AUG26
    months = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    if len(e) >= 7 and e[:2].isdigit() and e[2:5] in months:
        day = int(e[:2])
        month = months[e[2:5]]
        yy = int(e[5:7])
        year = 2000 + yy
        return f"{year:04d}-{month:02d}-{day:02d}"
    return current_expiry_date_iso(trade.get("symbol") or "NIFTY")


def _fmt_ist(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.astimezone(IST).strftime("%d %b '%y %H:%M")


def _candles_to_list(df: pd.DataFrame) -> list[dict]:
    out = []
    for _, row in df.iterrows():
        ts = row["timestamp"]
        if isinstance(ts, pd.Timestamp):
            if ts.tzinfo is None:
                ts = ts.tz_localize(IST)
            else:
                ts = ts.tz_convert(IST)
            iso = ts.isoformat()
        else:
            iso = str(ts)
        out.append({
            "time": iso,
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": int(row["volume"]) if row.get("volume") is not None else 0,
        })
    return out


def build_trade_chart(feed, trade_id: int) -> dict:
    trade = journal.get_trade(trade_id)
    if not trade:
        return {"ok": False, "error": f"Trade #{trade_id} not found"}

    entry = float(trade.get("entry_premium") or trade.get("entry_price") or 0)
    exit_px = trade.get("exit_premium")
    if exit_px is None:
        exit_px = trade.get("exit_price")
    exit_px = float(exit_px) if exit_px is not None else None

    target = float(trade.get("t1_premium") or trade.get("target1") or 0) or None
    stop = float(trade.get("sl_premium") or trade.get("stop_loss") or 0) or None

    # If stored target/stop look like index levels (>> premium), derive from premium RR
    if target and entry and target > entry * 5:
        target = None
    if stop and entry and stop > entry * 5:
        stop = None
    if target is None and entry:
        # fallback ~ +15% target if missing
        target = round(entry * 1.15, 2)
    if stop is None and entry:
        stop = round(entry * 0.92, 2)

    entry_dt = _parse_dt(trade.get("entry_time"))
    exit_dt = _parse_dt(trade.get("exit_time")) or datetime.now(IST)
    if entry_dt is None:
        entry_dt = exit_dt - timedelta(minutes=30)

    pad = timedelta(minutes=15)
    from_dt = (entry_dt - pad).replace(tzinfo=None)
    to_dt = (exit_dt + pad).replace(tzinfo=None)

    symbol = trade.get("symbol") or "NIFTY"
    side = trade.get("side") or "CE"
    strike = trade.get("strike")
    expiry_iso = _expiry_iso(trade)
    candles: list[dict] = []
    source = "synthetic"
    note = None

    getter = getattr(feed, "get_option_ohlcv_1m", None)
    if getter and strike and expiry_iso:
        try:
            df = getter(symbol, expiry_iso, int(strike), side, from_dt, to_dt)
            if df is not None and len(df) > 0:
                candles = _candles_to_list(df)
                source = "kite"
        except Exception as e:
            note = str(e)

    if not candles:
        # Synthetic bridge from entry -> exit for visualization
        minutes = max(10, int((exit_dt - entry_dt).total_seconds() / 60))
        end_price = exit_px if exit_px is not None else (target or entry)
        rows = []
        for i in range(minutes + 1):
            t = entry_dt + timedelta(minutes=i)
            frac = i / minutes
            mid = entry + (end_price - entry) * frac
            # mild wiggle
            wiggle = (0.5 - abs(0.5 - frac)) * (abs(end_price - entry) * 0.08)
            o = mid - wiggle * 0.2
            c = mid + wiggle * 0.2
            h = max(o, c) + wiggle
            l = min(o, c) - wiggle
            rows.append({
                "time": t.isoformat(),
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": 100,
            })
        candles = rows
        source = "synthetic"
        if note is None:
            note = "Using synthetic path (live option candles unavailable)"

    if exit_px is not None and entry:
        # Validate exit against candle highs when available
        highs = [c["high"] for c in candles] if candles else []
        max_h = max(highs) if highs else None
        if max_h is not None and exit_px > max_h * 1.02:
            note = (
                (note + " · " if note else "")
                + f"Recorded exit ₹{exit_px} never traded (max high ₹{max_h}) — P&L overlay may be wrong; repair via /api/journal/repair"
            )

    pnl_points = None
    if exit_px is not None and entry:
        pnl_points = round(exit_px - entry, 2)
    pnl_pct = None
    if pnl_points is not None and entry:
        pnl_pct = round((pnl_points / entry) * 100, 2)

    risk = abs(entry - stop) if stop else None
    reward = abs((exit_px or target or entry) - entry)
    rr = round(reward / risk, 1) if risk else trade.get("rr")

    title = trade.get("contract") or f"{symbol} {strike or ''} {side}".strip()
    return {
        "ok": True,
        "trade_id": trade_id,
        "title": title,
        "symbol": symbol,
        "side": side,
        "strike": strike,
        "expiry": trade.get("expiry"),
        "expiry_iso": expiry_iso,
        "timezone": "Asia/Kolkata",
        "entry": {
            "price": entry,
            "time": entry_dt.isoformat(),
            "time_label": _fmt_ist(entry_dt),
        },
        "exit": {
            "price": exit_px,
            "time": exit_dt.isoformat() if trade.get("exit_time") else None,
            "time_label": _fmt_ist(exit_dt) if trade.get("exit_time") else None,
        },
        "target": target,
        "stop": stop,
        "pnl_points": pnl_points,
        "pnl_pct": pnl_pct,
        "pnl_rupees": trade.get("pnl_rupees"),
        "rr": rr,
        "result": trade.get("result"),
        "candles": candles,
        "candle_source": source,
        "note": note,
    }
