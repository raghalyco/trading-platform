"""
TradeXPavan "DHB Setup" - intraday momentum-pullback-breakout strategy,
per the reference PDF:

  1. Selection (9:15-10:00): top gainer, high volume, strong momentum,
     clean price action. Locks the "First Day High" (FDH) = the high made
     during this window. No new stock selected after 10:00.
  2. Pullback: price must retrace from FDH before anything else - a
     breakout with no prior pullback is explicitly rejected (never chase).
  3. Confirmation: a bullish reversal candle (engulfing / hammer / strong
     rejection) forms at the pullback low.
  4. Entry: only after confirmation, on a volume-backed break back above FDH.
  5. Stop-loss: the swing low formed during the pullback+confirmation
     phase - never widened.
  6. Targets: T1 at 1:2 R:R (book half), T2 at 1:3 R:R (trail, hold rest).

This module is pure detection + backtest logic - deliberately NOT wired
into any live monitor yet. Validate against real Kite intraday history
first (run_dhb_backtest), review the numbers, THEN decide whether to wire
it into intraday_engine.py as a live mode.

ASSUMPTIONS made where the PDF doesn't fully specify a rule (flagged in
the backtest report's `assumptions` field too):
  - Treated as INTRADAY-only: any position still open by 15:20 is closed
    at that bar's price (square-off), since the setup is keyed off a
    single day's FDH/opening range with no overnight-hold rule stated.
  - Entry fill = the close of the breakout bar (not the FDH price itself),
    a more realistic backtest fill than assuming a perfect stop order.
  - T1/T2 booking split 50/50 (PDF says "book partial" / "hold remaining"
    without an exact split).
  - "Top gainer" / "high volume" filtered via a minimum %-gain-by-10:00
    threshold vs prior close (MIN_GAIN_PCT) - no cross-sectional ranking
    since this runs against a fixed watchlist, not the whole exchange.
"""
from __future__ import annotations

from datetime import datetime, timedelta, time as dtime
from typing import Optional

import pandas as pd

# Fixed watchlist for the first validation pass - liquid, historically
# momentum/gap-prone NSE names likely to actually produce "top gainer"
# days worth testing against, rather than a full-universe scan. Expanded
# from the original 8 after the first pass only found 5 trades in 30 days
# - too small a sample to read anything into.
DEFAULT_WATCHLIST = [
    "ADANIENT", "TATASTEEL", "JSWSTEEL", "VEDL",
    "SUZLON", "RVNL", "PNB", "IDEA",
    "NATIONALUM", "HINDALCO", "TATAPOWER", "SAIL",
    "BANKINDIA", "IOC", "GAIL", "ONGC",
    "NHPC", "NMDC", "IREDA", "IRFC",
    "YESBANK", "JIOFIN", "PAYTM", "BANKBARODA",
]

SELECTION_END = dtime(10, 0)
SQUARE_OFF_TIME = dtime(15, 20)
MIN_GAIN_PCT = 2.0          # default; run_dhb_backtest can override per-run
PULLBACK_MIN_PCT = 0.3      # min retracement from FDH to count as a real pullback
BREAKOUT_VOLUME_MULT = 1.2  # breakout bar volume vs recent average


def _is_bullish_engulfing(prev_o, prev_c, o, c) -> bool:
    return prev_c < prev_o and c > o and c >= prev_o and o <= prev_c


def _is_hammer(o, h, l, c) -> bool:
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return False
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return lower_wick >= body * 2 and upper_wick <= body * 0.5 and c >= o


def _is_strong_rejection(o, h, l, c) -> bool:
    """Long lower wick, closes in the top third of the bar's range,
    bullish - a rejection of lower prices without the strict hammer body/
    wick ratio."""
    rng = h - l
    if rng <= 0:
        return False
    close_pos = (c - l) / rng
    return close_pos >= 0.7 and c >= o


def _is_confirmation_candle(prev_row, row) -> Optional[str]:
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    po, pc = float(prev_row["open"]), float(prev_row["close"])
    if _is_bullish_engulfing(po, pc, o, c):
        return "Bullish Engulfing"
    if _is_hammer(o, h, l, c):
        return "Hammer"
    if _is_strong_rejection(o, h, l, c):
        return "Strong Rejection"
    return None


def detect_dhb_setup(day_df: pd.DataFrame, prev_close: float,
                      min_gain_pct: float = MIN_GAIN_PCT,
                      pullback_min_pct: float = PULLBACK_MIN_PCT,
                      breakout_volume_mult: float = BREAKOUT_VOLUME_MULT) -> Optional[dict]:
    """
    day_df: ONE trading day's 1-minute OHLCV, 9:15-15:30, sorted ascending.
    prev_close: previous trading day's close (for the %-gainer check).

    Returns a dict describing the trade (entry/SL/targets/basis) if the
    full sequence (selection -> pullback -> confirmation -> breakout)
    completes that day, else None (no signal - most days, for most
    stocks, correctly produce nothing here).
    """
    if day_df.empty or prev_close is None or prev_close <= 0:
        return None
    df = day_df.reset_index(drop=True)
    df["_time"] = pd.to_datetime(df["date"]).dt.time

    selection = df[df["_time"] <= SELECTION_END]
    if len(selection) < 10:
        return None

    fdh = float(selection["high"].max())
    gain_pct = (float(selection["close"].iloc[-1]) - prev_close) / prev_close * 100.0
    if gain_pct < min_gain_pct:
        return None  # not a "top gainer" day for this stock

    rest = df[df["_time"] > SELECTION_END].reset_index(drop=True)
    if rest.empty:
        return None

    avg_vol_20 = df["volume"].rolling(20, min_periods=5).mean()

    # Phase 1: pullback - track the running low since 10:00 until it's
    # retraced enough from FDH to count as a genuine pullback.
    pullback_low = None
    pullback_start_i = None
    for i in range(len(rest)):
        low_i = float(rest["low"].iloc[i])
        if pullback_low is None or low_i < pullback_low:
            pullback_low = low_i
            pullback_start_i = i
        retrace_pct = (fdh - pullback_low) / fdh * 100.0
        if retrace_pct >= pullback_min_pct:
            break
    else:
        return None  # never pulled back enough - "never chase the breakout"

    pullback_locked_i = i  # index in `rest` where pullback qualified

    # Phase 2: confirmation candle, searched from the pullback bar onward.
    confirmation_i = None
    confirmation_pattern = None
    for j in range(max(1, pullback_locked_i), len(rest)):
        pattern = _is_confirmation_candle(rest.iloc[j - 1], rest.iloc[j])
        if pattern:
            confirmation_i = j
            confirmation_pattern = pattern
            break
    if confirmation_i is None:
        return None  # pulled back but never showed a reversal candle

    swing_low = float(rest["low"].iloc[pullback_start_i:confirmation_i + 1].min())

    # Phase 3: breakout back above FDH, volume-backed, after confirmation.
    entry_i = None
    for k in range(confirmation_i + 1, len(rest)):
        row = rest.iloc[k]
        high_k, close_k, open_k = float(row["high"]), float(row["close"]), float(row["open"])
        if high_k < fdh:
            continue
        vol_k = float(row["volume"])
        # position in the FULL day for the rolling-average lookup
        full_idx = len(selection) + k
        avg_vol = avg_vol_20.iloc[full_idx] if full_idx < len(avg_vol_20) else None
        vol_ok = pd.isna(avg_vol) or avg_vol <= 0 or vol_k >= avg_vol * breakout_volume_mult
        if close_k >= open_k and vol_ok:
            entry_i = k
            break
    if entry_i is None:
        return None

    entry_row = rest.iloc[entry_i]
    entry_price = float(entry_row["close"])
    entry_time = pd.to_datetime(entry_row["date"])
    risk = entry_price - swing_low
    if risk <= 0:
        return None  # SL would be above/at entry - not a valid setup

    target1 = round(entry_price + 2 * risk, 2)
    target2 = round(entry_price + 3 * risk, 2)

    return {
        "fdh": round(fdh, 2),
        "gain_pct_at_1000": round(gain_pct, 2),
        "pullback_low": round(pullback_low, 2),
        "confirmation_pattern": confirmation_pattern,
        "confirmation_time": str(pd.to_datetime(rest.iloc[confirmation_i]["date"])),
        "swing_low": round(swing_low, 2),
        "entry_price": entry_price,
        "entry_time": str(entry_time),
        "entry_bar_index_in_rest": entry_i,
        "stop_loss": round(swing_low, 2),
        "target1": target1,
        "target2": target2,
        "risk_points": round(risk, 2),
    }


