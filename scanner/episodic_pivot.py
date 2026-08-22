"""
Episodic Pivot (delayed EP) scanner — Ankur Patel's method, as explained on
the "Master In One" podcast (book: Swing Trading Simplified). Original idea
traced to Pradeep Bonde, via Frank Cappiello's "Finding the Next Superstock".

Core logic, translated from the podcast into rules:

  1. NEGLECTED STOCK — the stock did nothing (no big prior move) in the
     EP_NEGLECT_LOOKBACK_DAYS before the trigger day. If it already ran up
     before the news, the news wasn't a surprise to the market and it's not
     an EP ("surprise is the key word").

  2. DAY-0 REACTION — a single day with (a) a big % move up, (b) unusually
     heavy volume vs its own 50-day average ("R_Vol"), and (c) a strong
     close (not a doji / faded day) — a company whose story just changed
     (results, new order, policy change, promoter change, black-swan news)
     and nobody — retail or funds — was positioned for it yet.

  3. DELAYED ENTRY — Indian circuit filters mean you can't safely buy the
     reaction candle itself (unmeasurable risk/no way to size a stop). So
     we WAIT for a tight, small-range ("range contraction") pullback candle
     that forms after day-0, ideally drifting back toward the 10 EMA. The
     HIGH of that candle is the entry trigger (GTT); its LOW is the stop.
     A fresh, un-broken tight candle = WATCHING. Its high just got taken
     out (recently) = TRIGGERED.

  4. LOW RISK, NOT A FIXED R MULTIPLE — the GATING target is a genuine
     technical projection (the day-0 candle's own range/"flagpole",
     projected up from entry — the "trade it like a flag" comment in the
     podcast), so risk_reward here is a real, independently-computed ratio,
     not display dressing. Setups that don't clear EP_MIN_RR or blow through
     EP_MAX_STOP_PCT are dropped, not shown with a decorative number.
     Ankur's own literal exit rule — a flat 1:3 (book ~50%, trail the rest
     below the 20 EMA) — is surfaced separately as `target_1_3` for
     reference/alerts, but deliberately does NOT gate what appears here.

State machine per symbol, mirroring swing_trade.py's WATCHING/TRIGGERED
pattern:
  - Find the most recent Day-0 event within EP_LOOKFORWARD_DAYS.
  - Walk forward from there tracking the latest un-broken tight candle.
  - If its high was broken recently (<= EP_TRIGGER_MAX_STALE_DAYS ago) and
    the breakout day didn't gap past EP_MAX_CHASE_GAP_PCT above it (the
    podcast's "if it opens 3-5%+ higher, I don't buy, I'll get another
    chance" rule) => TRIGGERED.
  - Else if a tight candle is currently active, un-broken => WATCHING (GTT
    candidate at its high).
  - Else (day-0 fresh, no pullback candle yet) => WATCHING with day-0 info
    only, no entry/stop until a tight candle actually forms.
"""
from datetime import date, timedelta

import pandas as pd

import config
import indicators as ind


def tradingview_chart_url(symbol: str) -> str:
    return f"https://in.tradingview.com/symbols/NSE-{symbol}/"


def _augment(daily: pd.DataFrame) -> pd.DataFrame:
    """daily: columns [date, open, high, low, close, volume], sorted ascending.
    Returns the same df with every column needed to detect day-0 events and
    tight-range pivot candles."""
    df = daily.copy().reset_index(drop=True)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"].astype(float)

    # "Neglected before day-0": the stock's own move in the window BEFORE
    # today (never including today) must be small — both net move and the
    # highest point reached in that window, so a stock that spiked then
    # cooled off net-flat still gets correctly excluded.
    n_look = config.EP_NEGLECT_LOOKBACK_DAYS
    prior_ref = close.shift(1 + n_look)
    prior_net_move_pct = (close.shift(1) - prior_ref) / prior_ref * 100
    prior_peak_move_pct = (close.shift(1).rolling(n_look).max() - prior_ref) / prior_ref * 100
    df["neglected"] = (prior_net_move_pct.abs() <= config.EP_NEGLECT_MAX_PRIOR_MOVE_PCT) & \
                       (prior_peak_move_pct <= config.EP_NEGLECT_MAX_PRIOR_MOVE_PCT)

    # Day-0 candidate flags.
    df["day0_move_pct"] = close.pct_change() * 100
    vol_avg = vol.shift(1).rolling(config.EP_RVOL_AVG_PERIOD).mean()
    df["rvol_pct"] = (vol / vol_avg * 100).where(vol_avg > 0)
    rng = (high - low)
    df["close_strength"] = ((close - low) / rng.replace(0, pd.NA)).fillna(1.0).clip(0, 1)

    df["day0_flag"] = (
        (df["day0_move_pct"] >= config.EP_MIN_DAY0_MOVE_PCT)
        & (df["rvol_pct"] >= config.EP_MIN_RVOL_MULT * 100)
        & (df["close_strength"] >= config.EP_MIN_CLOSE_STRENGTH)
        & df["neglected"]
    )

    # Tight/range-contraction candle flags (for the post-day0 pullback).
    df["ema10"] = ind.ema(close, 10)
    df["dist_from_ema10_pct"] = (close - df["ema10"]).abs() / df["ema10"] * 100
    atr14 = ind.atr(high, low, close, 14)
    tight_range_pct = rng / close * 100
    df["tight_range_pct"] = tight_range_pct
    df["tight_flag"] = (tight_range_pct <= config.EP_TIGHT_RANGE_MAX_PCT) | \
                        (rng <= config.EP_TIGHT_RANGE_MAX_ATR_MULT * atr14)

    return df


