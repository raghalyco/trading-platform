"""
Turns the Episodic Pivot backtest trade log (ep_backtest.py) into the
summary numbers shown on the dashboard's "EP Backtest (6M)" tab: win rate,
avg win/loss, realized R:R, total return, max drawdown, monthly breakdown,
best/worst stocks — computed fresh from whatever trade log is on disk (or
a freshly-run one), never hardcoded.

Two variants are always reported side by side (see ep_backtest.py's
docstring for why): CONSERVATIVE (stop checked same-day as entry — base
case) and OPTIMISTIC (stop not checked on entry day — upper bound), since
daily OHLC bars can't tell us the true intrabar sequence on the entry day
itself and that single assumption swings the result substantially.

Position sizing note: "Total Return" / "Max Drawdown" use a fixed 1% of
initial capital risked per trade, summed WITHOUT compounding — up to ~69
trades were open concurrently in the reference run, so a compounding
single-account curve would overstate returns by assuming impossible full
reinvestment across simultaneous positions.
"""
import os

import pandas as pd

import ep_backtest

BREAKEVEN_RISK_PCT = 1.0  # % of capital risked per trade for the equity-curve metrics


def _variant_stats(df: pd.DataFrame, pnl_col: str) -> dict:
    wins = df[df[pnl_col] > 0]
    losses = df[df[pnl_col] <= 0]
    n = len(df)
    win_rate = round(len(wins) / n * 100, 1) if n else 0.0
    avg_win = round(wins[pnl_col].mean(), 2) if len(wins) else 0.0
    avg_loss = round(losses[pnl_col].mean(), 2) if len(losses) else 0.0
    win_loss_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss else None

    r_mult = df[pnl_col] / df["stop_pct"]
    avg_r_multiple = round(r_mult.mean(), 3) if n else 0.0
    breakeven_wr = round(abs(avg_loss) / (avg_win + abs(avg_loss)) * 100, 2) if (avg_win + abs(avg_loss)) else None

    running = (r_mult * BREAKEVEN_RISK_PCT).cumsum() if n else pd.Series([], dtype=float)
    total_return_pct = round(float(running.iloc[-1]), 2) if n else 0.0
    if n:
        peak = running.cummax()
        max_dd_pts = round(float((peak - running).max()), 2)
    else:
        max_dd_pts = 0.0

    exit_col = "exit_reason" if pnl_col == "pnl_pct" else "exit_reason_alt"
    exit_counts = df[exit_col].value_counts().to_dict() if n else {}

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "win_loss_ratio": win_loss_ratio,
        "avg_r_multiple": avg_r_multiple,
        "avg_planned_rr": round(df["risk_reward"].mean(), 2) if n else 0.0,
        "breakeven_win_rate_pct": breakeven_wr,
        "total_return_pct": total_return_pct,
        "max_drawdown_pts": max_dd_pts,
        "exit_reason_counts": exit_counts,
    }


def compute_summary(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {
            "trades": 0, "unique_symbols": 0, "entry_date_min": None, "entry_date_max": None,
            "conservative": _variant_stats(df if df is not None else pd.DataFrame(columns=["pnl_pct", "stop_pct", "risk_reward", "exit_reason"]), "pnl_pct"),
            "optimistic": None, "monthly": [], "best_stocks": [], "worst_stocks": [], "max_concurrent_positions": 0,
        }

    df = df.copy()
    df["breakout_date"] = pd.to_datetime(df["breakout_date"])
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    df = df.sort_values("breakout_date").reset_index(drop=True)

    conservative = _variant_stats(df, "pnl_pct")
    optimistic = _variant_stats(df, "pnl_pct_alt") if "pnl_pct_alt" in df.columns else None

    # Max concurrent open positions (sizing/diversification context).
    events = []
    for _, r in df.iterrows():
        events.append((r["breakout_date"], 1))
        events.append((r["exit_date"] + pd.Timedelta(days=1), -1))
    events.sort()
    cur = max_c = 0
    for _, delta in events:
        cur += delta
        max_c = max(max_c, cur)

    # Monthly breakdown (conservative variant).
    df["month"] = df["breakout_date"].dt.to_period("M").astype(str)
    monthly_g = df.groupby("month").agg(
        trades=("pnl_pct", "count"),
        win_rate_pct=("pnl_pct", lambda s: round((s > 0).mean() * 100, 1)),
        avg_pnl_pct=("pnl_pct", lambda s: round(s.mean(), 2)),
        total_pnl_pct=("pnl_pct", lambda s: round(s.sum(), 2)),
    ).reset_index()
    monthly = monthly_g.to_dict(orient="records")

    # Best / worst stocks by total P&L contributed (conservative variant).
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
        "entry_date_min": str(df["breakout_date"].min().date()),
        "entry_date_max": str(df["breakout_date"].max().date()),
        "max_concurrent_positions": max_c,
        "conservative": conservative,
        "optimistic": optimistic,
        "monthly": monthly,
        "best_stocks": best_stocks,
        "worst_stocks": worst_stocks,
    }


def load_trades(refresh: bool = False) -> pd.DataFrame:
    """Loads the cached trade log CSV, or runs the backtest fresh if asked
    (or if no cached log exists yet)."""
    if refresh or not os.path.exists(ep_backtest.TRADES_CSV_PATH):
        return ep_backtest.run_backtest(verbose=False, save_csv=True)
    return pd.read_csv(ep_backtest.TRADES_CSV_PATH)


def get_report(refresh: bool = False) -> dict:
    from datetime import datetime
    df = load_trades(refresh=refresh)
    summary = compute_summary(df)
    summary["generated_at"] = datetime.now().isoformat()
    summary["backtest_months"] = ep_backtest.config.BACKTEST_MONTHS
    summary["universe_label"] = ep_backtest.config.EP_UNIVERSE
    summary["trades_detail"] = df.to_dict(orient="records") if df is not None and not df.empty else []
    return summary
