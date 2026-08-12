"""
Cross-scanner trade tracker — one "☆ Track" button on every screener table
(Stock for day, Previous Support Bounce, Swing Trade, DarvaX) writes into
this single journal, so trades taken from ANY scanner show up together in
the "My Trades" tab with live P&L against the current price.

SQLite (stdlib only), same pattern as signal_engine/app/signal_engine/journal.py.
"""
from __future__ import annotations

import sqlite3
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "cache", "trade_tracker.db")


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            source TEXT NOT NULL,          -- which scanner tab it was tracked from
            entry_price REAL NOT NULL,
            stop_loss REAL,
            target REAL,
            current_price REAL,
            pnl_pct REAL,
            status TEXT DEFAULT 'OPEN',    -- OPEN | TARGET_HIT | STOPPED_OUT | CLOSED
            chart_url TEXT,
            notes TEXT,
            tracked_at TEXT,
            closed_at TEXT,
            exit_price REAL
        )
    """)
    conn.commit()
    return conn


def _row_to_dict(conn, row) -> dict:
    cols = [d[0] for d in conn.execute("SELECT * FROM tracked_trades LIMIT 0").description]
    return dict(zip(cols, row))


def track_trade(symbol: str, source: str, entry_price: float,
                 stop_loss: Optional[float] = None, target: Optional[float] = None,
                 chart_url: Optional[str] = None, notes: Optional[str] = None) -> int:
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO tracked_trades
           (symbol, source, entry_price, stop_loss, target, current_price,
            status, chart_url, notes, tracked_at)
           VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
        (symbol.upper(), source, entry_price, stop_loss, target, entry_price,
         chart_url, notes, datetime.now().isoformat()),
    )
    conn.commit()
    trade_id = cur.lastrowid
    conn.close()
    return trade_id


def list_tracked(status: Optional[str] = None) -> list[dict]:
    conn = _connect()
    if status:
        rows = conn.execute(
            "SELECT * FROM tracked_trades WHERE status = ? ORDER BY id DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tracked_trades ORDER BY id DESC").fetchall()
    trades = [_row_to_dict(conn, r) for r in rows]
    conn.close()
    return trades


def refresh_prices(kite_client, universe_df) -> list[dict]:
    """Pull live LTP for every OPEN tracked trade, update P&L, and flip
    status to TARGET_HIT / STOPPED_OUT when price crosses those levels
    (informational only — never places or closes a broker order)."""
    open_trades = list_tracked("OPEN")
    if not open_trades:
        return []

    symbols = sorted({t["symbol"] for t in open_trades})
    tokens = {}
    for sym in symbols:
        row = universe_df[universe_df["tradingsymbol"] == sym]
        if not row.empty:
            tokens[sym] = int(row.iloc[0]["instrument_token"])

    ltp_by_symbol = {}
    if tokens:
        try:
            quotes = kite_client.kite.quote([f"NSE:{s}" for s in tokens])
            for sym in symbols:
                q = quotes.get(f"NSE:{sym}")
                if q:
                    ltp_by_symbol[sym] = float(q["last_price"])
        except Exception as e:
            print(f"  [warn] trade_tracker price refresh failed: {e}")

    conn = _connect()
    updated = []
    for t in open_trades:
        ltp = ltp_by_symbol.get(t["symbol"])
        if ltp is None:
            updated.append(t)
            continue
        entry = float(t["entry_price"])
        pnl_pct = round((ltp - entry) / entry * 100.0, 2) if entry else 0.0
        status = "OPEN"
        if t.get("target") is not None and ltp >= float(t["target"]):
            status = "TARGET_HIT"
        elif t.get("stop_loss") is not None and ltp <= float(t["stop_loss"]):
            status = "STOPPED_OUT"
        conn.execute(
            "UPDATE tracked_trades SET current_price=?, pnl_pct=?, status=? WHERE id=?",
            (ltp, pnl_pct, status, t["id"]),
        )
        t["current_price"] = ltp
        t["pnl_pct"] = pnl_pct
        t["status"] = status
        updated.append(t)
    conn.commit()
    conn.close()
    return updated


def close_trade(trade_id: int, exit_price: Optional[float] = None) -> Optional[dict]:
    conn = _connect()
    row = conn.execute("SELECT * FROM tracked_trades WHERE id = ?", (trade_id,)).fetchone()
    if row is None:
        conn.close()
        return None
    trade = _row_to_dict(conn, row)
    px = exit_price if exit_price is not None else trade.get("current_price") or trade["entry_price"]
    conn.execute(
        "UPDATE tracked_trades SET status='CLOSED', exit_price=?, closed_at=? WHERE id=?",
        (px, datetime.now().isoformat(), trade_id),
    )
    conn.commit()
    conn.close()
    trade["status"] = "CLOSED"
    trade["exit_price"] = px
    return trade


def untrack(trade_id: int) -> bool:
    conn = _connect()
    cur = conn.execute("DELETE FROM tracked_trades WHERE id = ?", (trade_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted
