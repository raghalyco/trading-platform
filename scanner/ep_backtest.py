"""
Episodic Pivot (delayed EP) historical backtest.

Reuses episodic_pivot.py's _augment()/_score() and config.py's EP_* constants
VERBATIM — no new thresholds, no new entry/exit rules invented for this
backtest. The only thing this script adds is the ENUMERATION of every
historical day-0 -> tight-candle -> breakout sequence per symbol (the live
scanner only ever reports the single most-recent one per symbol, since it's
a live dashboard), and a daily-bar trade simulator for each qualifying
breakout signal it finds.

Simulation assumptions (disclosed, not hidden):
  - Entry fill: at the tight-candle's high (the GTT trigger price) on the
    breakout day, UNLESS the day's open already gapped above that price
    (allowed up to EP_MAX_CHASE_GAP_PCT by the scanner's own chase filter),
    in which case fill = that day's open.
  - Exit check order each day starting the entry day: stop first (low <=
    stop), then target (high >= target). This is the standard
    worst-case-same-day daily-bar convention (can't know intrabar sequence
    from OHLC alone) and is deliberately conservative (never favorable to
    the strategy).
  - Time stop: MAX_HOLDING_DAYS (config.py, 60 sessions) — exit at that
    day's close if neither stop nor target has been hit by then.
  - THREE exit variants are simulated per trade (see simulate_exit /
    simulate_exit_trend_break): CONSERVATIVE (base case, above),
    OPTIMISTIC (stop not checked on the entry day — upper bound), and
    KELL_TREND (2026-08-22 Oliver Kell overlay: no fixed target at all,
    exits only on the hard stop or a close below both the 10 and 20 EMA —
    simulated here BEFORE this exit style is ever allowed to drive the
    live scanner's actual exits).
  - Universe / window: EP_UNIVERSE (nifty500) symbols, trade ENTRY dates
    restricted to the trailing BACKTEST_MONTHS (6) window
    (config.refresh_backtest_window()), using each symbol's full cached
    daily history for day-0/tight-candle detection (so an entry in month 1
    of the window can still reference a day-0 that happened earlier).
"""
import glob
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import indicators as ind
from episodic_pivot import _augment, _score

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def find_all_ep_trades(symbol: str, daily: pd.DataFrame) -> list:
    """Mirrors evaluate_symbol_episodic_pivot's day-0 detection + forward
    tight-candle/breakout walk, EXCEPT it (a) considers every day-0 event in
    the symbol's full history, not just the most recent one, and (b) records
    every valid (non-chased) breakout along that walk as its own trade
    candidate, not just the latest. Every gate (stop_pct, RR, score) is the
    exact same code path as the live scanner."""
    min_bars = config.EP_RVOL_AVG_PERIOD + config.EP_NEGLECT_LOOKBACK_DAYS + config.EP_LOOKFORWARD_DAYS + 20
    if daily is None or daily.empty or len(daily) < min_bars:
        return []

    df = _augment(daily)
    n = len(df)
    day0_idxs = df.index[df["day0_flag"]].tolist()

    trades = []
    for i0 in day0_idxs:
        day0 = df.iloc[i0]
        day0_range = float(day0["high"] - day0["low"])
        cutoff_j = min(n - 1, i0 + config.EP_LOOKFORWARD_DAYS)

        active_tight = None
        for j in range(i0 + 1, cutoff_j + 1):
            row = df.iloc[j]
            if bool(row["tight_flag"]):
                active_tight = {
                    "index": j, "date": row["date"], "high": float(row["high"]),
                    "low": float(row["low"]), "tight_range_pct": float(row["tight_range_pct"]),
                    "dist_from_ema10_pct": float(row["dist_from_ema10_pct"]) if pd.notna(row["dist_from_ema10_pct"]) else None,
                }
                continue
            if active_tight is not None:
                if float(row["high"]) > active_tight["high"]:
                    gap_pct = (float(row["open"]) - active_tight["high"]) / active_tight["high"] * 100
                    if gap_pct <= config.EP_MAX_CHASE_GAP_PCT:
                        entry = active_tight["high"]
                        stop = active_tight["low"]
                        risk = entry - stop
                        if risk > 0:
                            stop_pct = round(risk / entry * 100, 2)
                            if stop_pct <= config.EP_MAX_STOP_PCT:
                                target = round(entry + config.EP_TARGET_FLAGPOLE_MULT * day0_range, 2)
                                reward = target - entry
                                rr = round(reward / risk, 2)
                                if rr >= config.EP_MIN_RR:
                                    score = _score(
                                        day0["day0_move_pct"], day0["rvol_pct"], day0["close_strength"],
                                        tight_range_pct=active_tight["tight_range_pct"],
                                        dist_from_ema10_pct=active_tight["dist_from_ema10_pct"],
                                    )
                                    if score >= config.EP_MIN_SCORE:
                                        target_1_3 = round(entry + config.EP_TARGET_R_MULT_INFO * risk, 2)
                                        entry_fill = entry if float(row["open"]) <= entry else float(row["open"])
                                        trades.append({
                                            "symbol": symbol,
                                            "day0_date": str(pd.to_datetime(day0["date"]).date()),
                                            "day0_move_pct": round(float(day0["day0_move_pct"]), 2),
                                            "day0_rvol_pct": round(float(day0["rvol_pct"]), 0),
                                            "pivot_date": str(pd.to_datetime(active_tight["date"]).date()),
                                            "breakout_index": j,
                                            "breakout_date": str(pd.to_datetime(row["date"]).date()),
                                            "entry_price": round(entry, 2),
                                            "entry_fill": round(entry_fill, 2),
                                            "stop_loss": round(stop, 2),
                                            "target": target,
                                            "target_1_3": target_1_3,
                                            "risk_reward": rr,
                                            "stop_pct": stop_pct,
                                            "score": score,
                                        })
                    active_tight = None
                elif float(row["close"]) < active_tight["low"]:
                    active_tight = None  # base failed before it ever broke out

    return trades