def _simulate_dhb_outcome(rest: pd.DataFrame, setup: dict) -> dict:
    """Walks forward from the entry bar to day close (or 15:20 square-off),
    T1-before-SL race with a 50/50 booking split at T1, matching the PDF's
    "book partial, trail, hold remaining" instruction."""
    entry_i = setup["entry_bar_index_in_rest"]
    entry = setup["entry_price"]
    sl = setup["stop_loss"]
    t1 = setup["target1"]
    t2 = setup["target2"]
    risk = setup["risk_points"]

    t1_hit = False
    future = rest.iloc[entry_i + 1:]
    for _, row in future.iterrows():
        t = pd.to_datetime(row["date"])
        if t.time() >= SQUARE_OFF_TIME:
            break
        low_k, high_k = float(row["low"]), float(row["high"])
        if not t1_hit:
            if low_k <= sl:
                return {"result": "SL_HIT", "r_multiple": -1.0, "exit_time": str(t), "exit_price": sl}
            if high_k >= t1:
                t1_hit = True
                if high_k >= t2:
                    # same bar cleared T1 and T2 - treat as full T2 win
                    return {"result": "T2_HIT", "r_multiple": 2.5, "exit_time": str(t), "exit_price": t2}
        else:
            if high_k >= t2:
                return {"result": "T2_HIT", "r_multiple": 2.5, "exit_time": str(t), "exit_price": t2}
            if low_k <= entry:
                # trailing-stop simplification: once T1 is banked, protect
                # the remaining half at breakeven (entry) rather than the
                # original SL - a common, conservative trail rule.
                r = 0.5 * 2.0 + 0.5 * 0.0
                return {"result": "T1_HIT_BE", "r_multiple": r, "exit_time": str(t), "exit_price": entry}

    # Square-off / day end without a clean T2 or breakeven-stop exit
    last_row = future[future["date"].apply(lambda d: pd.to_datetime(d).time() < SQUARE_OFF_TIME)]
    last_close = float(last_row.iloc[-1]["close"]) if len(last_row) else entry
    if t1_hit:
        r = 0.5 * 2.0 + 0.5 * ((last_close - entry) / risk)
        return {"result": "T1_HIT_EOD", "r_multiple": round(r, 2), "exit_time": "square-off", "exit_price": last_close}
    r = (last_close - entry) / risk
    return {"result": "EOD_FLAT", "r_multiple": round(r, 2), "exit_time": "square-off", "exit_price": last_close}


def fetch_dhb_history(kite_client, watchlist: Optional[list] = None, days: int = 30) -> dict[str, pd.DataFrame]:
    """Fetches real Kite 1-minute history per symbol ONCE. Separated from
    evaluate_dhb_history() so a parameter sweep (different min_gain_pct /
    pullback / volume thresholds) can re-run detection many times against
    the same in-memory data instead of re-hitting Kite's API per config -
    24 symbols x 58 days is expensive enough to fetch once.

    days: capped at 58 (+padding stays under Kite's 60-day limit for the
    'minute' interval - a single historical_data() call fails outright
    past that, it doesn't silently truncate)."""
    symbols = watchlist or DEFAULT_WATCHLIST
    days = min(days, 58)
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days)

    data: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        instrument = kite_client.kite.ltp([f"NSE:{symbol}"]).get(f"NSE:{symbol}")
        if not instrument:
            print(f"  [warn] dhb: couldn't resolve {symbol}, skipping")
            continue
        token = instrument["instrument_token"]
        df = kite_client.get_history(token, symbol, "minute", from_dt, to_dt)
        if df.empty:
            print(f"  [warn] dhb: no 1-min history for {symbol}")
            continue
        df["_date_only"] = pd.to_datetime(df["date"]).dt.date
        data[symbol] = df
    return data


