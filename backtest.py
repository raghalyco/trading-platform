"""
For every symbol in the universe:
  1. Pull daily history from (BACKTEST_START - WARMUP_YEARS) to BACKTEST_END
  2. Compute indicators/signal over the whole thing (so EMA200 etc. are warm)
  3. Keep only signals whose date falls inside [BACKTEST_START, BACKTEST_END]
  4. Simulate a trade per signal:
       entry  = next trading day's open
       exit   = first day close >= entry * (1 + TARGET_MAX_PCT/100)   -> "target_hit"
                else, after MAX_HOLDING_DAYS with no target -> "time_stop"
                (optional STOP_LOSS_PCT checked day by day if configured)
     Overlapping signals on the same symbol while a trade is already open
     are skipped (one position at a time per symbol), matching how you'd
     realistically trade this off a Telegram/scanner alert.
"""
from datetime import timedelta

import pandas as pd
from tqdm import tqdm

import config
import scanner


def simulate_trades(sig_df: pd.DataFrame, symbol: str,
                    start=None, end=None) -> list:
    trades = []
    in_position = False
    exit_idx = -1
    window_start = start if start is not None else config.BACKTEST_START
    window_end = end if end is not None else config.BACKTEST_END

    signal_rows = sig_df.index[
        sig_df["signal"] & (sig_df["date"].dt.date >= window_start)
        & (sig_df["date"].dt.date <= window_end)
    ].tolist()

    for i in signal_rows:
        if in_position and i <= exit_idx:
            continue  # still holding a prior position, skip overlapping signal

        entry_i = i + 1
        if entry_i >= len(sig_df):
            continue  # signal on the last available day, no next-day open to enter on

        entry_date = sig_df.loc[entry_i, "date"]
        entry_price = sig_df.loc[entry_i, "open"]
        target_price = entry_price * (1 + config.TARGET_MAX_PCT / 100)
        stop_price = (entry_price * (1 + config.STOP_LOSS_PCT / 100)
                      if config.STOP_LOSS_PCT is not None else None)

        exit_price = None
        exit_date = None
        exit_reason = None
        j_final = min(entry_i + config.MAX_HOLDING_DAYS, len(sig_df) - 1)

        for j in range(entry_i, j_final + 1):
            row = sig_df.loc[j]
            if stop_price is not None and row["low"] <= stop_price:
                exit_price, exit_date, exit_reason = stop_price, row["date"], "stop_loss"
                break
            if row["high"] >= target_price:
                exit_price, exit_date, exit_reason = target_price, row["date"], "target_hit"
                break

        if exit_price is None:
            # timed out: exit at the close of the last day in the holding window
            row = sig_df.loc[j_final]
            exit_price, exit_date, exit_reason = row["close"], row["date"], "time_stop"

        pnl_pct = (exit_price / entry_price - 1) * 100
        duration_days = (exit_date - entry_date).days

        trades.append({
            "symbol": symbol,
            "signal_date": str(sig_df.loc[i, "date"].date()),
            "entry_date": str(entry_date.date()),
            "entry_price": round(entry_price, 2),
            "exit_date": str(exit_date.date()),
            "exit_price": round(exit_price, 2),
            "pnl_pct": round(pnl_pct, 2),
            "duration_days": duration_days,
            "exit_reason": exit_reason,
            "hit_min_target": pnl_pct >= config.TARGET_MIN_PCT,
        })

        in_position = True
        exit_idx = j_final if exit_reason == "time_stop" else j

    return trades


def run_backtest(kite_client, universe_df: pd.DataFrame) -> pd.DataFrame:
    config.refresh_backtest_window()  # always the latest BACKTEST_MONTHS ending today
    from_date = config.BACKTEST_START - timedelta(days=365 * config.WARMUP_YEARS)
    to_date = config.BACKTEST_END

    all_trades = []
    all_signal_counts = []

    for _, row in tqdm(universe_df.iterrows(), total=len(universe_df), desc="Backtesting"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]

        daily = kite_client.get_daily_history(token, symbol, from_date, to_date)
        if daily.empty or len(daily) < config.EMA_SLOW:
            continue  # not enough history to trust EMA200 etc.

        sig_df = scanner.compute_signals(daily)
        trades = simulate_trades(sig_df, symbol)
        all_trades.extend(trades)
        all_signal_counts.append({"symbol": symbol, "num_signals": len(trades)})

    trades_df = pd.DataFrame(all_trades)
    return trades_df


def summarize(trades_df: pd.DataFrame) -> dict:
    if trades_df.empty:
        return {"total_trades": 0}
    return {
        "total_trades": len(trades_df),
        "win_rate_pct": round((trades_df["pnl_pct"] > 0).mean() * 100, 1),
        "hit_min_10pct_target_rate": round(trades_df["hit_min_target"].mean() * 100, 1),
        "avg_pnl_pct": round(trades_df["pnl_pct"].mean(), 2),
        "median_pnl_pct": round(trades_df["pnl_pct"].median(), 2),
        "avg_duration_days": round(trades_df["duration_days"].mean(), 1),
        "target_hit_count": int((trades_df["exit_reason"] == "target_hit").sum()),
        "time_stop_count": int((trades_df["exit_reason"] == "time_stop").sum()),
        "stop_loss_count": int((trades_df["exit_reason"] == "stop_loss").sum()),
    }