def simulate_exit(df: pd.DataFrame, breakout_index: int, entry_fill: float, stop: float, target: float,
                   stop_check_on_entry_day: bool = True):
    """Walk forward from the breakout (entry) day. Stop checked before target
    each day (conservative same-day-ambiguity convention). Time stop at
    config.MAX_HOLDING_DAYS sessions -> exit at that day's close.

    stop_check_on_entry_day=False skips the stop check on the entry day
    itself (only target is checked that day) — an optimistic-bound variant
    used to bracket how much the conservative same-day-stop convention is
    driving results, since OHLC bars can't tell us the true intraday
    open->low->high->close sequence."""
    n = len(df)
    for k, j in enumerate(range(breakout_index, min(n, breakout_index + config.MAX_HOLDING_DAYS + 1))):
        row = df.iloc[j]
        low = float(row["low"])
        high = float(row["high"])
        check_stop = stop_check_on_entry_day or k > 0
        if check_stop and low <= stop:
            return {
                "exit_date": str(pd.to_datetime(row["date"]).date()),
                "exit_price": round(stop, 2),
                "exit_reason": "STOP",
                "holding_days": k,
            }
        if high >= target:
            return {
                "exit_date": str(pd.to_datetime(row["date"]).date()),
                "exit_price": round(target, 2),
                "exit_reason": "TARGET",
                "holding_days": k,
            }
    # time stop / ran out of data
    last_j = min(n - 1, breakout_index + config.MAX_HOLDING_DAYS)
    row = df.iloc[last_j]
    reason = "TIME_STOP" if (last_j - breakout_index) >= config.MAX_HOLDING_DAYS else "END_OF_DATA"
    return {
        "exit_date": str(pd.to_datetime(row["date"]).date()),
        "exit_price": round(float(row["close"]), 2),
        "exit_reason": reason,
        "holding_days": last_j - breakout_index,
    }


def simulate_exit_trend_break(df: pd.DataFrame, breakout_index: int, stop: float):
    """3rd exit variant — Oliver Kell's trend-following exit (2026-08-22
    decision: simulate this as a backtest variant BEFORE it's ever allowed
    to drive the live scanner). Unlike the Conservative/Optimistic variants,
    this one has NO fixed price target at all — that's the point of Kell's
    system: let a confirmed trend run (hold/add) rather than take a
    flagpole-projected profit, and only exit on a genuine trend break
    (close below BOTH the 10 and 20 EMA — episodic_pivot.py's
    kell_ema_break column) or the same hard stop-loss everyone gets. Stop
    is checked same-day as entry (conservative convention, for apples-to-
    apples comparability with the base-case variant)."""
    n = len(df)
    for k, j in enumerate(range(breakout_index, min(n, breakout_index + config.MAX_HOLDING_DAYS + 1))):
        row = df.iloc[j]
        low = float(row["low"])
        if low <= stop:
            return {
                "exit_date": str(pd.to_datetime(row["date"]).date()),
                "exit_price": round(stop, 2),
                "exit_reason": "STOP",
                "holding_days": k,
            }
        if k > 0 and bool(row.get("kell_ema_break", False)):
            return {
                "exit_date": str(pd.to_datetime(row["date"]).date()),
                "exit_price": round(float(row["close"]), 2),
                "exit_reason": "EMA_BREAK",
                "holding_days": k,
            }
    last_j = min(n - 1, breakout_index + config.MAX_HOLDING_DAYS)
    row = df.iloc[last_j]
    reason = "TIME_STOP" if (last_j - breakout_index) >= config.MAX_HOLDING_DAYS else "END_OF_DATA"
    return {
        "exit_date": str(pd.to_datetime(row["date"]).date()),
        "exit_price": round(float(row["close"]), 2),
        "exit_reason": reason,
        "holding_days": last_j - breakout_index,
    }


RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.RESULTS_DIR)
TRADES_CSV_PATH = os.path.join(RESULTS_DIR, "ep_backtest_trades.csv")


def run_backtest(verbose: bool = True, save_csv: bool = True) -> pd.DataFrame:
    """Runs the full historical enumeration + simulation described in this
    module's docstring and returns the resulting trade log as a DataFrame
    (one row per real, de-duplicated trade). Used by both the CLI
    (`python ep_backtest.py`) and the dashboard's "Run Backtest" button
    (app.py's /api/episodic_pivot/backtest route)."""
    bt_start, bt_end = config.refresh_backtest_window()
    if verbose:
        print(f"Backtest window (trade ENTRY dates): {bt_start} to {bt_end}")

    files = sorted(glob.glob(os.path.join(CACHE_DIR, "daily_*.parquet")))
    if verbose:
        print(f"Found {len(files)} cached daily files")

    all_trades = []
    scanned = 0
    for fp in files:
        symbol = os.path.basename(fp)[len("daily_"):-len(".parquet")]
        try:
            daily = pd.read_parquet(fp)
        except Exception as e:
            if verbose:
                print(f"  [warn] {symbol}: failed to read ({e})")
            continue
        if daily is None or daily.empty:
            continue
        daily = daily.sort_values("date").reset_index(drop=True)
        scanned += 1
        try:
            df_aug = _augment(daily)
            trades = find_all_ep_trades(symbol, daily)
        except Exception as e:
            if verbose:
                print(f"  [warn] {symbol}: eval failed ({e})")
            continue

        for t in trades:
            entry_date = pd.to_datetime(t["breakout_date"]).date()
            if not (bt_start <= entry_date <= bt_end):
                continue
            exit_info = simulate_exit(df_aug, t["breakout_index"], t["entry_fill"], t["stop_loss"], t["target"],
                                       stop_check_on_entry_day=True)
            pnl_pct = round((exit_info["exit_price"] - t["entry_fill"]) / t["entry_fill"] * 100, 2)
            exit_info_alt = simulate_exit(df_aug, t["breakout_index"], t["entry_fill"], t["stop_loss"], t["target"],
                                           stop_check_on_entry_day=False)
            pnl_pct_alt = round((exit_info_alt["exit_price"] - t["entry_fill"]) / t["entry_fill"] * 100, 2)
            exit_info_kell = simulate_exit_trend_break(df_aug, t["breakout_index"], t["stop_loss"])
            pnl_pct_kell = round((exit_info_kell["exit_price"] - t["entry_fill"]) / t["entry_fill"] * 100, 2)
            trade = {
                **t, **exit_info, "pnl_pct": pnl_pct,
                "exit_date_alt": exit_info_alt["exit_date"], "exit_price_alt": exit_info_alt["exit_price"],
                "exit_reason_alt": exit_info_alt["exit_reason"], "holding_days_alt": exit_info_alt["holding_days"],
                "pnl_pct_alt": pnl_pct_alt,
                "exit_date_kell": exit_info_kell["exit_date"], "exit_price_kell": exit_info_kell["exit_price"],
                "exit_reason_kell": exit_info_kell["exit_reason"], "holding_days_kell": exit_info_kell["holding_days"],
                "pnl_pct_kell": pnl_pct_kell,
            }
            all_trades.append(trade)

    if verbose:
        print(f"Scanned {scanned} symbols, found {len(all_trades)} trades entered within the backtest window")

    out_df = pd.DataFrame(all_trades)

    # De-duplicate: when multiple day-0 events are close together, each one's
    # independent forward walk can "see" and record the SAME tight-candle
    # breakout more than once (identical pivot/entry/stop, different day0
    # anchor). A single breakout is one real trade. Keep the instance whose
    # day-0 is the MOST RECENT one before the pivot — this matches the live
    # scanner's own rule (evaluate_symbol_episodic_pivot uses
    # day0_candidates.max(), i.e. the freshest qualifying day-0).
    before = len(out_df)
    if not out_df.empty:
        out_df = out_df.sort_values("day0_date").drop_duplicates(
            subset=["symbol", "pivot_date", "breakout_date", "entry_price", "stop_loss"],
            keep="last",
        ).reset_index(drop=True)
    if verbose:
        print(f"De-duplicated {before - len(out_df)} overlapping-day0 duplicate trade(s) -> {len(out_df)} unique trades")

    if save_csv:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        out_df.to_csv(TRADES_CSV_PATH, index=False)
        if verbose:
            print(f"Saved trade log -> {TRADES_CSV_PATH}")

    return out_df


def main():
    run_backtest(verbose=True, save_csv=True)


if __name__ == "__main__":
    main()