def _score(day0_move_pct, rvol_pct, close_strength, tight_range_pct=None, dist_from_ema10_pct=None) -> float:
    """0-100 composite quality score. Volume shock and day-0 move size carry
    the most weight (that's the entire premise of an EP); tightness and
    proximity to EMA10 only apply once a pullback candle actually exists —
    a fresh day-0 with no pivot candle yet scores lower until one forms."""
    s = 0.0
    s += 35 * min(1.0, (rvol_pct or 0) / 1500.0)
    s += 20 * min(1.0, (day0_move_pct or 0) / 20.0)
    s += 15 * max(0.0, min(1.0, close_strength or 0))
    if tight_range_pct is not None:
        s += 15 * max(0.0, 1 - min(1.0, tight_range_pct / config.EP_TIGHT_RANGE_MAX_PCT))
    if dist_from_ema10_pct is not None:
        s += 15 * max(0.0, 1 - min(1.0, dist_from_ema10_pct / config.EP_TIGHT_RANGE_NEAR_EMA_PCT))
    return round(min(100.0, s), 1)


def evaluate_symbol_episodic_pivot(symbol: str, daily: pd.DataFrame) -> "dict | None":
    min_bars = config.EP_RVOL_AVG_PERIOD + config.EP_NEGLECT_LOOKBACK_DAYS + config.EP_LOOKFORWARD_DAYS + 20
    if daily is None or daily.empty or len(daily) < min_bars:
        return None

    df = _augment(daily)
    n = len(df)
    last_idx = n - 1

    cutoff = max(0, last_idx - config.EP_LOOKFORWARD_DAYS)
    day0_candidates = df.index[df["day0_flag"] & (df.index >= cutoff) & (df.index <= last_idx)]
    if len(day0_candidates) == 0:
        return None
    i0 = int(day0_candidates.max())
    day0 = df.iloc[i0]
    day0_range = float(day0["high"] - day0["low"])

    # Walk forward from day-0, tracking the latest un-broken tight candle
    # and the most recent valid (non-chased) breakout of one.
    active_tight = None
    last_breakout = None
    for j in range(i0 + 1, n):
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
                    last_breakout = {**active_tight, "breakout_index": j, "breakout_date": row["date"]}
                active_tight = None
            elif float(row["close"]) < active_tight["low"]:
                active_tight = None  # base failed before it ever broke out

    current_price = float(df.iloc[last_idx]["close"])
    days_since_day0 = last_idx - i0

    base = {
        "symbol": symbol,
        "day0_date": str(pd.to_datetime(day0["date"]).date()),
        "day0_move_pct": round(float(day0["day0_move_pct"]), 2),
        "day0_rvol_pct": round(float(day0["rvol_pct"]), 0),
        "day0_close_strength": round(float(day0["close_strength"]), 2),
        "days_since_day0": days_since_day0,
        "current_price": round(current_price, 2),
        "tv_chart_url": tradingview_chart_url(symbol),
    }

    entry = stop = target = rr = stop_pct = target_1_3 = None
    pivot_date = None
    tight_range_pct = dist_from_ema10_pct = None
    status = "WATCHING"
    days_since_trigger = None

    if last_breakout is not None and (last_idx - last_breakout["breakout_index"]) <= config.EP_TRIGGER_MAX_STALE_DAYS:
        status = "TRIGGERED"
        pivot_date = str(pd.to_datetime(last_breakout["date"]).date())
        entry = last_breakout["high"]
        stop = last_breakout["low"]
        tight_range_pct = last_breakout["tight_range_pct"]
        dist_from_ema10_pct = last_breakout["dist_from_ema10_pct"]
        days_since_trigger = last_idx - last_breakout["breakout_index"]
    elif active_tight is not None:
        status = "WATCHING"
        pivot_date = str(pd.to_datetime(active_tight["date"]).date())
        entry = active_tight["high"]
        stop = active_tight["low"]
        tight_range_pct = active_tight["tight_range_pct"]
        dist_from_ema10_pct = active_tight["dist_from_ema10_pct"]

    if entry is not None and stop is not None:
        risk = entry - stop
        if risk <= 0:
            return None
        stop_pct = round(risk / entry * 100, 2)
        if stop_pct > config.EP_MAX_STOP_PCT:
            return None
        target = round(entry + config.EP_TARGET_FLAGPOLE_MULT * day0_range, 2)
        reward = target - entry
        rr = round(reward / risk, 2) if risk > 0 else 0.0
        if rr < config.EP_MIN_RR:
            return None
        # Informational only (does not gate the setup): Ankur's own literal
        # exit convention — book ~50% at 1:3, trail the rest below the
        # EP_TRAIL_EMA_PERIOD_INFO-period EMA.
        target_1_3 = round(entry + config.EP_TARGET_R_MULT_INFO * risk, 2)

    score = _score(
        day0["day0_move_pct"], day0["rvol_pct"], day0["close_strength"],
        tight_range_pct=tight_range_pct, dist_from_ema10_pct=dist_from_ema10_pct,
    )
    if score < config.EP_MIN_SCORE:
        return None

    return {
        **base,
        "status": status,
        "pivot_date": pivot_date,
        "entry_price": round(entry, 2) if entry is not None else None,
        "stop_loss": round(stop, 2) if stop is not None else None,
        "target": target,
        "target_1_3": target_1_3,
        "trail_ema_period": config.EP_TRAIL_EMA_PERIOD_INFO if target_1_3 is not None else None,
        "risk_reward": rr,
        "stop_pct": stop_pct,
        "days_since_trigger": days_since_trigger,
        "score": score,
    }


