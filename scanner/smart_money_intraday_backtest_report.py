"""
Turns the Smart Money intraday SELL backtest trade log
(smart_money_intraday_backtest.py) into the summary numbers shown on the
"Smart Money Intraday SELL (F&O)" dashboard tab: win rate, avg win/loss,
realized R-multiple, total return, max drawdown, monthly breakdown,
best/worst stocks — same methodology as ep_backtest_report.py (R-multiple
= pnl_pct / stop_pct, a fixed 1%-of-capital-risked-per-trade equity curve)
so the two reports read the same way.

Unlike the Episodic Pivot backtest (which walks DAILY bars and so has to
report CONSERVATIVE/OPTIMISTIC/KELL_TREND variants to hedge against not
knowing the true intrabar stop-vs-target sequence), this backtest walks
the real 5-minute bars the trade would have lived on, so there is only
ONE exit variant: "actual" — the stop/target/eod_square_off sequence is
exactly what the bars show, no ambiguity to hedge.

Needs a LIVE Kite session to compute a fresh run (it fetches 5-min/15-min
intraday history), so the route that calls get_report() passes in the
already-logged-in kite_client from app.py rather than opening a second
session here. A cached run is reloaded instantly from results/ unless
refresh=True is passed.
"""
import json
import os
from datetime import datetime

import pandas as pd

import smart_money_intraday_backtest as smib

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
TRADES_CSV_PATH = os.path.join(RESULTS_DIR, "sm_intraday_sell_backtest_trades.csv")
META_JSON_PATH = TRADES_CSV_PATH + ".meta.json"
BREAKEVEN_RISK_PCT = 1.0  # % of capital risked per trade for the equity-curve metrics

_EMPTY_COLUMNS = ["symbol", "pnl_pct", "stop_pct", "risk_reward", "exit_reason",
                  "entry_time", "exit_time"]


def _stats(df: pd.DataFrame) -> dict:
    n = len(df)
    if n == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "win_loss_ratio": None,
            "avg_r_multiple": 0.0, "avg_planned_rr": 0.0,
            "breakeven_win_rate_pct": None, "total_return_pct": 0.0,
            "max_drawdown_pts": 0.0, "exit_reason_counts": {},
        }

    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    win_rate = round(len(wins) / n * 100, 1)
    avg_win = round(wins["pnl_pct"].mean(), 2) if len(wins) else 0.0
    avg_loss = round(losses["pnl_pct"].mean(), 2) if len(losses) else 0.0
    win_loss_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss else None

    r_mult = df["pnl_pct"] / df["stop_pct"]
    avg_r_multiple = round(r_mult.mean(), 3)
    breakeven_wr = (
        round(abs(avg_loss) / (avg_win + abs(avg_loss)) * 100, 2)
        if (avg_win + abs(avg_loss)) else None
    )

    running = (r_mult * BREAKEVEN_RISK_PCT).cumsum()
    total_return_pct = round(float(running.iloc[-1]), 2)
    peak = running.cummax()
    max_dd_pts = round(float((peak - running).max()), 2)

    exit_counts = df["exit_reason"].value_counts().to_dict()

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "win_loss_ratio": win_loss_ratio,
        "avg_r_multiple": avg_r_multiple,
        "avg_planned_rr": round(df["risk_reward"].mean(), 2),
        "breakeven_win_rate_pct": breakeven_wr,
        "total_return_pct": total_return_pct,
        "max_drawdown_pts": max_dd_pts,
        "exit_reason_counts": exit_counts,
    }