def evaluate_dhb_history(data: dict[str, pd.DataFrame],
                          min_gain_pct: float = MIN_GAIN_PCT,
                          pullback_min_pct: float = PULLBACK_MIN_PCT,
                          breakout_volume_mult: float = BREAKOUT_VOLUME_MULT) -> dict:
    """Runs DHB detection+simulation against already-fetched history (see
    fetch_dhb_history) for one specific threshold combination. Returns the
    same aggregate report shape as run_dhb_backtest."""
    trades = []
    scanned_days = 0

    for symbol, df in data.items():
        trading_days = sorted(df["_date_only"].unique())
        for idx, d in enumerate(trading_days):
            if idx == 0:
                continue  # need a prior day's close
            prev_day = trading_days[idx - 1]
            prev_day_df = df[df["_date_only"] == prev_day]
            if prev_day_df.empty:
                continue
            prev_close = float(prev_day_df.iloc[-1]["close"])
            day_df = df[df["_date_only"] == d]
            scanned_days += 1

            setup = detect_dhb_setup(
                day_df, prev_close, min_gain_pct=min_gain_pct,
                pullback_min_pct=pullback_min_pct, breakout_volume_mult=breakout_volume_mult,
            )
            if setup is None:
                continue

            day_df_reset = day_df.reset_index(drop=True)
            day_df_reset["_time"] = pd.to_datetime(day_df_reset["date"]).dt.time
            rest = day_df_reset[day_df_reset["_time"] > SELECTION_END].reset_index(drop=True)
            outcome = _simulate_dhb_outcome(rest, setup)

            trades.append({
                "symbol": symbol, "date": str(d),
                **setup, **outcome,
            })

    wins = sum(1 for t in trades if t["r_multiple"] > 0)
    losses = sum(1 for t in trades if t["r_multiple"] <= 0)
    win_rate = round(100.0 * wins / len(trades), 1) if trades else None
    avg_r = round(sum(t["r_multiple"] for t in trades) / len(trades), 2) if trades else None
    total_r = round(sum(t["r_multiple"] for t in trades), 2) if trades else None

    return {
        "generated_at": datetime.now().isoformat(),
        "watchlist": list(data.keys()),
        "days_scanned": scanned_days,
        "min_gain_pct": min_gain_pct,
        "pullback_min_pct": pullback_min_pct,
        "breakout_volume_mult": breakout_volume_mult,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "avg_r_multiple": avg_r,
        "total_r_multiple": total_r,
        "sample_trades": trades[:100],
    }


def run_dhb_backtest(kite_client, watchlist: Optional[list] = None, days: int = 30,
                      min_gain_pct: float = MIN_GAIN_PCT) -> dict:
    """Convenience one-shot wrapper (fetch + evaluate) for a single config -
    what the earlier single-run calls used. For sweeping multiple
    threshold combinations, call fetch_dhb_history() once and
    evaluate_dhb_history() many times instead."""
    data = fetch_dhb_history(kite_client, watchlist=watchlist, days=days)
    report = evaluate_dhb_history(data, min_gain_pct=min_gain_pct)
    report["days_requested"] = min(days, 58)
    report["assumptions"] = [
        "Intraday-only: open positions squared off at 15:20 if T1/T2/SL not hit.",
        "Entry fill = close of the breakout bar, not the raw FDH price.",
        "T1/T2 booking split 50/50; after T1, remaining half trails to breakeven (not the original SL).",
        "'Top gainer' = up >= {}% by 10:00 vs prior close (fixed watchlist, no cross-sectional ranking).".format(min_gain_pct),
        "Fixed watchlist only - not yet validated against the full scanner universe.",
    ]
    return report
