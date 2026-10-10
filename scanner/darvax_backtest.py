"""
DarvaX (Darvas Box) historical backtest — 6 months, mirroring the shape of
ep_backtest.py: enumerate EVERY historical box breakout per symbol (the live
scanner only ever reports each symbol's single most-recent one), simulate a
trade per breakout, and report win rate / R-multiples / drawdown the same
way the Episodic Pivot backtest tab does.

Reuses darvax.py's find_darvas_boxes() and _timeframe_params() VERBATIM for
box detection - no new box-construction rules invented here. What differs
from the live evaluate_symbol_darvax() is necessarily different, and is
disclosed explicitly:

  1. LOOKAHEAD FIX — the live scanner's quality score uses
     `dist_from_ath_pct` computed against the high of the WHOLE fetched
     window (correct for "how does this look today", since "today" IS the
     end of the window). A historical trade can't know about bars that
     hadn't happened yet, so here it's computed using only bars up to and
     including the breakout itself.
  2. FRESHNESS TERM DROPPED — the live score also rewards a breakout for
     being close to "now" (`max(0, 5 - bars_since_breakout) * 2`). That's a
     live-dashboard-only concept ("is this worth showing today") with no
     backtest analog, so it's simply omitted here rather than papering over
     it with a fake definition of "now" for a past trade.
  3. LIVE-FRESHNESS FILTERS DON'T APPLY — the live scanner's
     breakout_lookback ("did this break out recently") and
     extension_from_breakout_pct ("don't chase a move that's already run")
     filters exist only to keep the LIVE dashboard from showing stale or
     already-extended setups. A backtest trade is entered exactly at its
     own breakout bar regardless of how far price later ran, so neither
     filter is applied here. The uptrend, volume, touch-count, box-age, and
     min-score gates ARE all still applied, identically.
  4. TARGET IS A BACKTEST-ONLY ADDITION — the live scanner has no fixed
     target at all (DarvaX is meant to be ridden/pyramided, not sold at a
     fixed level), so nothing here can be "reused verbatim" for an exit
     price. This backtest uses the standard technical-analysis "measured
     move" convention: target = breakout_close + (box_top - box_bottom),
     i.e. the box's own height projected up from the breakout. This is a
     disclosed simplification, not a claim about how anyone actually trades
     DarvaX setups.
  5. ENTRY / STOP — entry = the breakout day's close (exactly what the live
     scanner calls `breakout_close`, so no extra assumption is introduced).
     Stop = box_bottom, Darvas's own literal stop-loss rule.

Simulation: starting the day AFTER the breakout (already filled at that
day's close), each day checks stop first (low <= box_bottom, conservative
same-day-ambiguity convention - can't know the true intrabar sequence from
OHLC alone) then target (high >= measured-move target). Time stop at
config.MAX_HOLDING_DAYS sessions, exit at that day's close.

Universe / window: reads every cached `cache/daily_*.parquet` file (however
it got cached - by any scanner tab), same as ep_backtest.py, and restricts
trade ENTRY (breakout) dates to config.refresh_backtest_window()'s trailing
BACKTEST_MONTHS (6) window, using each symbol's full history for box
detection so an entry early in the window can still reference a box that
started forming earlier.
"""
import glob
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import indicators as ind
from darvax import find_darvas_boxes, _timeframe_params, _count_box_touches

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def _score(vol_ratio: float, box_height_pct: float, dist_from_ath_pct: float, touch_count: int) -> float:
    """Same weighted formula as darvax.py's live score, minus the
    freshness term (see module docstring, point 2)."""
    score = (
        min(vol_ratio, 6.0) * 8
        + max(0, 15 - box_height_pct) * 2
        + max(0, 10 - dist_from_ath_pct) * 3
        + min(touch_count, 8) * 3
    )
    return round(max(0.0, min(100.0, score)), 1)