def scan_episodic_pivot(kite_client, universe_df=None, universe_mode=None) -> dict:
    import universe as universe_mod
    from datetime import datetime
    from tqdm import tqdm

    try:
        mode = universe_mod.normalize_nifty_mode(universe_mode or config.EP_UNIVERSE or "nifty500")
    except ValueError:
        mode = "nifty500"

    try:
        scan_df = universe_mod.build_nifty_index_universe(kite_client, mode)
    except Exception as e:
        print(f"  [warn] episodic-pivot universe {mode} failed ({e}) — falling back")
        scan_df = universe_df

    label = universe_mod.nifty_mode_label(mode)
    if scan_df is None or scan_df.empty:
        return {
            "generated_at": datetime.now().isoformat(),
            "universe_mode": mode, "universe_label": label,
            "universe_size": 0, "scanned": 0, "num_results": 0, "results": [],
        }

    today = date.today()
    from_date = today - timedelta(days=config.EP_LOOKBACK_DAYS)
    results = []
    scanned = 0
    print(f"Episodic Pivot: scanning {label} ({len(scan_df)} symbols)...")

    for _, row in tqdm(scan_df.iterrows(), total=len(scan_df), desc=f"Episodic Pivot ({label})"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        scanned += 1
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, today)
            hit = evaluate_symbol_episodic_pivot(symbol, daily)
            if hit:
                results.append(hit)
        except Exception as e:
            print(f"  [warn] episodic-pivot skipped {symbol}: {e}")
            continue

    status_rank = {"TRIGGERED": 1, "WATCHING": 0}
    results.sort(
        key=lambda r: (status_rank.get(r.get("status"), 0), r.get("score") or 0),
        reverse=True,
    )
    print(f"Episodic Pivot: {len(results)} hit(s) of {scanned} scanned ({label})")
    return {
        "generated_at": datetime.now().isoformat(),
        "universe_mode": mode, "universe_label": label,
        "universe_size": len(scan_df), "scanned": scanned,
        "num_results": len(results), "results": results,
    }
