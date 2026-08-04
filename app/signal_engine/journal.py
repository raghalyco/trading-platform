"""
Auto trade journal. Every trade logs on exit: entry/exit price, time held,
P&L, R:R, signal source. Filterable by WIN/LOSS, exportable to CSV.

Uses SQLite (stdlib only, zero extra dependency) so it works out of the box.
"""

import sqlite3
import csv
import io
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "journal.db")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            side TEXT,
            signal_source TEXT,
            mode TEXT,
            entry_price REAL,
            exit_price REAL,
            entry_time TEXT,
            exit_time TEXT,
            hold_minutes REAL,
            pnl_points REAL,
            pnl_rupees REAL,
            rr REAL,
            result TEXT
        )
    """)
    return conn


def log_entry(symbol: str, side: str, signal_source: str, mode: str,
              entry_price: float, rr: float) -> int:
    """Called when the user clicks IN. Returns the trade's row id (open trade)."""
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO trades (symbol, side, signal_source, mode, entry_price,
           entry_time, rr, result) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
        (symbol, side, signal_source, mode, entry_price, datetime.now().isoformat(), rr),
    )
    conn.commit()
    trade_id = cur.lastrowid
    conn.close()
    return trade_id


def log_exit(trade_id: int, exit_price: float, lot_size: int = 1,
             points_per_lot_value: float = 1.0) -> dict:
    """Called when the user clicks EXIT. Computes P&L and closes the row."""
    conn = _connect()
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"No trade with id {trade_id}")

    cols = [d[0] for d in conn.execute("SELECT * FROM trades LIMIT 0").description]
    trade = dict(zip(cols, row))

    entry_price = trade["entry_price"]
    side = trade["side"]
    sign = 1 if side == "CE" else -1
    pnl_points = sign * (exit_price - entry_price)
    pnl_rupees = pnl_points * lot_size * points_per_lot_value

    entry_dt = datetime.fromisoformat(trade["entry_time"])
    exit_dt = datetime.now()
    hold_minutes = round((exit_dt - entry_dt).total_seconds() / 60, 1)
    result = "WIN" if pnl_points > 0 else ("LOSS" if pnl_points < 0 else "BREAKEVEN")

    conn.execute(
        """UPDATE trades SET exit_price=?, exit_time=?, hold_minutes=?,
           pnl_points=?, pnl_rupees=?, result=? WHERE id=?""",
        (exit_price, exit_dt.isoformat(), hold_minutes, pnl_points, pnl_rupees, result, trade_id),
    )
    conn.commit()
    conn.close()

    return {
        "id": trade_id,
        "pnl_points": round(pnl_points, 2),
        "pnl_rupees": round(pnl_rupees, 2),
        "hold_minutes": hold_minutes,
        "result": result,
    }


def list_trades(result_filter: str | None = None) -> list[dict]:
    """result_filter: 'WIN', 'LOSS', 'OPEN', or None for all."""
    conn = _connect()
    if result_filter:
        rows = conn.execute("SELECT * FROM trades WHERE result = ? ORDER BY id DESC", (result_filter,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM trades LIMIT 0").description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def daily_summary(day: str | None = None) -> dict:
    """
    Aggregates closed trades for a given day (default: today, IST).
    Returns total P&L in rupees, trade count, and win count - matches
    the 'P&L -₹651, 5 trades' style header shown in the reference app.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")

    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    conn = _connect()
    rows = conn.execute(
        "SELECT pnl_rupees, result FROM trades WHERE result != 'OPEN' AND exit_time LIKE ?",
        (f"{day}%",),
    ).fetchall()
    conn.close()

    total_pnl = sum(r[0] for r in rows if r[0] is not None)
    wins = sum(1 for r in rows if r[1] == "WIN")
    losses = sum(1 for r in rows if r[1] == "LOSS")

    return {
        "date": day,
        "pnl_rupees": round(total_pnl, 2),
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
    }


def export_csv() -> str:
    trades = list_trades()
    if not trades:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(trades[0].keys()))
    writer.writeheader()
    writer.writerows(trades)
    return output.getvalue()
