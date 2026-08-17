"""
Auto trade journal. Every trade logs on exit: entry/exit price, time held,
P&L, R:R, signal source. Filterable by WIN/LOSS, exportable to CSV.

Uses SQLite (stdlib only, zero extra dependency) so it works out of the box.
Also stores Target 1 / SL so the target monitor can alert when T1 is hit.
"""

import sqlite3
import csv
import io
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "journal.db")

_EXTRA_COLUMNS = {
    "target1": "REAL",
    "target2": "REAL",
    "stop_loss": "REAL",
    "contract": "TEXT",
    "expiry": "TEXT",
    "strike": "INTEGER",
    "entry_premium": "REAL",
    "t1_premium": "REAL",
    "t2_premium": "REAL",
    "sl_premium": "REAL",
    "t1_hit_at": "TEXT",
    "t1_alerted": "INTEGER DEFAULT 0",
}


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
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
    }
    for col, typedef in _EXTRA_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
    conn.commit()
    return conn


def _row_to_dict(conn, row) -> dict:
    cols = [d[0] for d in conn.execute("SELECT * FROM trades LIMIT 0").description]
    return dict(zip(cols, row))


def log_entry(symbol: str, side: str, signal_source: str, mode: str,
              entry_price: float, rr: float,
              target1: float | None = None, target2: float | None = None,
              stop_loss: float | None = None, contract: str | None = None,
              expiry: str | None = None, strike: int | None = None,
              entry_premium: float | None = None, t1_premium: float | None = None,
              t2_premium: float | None = None, sl_premium: float | None = None) -> int:
    """Called when the user clicks IN. Returns the trade's row id (open trade)."""
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO trades (
               symbol, side, signal_source, mode, entry_price, entry_time, rr,
               result, target1, target2, stop_loss, contract, expiry, strike,
               entry_premium, t1_premium, t2_premium, sl_premium, t1_alerted
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            symbol, side, signal_source, mode, entry_price,
            datetime.now().isoformat(), rr,
            target1, target2, stop_loss, contract, expiry, strike,
            entry_premium, t1_premium, t2_premium, sl_premium,
        ),
    )
    conn.commit()
    trade_id = cur.lastrowid
    conn.close()
    return trade_id


def log_exit(trade_id: int, exit_price: float, lot_size: int = 1,
             points_per_lot_value: float = 1.0) -> dict:
    """
    Close a trade and compute P&L.

    Premium captures (entry_premium set): always long the option, so
    pnl_points = exit_premium - entry_premium (ignore CE/PE index sign).
    Index-only trades: keep directional sign on index points.
    """
    conn = _connect()
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"No trade with id {trade_id}")

    trade = _row_to_dict(conn, row)
    entry_premium = trade.get("entry_premium")
    side = trade["side"]

    if entry_premium is not None:
        entry = float(entry_premium)
        exit_px = float(exit_price)
        pnl_points = exit_px - entry
        # Store premium exit; keep index entry_price untouched
        exit_col_price = exit_px
    else:
        entry = float(trade["entry_price"])
        exit_px = float(exit_price)
        sign = 1 if side == "CE" else -1
        pnl_points = sign * (exit_px - entry)
        exit_col_price = exit_px

    pnl_rupees = pnl_points * lot_size * points_per_lot_value

    entry_dt = datetime.fromisoformat(trade["entry_time"])
    exit_dt = datetime.now()
    hold_minutes = round((exit_dt - entry_dt).total_seconds() / 60, 1)
    result = "WIN" if pnl_points > 0 else ("LOSS" if pnl_points < 0 else "BREAKEVEN")

    conn.execute(
        """UPDATE trades SET exit_price=?, exit_time=?, hold_minutes=?,
           pnl_points=?, pnl_rupees=?, result=? WHERE id=?""",
        (exit_col_price, exit_dt.isoformat(), hold_minutes,
         round(pnl_points, 2), round(pnl_rupees, 2), result, trade_id),
    )
    conn.commit()
    conn.close()

    return {
        "id": trade_id,
        "pnl_points": round(pnl_points, 2),
        "pnl_rupees": round(pnl_rupees, 2),
        "hold_minutes": hold_minutes,
        "result": result,
        "pricing": "premium" if entry_premium is not None else "index",
    }


def open_trades_awaiting_t1() -> list[dict]:
    """OPEN trades that have a target1 (index or premium) and not yet alerted."""
    conn = _connect()
    rows = conn.execute(
        """SELECT * FROM trades
           WHERE result = 'OPEN'
             AND (target1 IS NOT NULL OR t1_premium IS NOT NULL)
             AND COALESCE(t1_alerted, 0) = 0
           ORDER BY id ASC"""
    ).fetchall()
    trades = [_row_to_dict(conn, r) for r in rows]
    conn.close()
    return trades


def mark_t1_hit(trade_id: int, spot_at_hit: float | None = None) -> None:
    conn = _connect()
    conn.execute(
        """UPDATE trades SET t1_alerted = 1, t1_hit_at = ?
           WHERE id = ?""",
        (datetime.now().isoformat(), trade_id),
    )
    conn.commit()
    conn.close()


def get_trade(trade_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    trade = _row_to_dict(conn, row) if row else None
    conn.close()
    return trade


def list_trades(result_filter: str | None = None, symbol: str | None = None) -> list[dict]:
    """result_filter: 'WIN', 'LOSS', 'OPEN', or None for all.
    symbol: optional NIFTY/SENSEX filter.
    """
    conn = _connect()
    clauses = []
    params: list = []
    if result_filter:
        clauses.append("result = ?")
        params.append(result_filter)
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM trades {where} ORDER BY id DESC", params
    ).fetchall()
    trades = [_row_to_dict(conn, r) for r in rows]
    conn.close()
    return trades


def purge_older_than(days: int) -> int:
    """Delete all trades (OPEN or closed) with entry_time older than `days`
    days ago. Returns the number of rows deleted. Used to clear stale test
    or pre-update data so the journal only shows recent trades."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn = _connect()
    cur = conn.execute("DELETE FROM trades WHERE entry_time < ?", (cutoff,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


def daily_summary(day: str | None = None, symbol: str | None = None) -> dict:
    """
    Aggregates closed trades for a given day (default: today, IST).
    Returns total P&L in rupees, trade count, and win count - matches
    the 'P&L -₹651, 5 trades' style header shown in the reference app.
    """
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")

    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    conn = _connect()
    if symbol:
        rows = conn.execute(
            """SELECT pnl_rupees, result FROM trades
               WHERE result != 'OPEN' AND exit_time LIKE ? AND symbol = ?""",
            (f"{day}%", symbol.upper()),
        ).fetchall()
    else:
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
        "symbol": symbol.upper() if symbol else None,
        "pnl_rupees": round(total_pnl, 2),
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
    }


def export_csv(symbol: str | None = None) -> str:
    trades = list_trades(symbol=symbol)
    if not trades:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(trades[0].keys()))
    writer.writeheader()
    writer.writerows(trades)
    return output.getvalue()