def find_all_darvax_trades(symbol: str, daily: pd.DataFrame, timeframe: str = "daily") -> list:
    """Mirrors evaluate_symbol_darvax's gates, EXCEPT it considers every
    completed box+breakout in the symbol's full history, not just the most
    recent one, and fixes the lookahead issue described in the module
    docstring. Every gate that has a real backtest analog (uptrend, volume,
    box-age, touch-count, score) is the exact same threshold/config the
    live scanner uses."""
    params = _timeframe_params(timeframe)
    if daily is None or daily.empty or len(daily) < params["min_bars"]:
        return []

    df = daily.reset_index(drop=True)
    n = len(df)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    vol = df["volume"].astype(float)

    uptrend_ema = ind.ema(close, params["uptrend_ema_period"])
    vol_sma = vol.rolling(params["volume_sma_bars"]).mean()
    # Running all-time-high as of each bar (causal - never looks forward),
    # used for dist_from_ath_pct so a historical trade only ever "knows"
    # about highs that had actually happened by its own breakout day.
    running_high = high.cummax()

    boxes = find_darvas_boxes(df, confirm_bars=params["confirm_bars"])
    trades = []

    for box in boxes:
        top_idx = box["box_top_idx"]
        breakout_idx = box["breakout_idx"]
        box_age_bars = breakout_idx - top_idx
        if box_age_bars > params["max_box_age"]:
            continue

        lookback_idx = max(0, top_idx - params["uptrend_lookback_bars"])
        if top_idx < params["uptrend_ema_period"] or pd.isna(uptrend_ema.iloc[top_idx]) or pd.isna(uptrend_ema.iloc[lookback_idx]):
            continue
        in_uptrend = (
            close.iloc[top_idx] > uptrend_ema.iloc[top_idx]
            and uptrend_ema.iloc[top_idx] > uptrend_ema.iloc[lookback_idx]
        )
        if not in_uptrend:
            continue

        avg_vol = vol_sma.iloc[breakout_idx - 1] if breakout_idx > 0 else None
        if avg_vol is None or pd.isna(avg_vol) or avg_vol <= 0:
            continue
        vol_ratio = float(vol.iloc[breakout_idx] / avg_vol)
        if vol_ratio < params["volume_mult"]:
            continue

        box_top = float(box["box_top"])
        box_bottom = float(box["box_bottom"])
        touch_count = _count_box_touches(df, box_top, box_bottom, top_idx, breakout_idx, params["touch_band_pct"])
        if touch_count < params["min_touches"]:
            continue

        box_height_pct = round((box_top - box_bottom) / box_bottom * 100.0, 2)
        window_high = float(running_high.iloc[breakout_idx])
        entry = float(box["breakout_close"])
        dist_from_ath_pct = round((window_high - entry) / window_high * 100.0, 2) if window_high else 0.0

        score = _score(vol_ratio, box_height_pct, dist_from_ath_pct, touch_count)
        if score < params["min_score"]:
            continue

        risk = entry - box_bottom
        if risk <= 0:
            continue
        stop_pct = round(risk / entry * 100, 2)
        target = round(entry + (box_top - box_bottom), 2)  # measured move (see docstring, point 4)
        reward = target - entry
        rr = round(reward / risk, 2) if risk > 0 else 0.0

        trades.append({
            "symbol": symbol,
            "box_top_date": str(pd.to_datetime(df["date"].iloc[top_idx]).date()),
            "box_bottom_date": str(pd.to_datetime(df["date"].iloc[box["box_bottom_idx"]]).date()),
            "breakout_date": str(pd.to_datetime(df["date"].iloc[breakout_idx]).date()),
            "breakout_index": breakout_idx,
            "box_top": round(box_top, 2),
            "box_bottom": round(box_bottom, 2),
            "box_height_pct": box_height_pct,
            "box_age_bars": box_age_bars,
            "touch_count": touch_count,
            "volume_ratio": round(vol_ratio, 2),
            "dist_from_ath_pct": dist_from_ath_pct,
            "entry_price": round(entry, 2),
            "stop_loss": round(box_bottom, 2),
            "stop_pct": stop_pct,
            "target": target,
            "risk_reward": rr,
            "score": score,
        })

    return trades


def simulate_exit(df: pd.DataFrame, breakout_index: int, stop: float, target: float) -> dict:
    """Walk forward from the day AFTER the breakout (already filled at the
    breakout day's own close). Stop checked before target each day
    (conservative same-day-ambiguity convention). Time stop at
    config.MAX_HOLDING_DAYS sessions -> exit at that day's close."""
    n = len(df)
    start = breakout_index + 1
    last_j = min(n - 1, breakout_index + config.MAX_HOLDING_DAYS)
    for j in range(start, last_j + 1):
        row = df.iloc[j]
        low = float(row["low"])
        high = float(row["high"])
        holding_days = j - breakout_index
        if low <= stop:
            return {
                "exit_date": str(pd.to_datetime(row["date"]).date()),
                "exit_price": round(stop, 2),
                "exit_reason": "STOP",
                "holding_days": holding_days,
            }
        if high >= target:
            return {
                "exit_date": str(pd.to_datetime(row["date"]).date()),
                "exit_price": round(target, 2),
                "exit_reason": "TARGET",
                "holding_days": holding_days,
            }
    row = df.iloc[last_j]
    reason = "TIME_STOP" if (last_j - breakout_index) >= config.MAX_HOLDING_DAYS else "END_OF_DATA"
    return {
        "exit_date": str(pd.to_datetime(row["date"]).date()),
        "exit_price": round(float(row["close"]), 2),
        "exit_reason": reason,
        "holding_days": last_j - breakout_index,
    }


RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.RESULTS_DIR)
TRADES_CSV_PATH = os.path.join(RESULTS_DIR, "darvax_backtest_trades.csv")


def run_backtest(timeframe: str = "daily", verbose: bool = True, save_csv: bool = True) -> pd.DataFrame:
    """Full historical enumeration + simulation, reading every cached daily
    file. Used by both the CLI (`python darvax_backtest.py`) and the
    dashboard's "Run Backtest" button (app.py's /api/darvax/backtest)."""
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
            trades = find_all_darvax_trades(symbol, daily, timeframe=timeframe)
        except Exception as e:
            if verbose:
                print(f"  [warn] {symbol}: eval failed ({e})")
            continue

        for t in trades:
            entry_date = pd.to_datetime(t["breakout_date"]).date()
            if not (bt_start <= entry_date <= bt_end):
                continue
            exit_info = simulate_exit(daily, t["breakout_index"], t["stop_loss"], t["target"])
            pnl_pct = round((exit_info["exit_price"] - t["entry_price"]) / t["entry_price"] * 100, 2)
            all_trades.append({**t, **exit_info, "pnl_pct": pnl_pct})

    if verbose:
        print(f"Scanned {scanned} symbols, found {len(all_trades)} trades entered within the backtest window")

    out_df = pd.DataFrame(all_trades)

    # De-duplicate: the same completed box can be re-detected via slightly
    # different bar windows in different cache files (rare, but matches
    # ep_backtest.py's own defensive de-dup). One breakout = one trade.
    before = len(out_df)
    if not out_df.empty:
        out_df = out_df.sort_values("breakout_date").drop_duplicates(
            subset=["symbol", "breakout_date", "entry_price", "stop_loss"],
            keep="last",
        ).reset_index(drop=True)
    if verbose:
        print(f"De-duplicated {before - len(out_df)} duplicate trade(s) -> {len(out_df)} unique trades")

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