def compute_summary(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {
            "trades": 0, "unique_symbols": 0, "entry_date_min": None, "entry_date_max": None,
            "max_concurrent_positions": 0,
            "actual": _stats(pd.DataFrame(columns=_EMPTY_COLUMNS)),
            "monthly": [], "best_stocks": [], "worst_stocks": [],
        }

    df = df.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    df = df.sort_values("entry_time").reset_index(drop=True)

    actual = _stats(df)

    # Max concurrent open positions (sizing/diversification context).
    events = []
    for _, r in df.iterrows():
        events.append((r["entry_time"], 1))
        events.append((r["exit_time"], -1))
    events.sort()
    cur = max_c = 0
    for _, delta in events:
        cur += delta
        max_c = max(max_c, cur)

    df["month"] = df["entry_time"].dt.to_period("M").astype(str)
    monthly_g = df.groupby("month").agg(
        trades=("pnl_pct", "count"),
        win_rate_pct=("pnl_pct", lambda s: round((s > 0).mean() * 100, 1)),
        avg_pnl_pct=("pnl_pct", lambda s: round(s.mean(), 2)),
        total_pnl_pct=("pnl_pct", lambda s: round(s.sum(), 2)),
    ).reset_index()
    monthly = monthly_g.to_dict(orient="records")

    by_symbol = df.groupby("symbol").agg(
        trades=("pnl_pct", "count"),
        total_pnl_pct=("pnl_pct", lambda s: round(s.sum(), 2)),
        avg_pnl_pct=("pnl_pct", lambda s: round(s.mean(), 2)),
        win_rate_pct=("pnl_pct", lambda s: round((s > 0).mean() * 100, 1)),
    ).reset_index().sort_values("total_pnl_pct", ascending=False)
    best_stocks = by_symbol.head(10).to_dict(orient="records")
    worst_stocks = by_symbol.tail(10).sort_values("total_pnl_pct").to_dict(orient="records")

    return {
        "trades": len(df),
        "unique_symbols": int(df["symbol"].nunique()),
        "entry_date_min": str(df["entry_time"].min().date()),
        "entry_date_max": str(df["entry_time"].max().date()),
        "max_concurrent_positions": max_c,
        "actual": actual,
        "monthly": monthly,
        "best_stocks": best_stocks,
        "worst_stocks": worst_stocks,
    }


def _save(df: pd.DataFrame, meta: dict) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    df.to_csv(TRADES_CSV_PATH, index=False)
    with open(META_JSON_PATH, "w") as f:
        json.dump(meta, f)


def load_trades(kite_client, refresh: bool = False, months=None) -> tuple:
    """Returns (trades_df, meta).

    A full backtest over several months at 5-min resolution across the
    whole F&O universe is too slow to run on demand, so instead of
    recomputing a rolling N-month window from scratch every time, this
    keeps a persisted trade log (results/sm_intraday_sell_backtest_trades.csv)
    and grows it day by day:
      - No cache yet, or refresh=True: (re)build from the CURRENT CALENDAR
        MONTH only (fast) rather than 3 months.
      - Every later call (refresh=False, cache present): fetch only the
        days from the last cached trade's entry date onward and APPEND
        them to the log (de-duplicated), so the history you've got keeps
        accumulating the longer this keeps getting called, without ever
        re-scanning months of data in one request. Call this often (e.g.
        on each dashboard load, or a daily schedule) to keep it current.
      - `months`: back-compat override for the CLI's --months flag — a
        full one-shot rebuild of that fixed trailing window, bypassing the
        current-month/incremental behavior above entirely.
    """
    if months is not None:
        result = smib.run_intraday_sell_backtest(kite_client, months=months)
        trades = result.pop("trades")
        df = pd.DataFrame(trades)
        _save(df, result)
        return df, result

    have_cache = os.path.exists(TRADES_CSV_PATH) and os.path.exists(META_JSON_PATH)
    today = datetime.now()
    month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if not have_cache or refresh:
        result = smib.run_intraday_sell_backtest(kite_client, start_dt=month_start, end_dt=today)
        trades = result.pop("trades")
        df = pd.DataFrame(trades)
        result["monitoring_mode"] = "current_month_rebuild"
        _save(df, result)
        return df, result

    # Incremental day-to-day top-up: fetch only what's new since the last
    # cached trade and append it (re-fetching the last cached day too, in
    # case that run happened mid-session and missed late trades).
    old_df = pd.read_csv(TRADES_CSV_PATH)
    with open(META_JSON_PATH) as f:
        old_meta = json.load(f)

    if not old_df.empty and "entry_time" in old_df.columns:
        fetch_start = pd.to_datetime(old_df["entry_time"]).max().normalize()
    else:
        fetch_start = month_start

    if fetch_start.date() > today.date():
        return old_df, old_meta  # already caught up today

    result = smib.run_intraday_sell_backtest(kite_client, start_dt=fetch_start, end_dt=today)
    new_trades = result.pop("trades")
    new_df = pd.DataFrame(new_trades)

    if not new_df.empty:
        combined = pd.concat([old_df, new_df], ignore_index=True)
        dedup_keys = [c for c in ("symbol", "entry_time") if c in combined.columns]
        if dedup_keys:
            combined = combined.drop_duplicates(subset=dedup_keys, keep="last")
    else:
        combined = old_df

    merged_meta = dict(result)
    merged_meta["window_start"] = old_meta.get("window_start", merged_meta.get("window_start"))
    merged_meta["months"] = None
    merged_meta["monitoring_mode"] = "daily_accumulating"
    _save(combined, merged_meta)
    return combined, merged_meta


def get_report(kite_client, refresh: bool = False, months=None) -> dict:
    df, meta = load_trades(kite_client, refresh=refresh, months=months)
    summary = compute_summary(df)
    summary["generated_at"] = datetime.now().isoformat()
    summary["window_start"] = meta.get("window_start")
    summary["window_end"] = meta.get("window_end")
    summary["months"] = meta.get("months")
    summary["universe_size"] = meta.get("universe_size")
    summary["signal_interval"] = meta.get("signal_interval")
    summary["htf_interval"] = meta.get("htf_interval")
    summary["downtrend_filter"] = meta.get("downtrend_filter")
    summary["monitoring_mode"] = meta.get("monitoring_mode")
    summary["trades_detail"] = df.to_dict(orient="records") if df is not None and not df.empty else []
    return summary
