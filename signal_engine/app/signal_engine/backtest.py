"""
Historical win/loss report for emitted SCALP / SMART TRADE / GBB levels.

Simulates OPTION PREMIUM P&L using the same Black-Scholes model and the
same mode-based target rule live auto-capture actually uses - GBB and
SCALP use a RATIO target (entry + (entry - sl) * rr_multiple, scaling
with that trade's actual stop distance), SMART TRADE uses the fixed
+target_premium_points rule - not raw index points. See run_backtest's
docstring for why this matters and what it still can't capture.

Honest limitations (read before trusting win_rate):
  - Premium is a Black-Scholes ESTIMATE (option_pricing.py), calibrated
    against real VIX history but not against live bid-ask quotes. Same
    known limitation as live trading: near-expiry premiums are likely
    under-estimated unless iv_multiplier is tuned (see option_pricing.py).
  - Multi-month runs use 5-minute bars (Kite 1m history is capped) while
    live SCALP is designed on 1-minute — coarser fills.
  - No slippage, spread, or gap modeling.
  - Entry premium priced off the signal bar's close; SL premium priced by
    re-pricing the SAME option at the index stop-loss level (same
    approach trade_recommendation.py uses live) - VIX/time-to-expiry held
    fixed at signal time, not re-estimated bar-by-bar.
  - Target is the live rule exactly, per mode: GBB/SCALP use a ratio off
    the actual premium stop distance (entry + (entry-sl)*rr_multiple);
    SMART TRADE uses entry_premium + target_premium_points (fixed points).
    Matches auto_trade.py's real gate.
  - Default scoring: T1 before SL = WIN, SL first = LOSS.
  - Optional mark_to_market: at hold end, close vs entry decides WIN/LOSS
    instead of TIMEOUT (more trades "decided", still approximate).
"""

from __future__ import annotations

import pandas as pd

from app.indicators.core import atr
from app.indicators.multi_tf import resample_ohlcv
from app.price_action.patterns import detect_pattern, pa_bonus_points
from app.signal_engine.scorer import score_components, compute_score, verdict_label
from app.signal_engine.smart_scorer import score_smart_trade, compute_smart_score
from app.signal_engine.modes import scalp_levels, smart_trade_levels, gbb_levels, expiry_date_iso_for
from app.signal_engine.gbb_setup import compute_gbb_signal
from app.signal_engine.option_pricing import estimate_premium
from app.signal_engine.regime import classify_regime
from app.signal_engine.trade_chart import IST, candles_to_list
from app.signal_engine.trade_recommendation import LOT_SIZES
from app.config import CONFIG


def _fmt_ist(ts) -> str | None:
    """Format a (possibly naive, assumed-IST) timestamp as "DD Mon 'YY HH:MM".
    Matches trade_chart._fmt_ist's convention — callers append "IST" for display."""
    if ts is None:
        return None
    try:
        t = pd.Timestamp(ts)
    except (ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    else:
        t = t.tz_convert(IST)
    return t.strftime("%d %b '%y %H:%M")


def _simulate_outcome(side: str, entry: float, t1: float, sl: float,
                      future_bars, max_bars: int,
                      mark_to_market: bool = False) -> dict:
    """
    Mark WIN if T1 is touched before SL; LOSS if SL first; TIMEOUT otherwise
    (or MTM WIN/LOSS at hold end if mark_to_market=True).
    """
    bars = future_bars.head(max_bars).reset_index(drop=True)
    if len(bars) == 0:
        return {"result": "TIMEOUT", "bars_held": 0, "reason": "no_future_bars",
                "exit_price": entry, "pnl_points": 0.0, "exit_ts": None}

    for offset, row in bars.iterrows():
        high, low = float(row["high"]), float(row["low"])
        if side == "CE":
            hit_sl = low <= sl
            hit_t1 = high >= t1
        else:
            hit_sl = high >= sl
            hit_t1 = low <= t1

        held = int(offset) + 1
        if hit_sl and hit_t1:
            return {
                "result": "LOSS", "bars_held": held, "reason": "same_bar_sl_and_t1",
                "exit_price": sl, "pnl_points": -abs(entry - sl),
                "exit_ts": row["timestamp"],
            }
        if hit_sl:
            return {
                "result": "LOSS", "bars_held": held, "reason": "sl_hit",
                "exit_price": sl, "pnl_points": -abs(entry - sl),
                "exit_ts": row["timestamp"],
            }
        if hit_t1:
            return {
                "result": "WIN", "bars_held": held, "reason": "t1_hit",
                "exit_price": t1, "pnl_points": abs(t1 - entry),
                "exit_ts": row["timestamp"],
            }

    last_close = float(bars.iloc[-1]["close"])
    last_ts = bars.iloc[-1]["timestamp"]
    if side == "CE":
        pnl = last_close - entry
    else:
        pnl = entry - last_close

    if mark_to_market:
        result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
        return {
            "result": result,
            "bars_held": len(bars),
            "reason": "mark_to_market_at_hold_end",
            "exit_price": last_close,
            "pnl_points": round(pnl, 2),
            "exit_ts": last_ts,
        }

    return {
        "result": "TIMEOUT",
        "bars_held": len(bars),
        "reason": "max_hold_reached",
        "exit_price": last_close,
        "pnl_points": round(pnl, 2),
        "exit_ts": last_ts,
    }


def _atm_strike(spot: float) -> int:
    return int(round(spot / 50) * 50)


def _premium_at(spot: float, strike: int, side: str, vix_pct: float,
                 expiry_iso: str | None, as_of, iv_multiplier: float) -> float | None:
    """Thin wrapper around option_pricing.estimate_premium with the
    error/edge cases (no expiry, already-expired, bad VIX) turned into a
    clean None instead of an exception, since the backtest walks through
    hundreds of arbitrary historical instants."""
    if expiry_iso is None or vix_pct is None or vix_pct <= 0:
        return None
    try:
        now = pd.Timestamp(as_of)
        if now.tzinfo is None:
            now = now.tz_localize(IST)
        return estimate_premium(
            spot=spot, strike=strike, side=side, vix_pct=float(vix_pct),
            expiry_date_iso=expiry_iso, now=now.to_pydatetime(),
            iv_multiplier=iv_multiplier,
        )
    except Exception:
        return None


def _simulate_premium_outcome(side: str, entry_premium: float, t1_premium: float,
                               sl_premium: float, strike: int, expiry_iso: str,
                               future_bars, future_vix, iv_multiplier: float,
                               max_bars: int, mark_to_market: bool = False) -> dict:
    """Same T1-before-SL race as _simulate_outcome, but re-prices each
    future INDEX bar's high/low into OPTION PREMIUM terms via
    Black-Scholes before comparing against the premium target/stop - this
    is what actually determines WIN/LOSS for a real option buyer, not the
    raw index move."""
    bars = future_bars.head(max_bars).reset_index(drop=True)
    vix_bars = future_vix.head(max_bars).reset_index(drop=True) if future_vix is not None else None
    if len(bars) == 0:
        return {"result": "TIMEOUT", "bars_held": 0, "reason": "no_future_bars",
                "exit_price": entry_premium, "index_exit_price": None, "pnl_points": 0.0, "exit_ts": None}

    for offset, row in bars.iterrows():
        vix_now = float(vix_bars.iloc[offset]) if vix_bars is not None and offset < len(vix_bars) else None
        ts = row["timestamp"]
        spot_high, spot_low = float(row["high"]), float(row["low"])
        # CE premium rises with spot -> bar's spot HIGH gives premium HIGH.
        # PE premium rises as spot FALLS -> bar's spot LOW gives premium HIGH.
        hi_spot, lo_spot = (spot_high, spot_low) if side == "CE" else (spot_low, spot_high)
        premium_high = _premium_at(hi_spot, strike, side, vix_now, expiry_iso, ts, iv_multiplier)
        premium_low = _premium_at(lo_spot, strike, side, vix_now, expiry_iso, ts, iv_multiplier)
        if premium_high is None or premium_low is None:
            # Past expiry or bad VIX at this instant - can't price, skip bar
            continue

        hit_sl = premium_low <= sl_premium
        hit_t1 = premium_high >= t1_premium
        held = int(offset) + 1

        if hit_sl and hit_t1:
            return {"result": "LOSS", "bars_held": held, "reason": "same_bar_sl_and_t1",
                    "exit_price": sl_premium, "index_exit_price": lo_spot,
                    "pnl_points": -abs(entry_premium - sl_premium), "exit_ts": ts}
        if hit_sl:
            return {"result": "LOSS", "bars_held": held, "reason": "sl_hit",
                    "exit_price": sl_premium, "index_exit_price": lo_spot,
                    "pnl_points": -abs(entry_premium - sl_premium), "exit_ts": ts}
        if hit_t1:
            return {"result": "WIN", "bars_held": held, "reason": "t1_hit",
                    "exit_price": t1_premium, "index_exit_price": hi_spot,
                    "pnl_points": abs(t1_premium - entry_premium), "exit_ts": ts}

    last_row = bars.iloc[-1]
    last_ts = last_row["timestamp"]
    last_vix = float(vix_bars.iloc[-1]) if vix_bars is not None and len(vix_bars) else None
    last_spot = float(last_row["close"])
    last_premium = _premium_at(last_spot, strike, side, last_vix, expiry_iso, last_ts, iv_multiplier)
    if last_premium is None:
        last_premium = entry_premium
    pnl = last_premium - entry_premium

    if mark_to_market:
        result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
        return {"result": result, "bars_held": len(bars), "reason": "mark_to_market_at_hold_end",
                "exit_price": round(last_premium, 2), "index_exit_price": last_spot,
                "pnl_points": round(pnl, 2), "exit_ts": last_ts}

    return {"result": "TIMEOUT", "bars_held": len(bars), "reason": "max_hold_reached",
            "exit_price": round(last_premium, 2), "index_exit_price": last_spot,
            "pnl_points": round(pnl, 2), "exit_ts": last_ts}


def _signal_at(df_slice, symbol: str, mode: str,
               min_score: int | None = None) -> dict | None:
    if len(df_slice) < 40:
        return None

    # If bars are already ~5m, resampling again is a no-op / weak; still OK.
    df_5m = resample_ohlcv(df_slice, "5min")
    if len(df_5m) < 5:
        df_5m = df_slice

    gbb_result = None
    if mode == "GBB":
        # df_slice already accumulates every bar up to this point in the
        # walk-forward loop (session_vwap resets per calendar day itself,
        # see gbb_setup.py), so no separate longer fetch is needed here
        # the way orchestrator.py's live path needs one.
        gbb_result = compute_gbb_signal(df_5m, df_slice, CONFIG.gbb.min_grade_score_pct)
        side = gbb_result["side"] or "CE"
        base = {"score": round(gbb_result["score"] / gbb_result["max_score"] * 7, 1) if gbb_result["max_score"] else 0,
                "side": side}
        max_components = 7
    elif mode == "SCALP":
        comp = score_components(df_slice)
        base = compute_score(comp["votes"])
        side = base["side"]
        max_components = base["max_score"]
    else:
        smart = score_smart_trade(df_slice)
        base = compute_smart_score(smart["votes"])
        side = base["side"]
        max_components = base["max_score"]

    pa = detect_pattern(df_slice)
    bonus = pa_bonus_points(pa, side)
    total_score = base["score"] + bonus

    if mode == "GBB":
        if gbb_result["side"] is None or gbb_result["grade"] == "NO TRADE" or gbb_result["state"] != "CONFIRMED":
            return None
        verdict = "STRONG BUY" if (gbb_result["grade"] in ("A+", "A") and side == "CE") else \
                  "STRONG SELL" if (gbb_result["grade"] in ("A+", "A") and side == "PE") else \
                  ("BUY" if side == "CE" else "SELL")
    else:
        verdict = verdict_label(total_score, side, max_components)

    if "WAIT" in verdict:
        return None
    # Gate on the raw base score (out of 7), matching auto_trade.py's live
    # capture gate exactly (min_base_score) - NOT total_score, which is
    # inflated by up to +2 price-action bonus points and would let weaker
    # base setups through that live trading would have skipped.
    if min_score is not None and base["score"] < min_score:
        return None

    atr_value = float(atr(df_5m).iloc[-1]) if len(df_5m) > 14 else float(atr(df_slice).iloc[-1])
    if atr_value != atr_value:  # NaN
        return None

    entry = float(df_slice.iloc[-1]["close"])
    if mode == "GBB":
        levels = gbb_levels(entry, side, gbb_result.get("structure_stop"), atr_value)
        max_hold = levels["max_hold_minutes"]
    elif mode == "SCALP":
        levels = scalp_levels(entry, side, atr_value)
        max_hold = levels["max_hold_minutes"]
    else:
        levels = smart_trade_levels(df_slice, side, atr_value, symbol)
        max_hold = 30

    regime = classify_regime(df_5m) if len(df_5m) > 20 else {"regime": "UNKNOWN", "adx": None}

    return {
        "verdict": verdict,
        "side": side,
        "mode": mode,
        "score": total_score,
        "base_score": base["score"],
        "max_components": max_components,
        "levels": levels,
        "price_action": pa,
        "regime": regime,
        "max_hold": max_hold,
        "atr": atr_value,
        "entry_ts": str(df_slice.iloc[-1]["timestamp"]),
    }


def _generate_trades(df, symbol: str, mode: str,
                     step_minutes: int, min_history: int,
                     bar_minutes: int, hold_minutes: int | None,
                     mark_to_market: bool, min_score: int,
                     vix_series=None, iv_multiplier: float | None = None,
                     target_premium_points: float | None = None,
                     sl_premium_points: float | None = None) -> list[dict]:
    """Walk-forward signal generation + outcome simulation. Shared by
    run_backtest (aggregate report) and build_backtest_trade_chart
    (single-trade detail view) so both see identical trades.

    vix_series: pd.Series of India VIX closes, POSITION-aligned to df (same
    length, same bar timestamps) - required to price option premium via
    Black-Scholes at each signal/future bar. If None, falls back to raw
    index points (old behavior) with a clear "pricing": "index" tag.

    target_premium_points: overrides CONFIG.auto_trade.target_premium_points
    for this run (default None = use the live config value, 12).

    sl_premium_points: if set, uses a FIXED premium-points stop instead of
    the ATR-derived index stop re-priced into premium terms - lets you
    test a stop sized proportionally to the fixed target (e.g. symmetric
    1:1) instead of whatever the ATR happens to produce."""
    bar_minutes = max(1, int(bar_minutes))
    step_bars = max(1, int(round(step_minutes / bar_minutes)))
    min_bars = max(40, int(round(min_history / bar_minutes)))
    iv_mult = iv_multiplier if iv_multiplier is not None else CONFIG.option_pricing.iv_multiplier
    target_pts = target_premium_points if target_premium_points is not None else CONFIG.auto_trade.target_premium_points
    lot_size = LOT_SIZES.get(symbol, 65)

    trades = []
    i = min_bars
    n = len(df)
    while i < n - 2:
        slice_df = df.iloc[: i + 1].reset_index(drop=True)
        sig = _signal_at(slice_df, symbol, mode, min_score=min_score)
        if sig is None:
            i += step_bars
            continue

        # Anti-overtrading: mirror the live gate (auto_trade.py) - cap
        # trades per calendar day and stop for the day after a WIN, so the
        # backtest doesn't credit itself with entries the live bot would
        # never have taken.
        entry_day = pd.Timestamp(sig["entry_ts"]).strftime("%Y-%m-%d")
        todays_trades = [t for t in trades if pd.Timestamp(t["entry_ts"]).strftime("%Y-%m-%d") == entry_day]
        if len(todays_trades) >= CONFIG.auto_trade.max_trades_per_day:
            i += step_bars
            continue
        if CONFIG.auto_trade.stop_after_first_win and any(t["result"] == "WIN" for t in todays_trades):
            i += step_bars
            continue

        # Time-of-day cutoffs: mirror the live gate (auto_trade.py) - no
        # entries before no_entry_before or at/after no_entry_after, so the
        # backtest doesn't credit itself with entries the live bot would
        # now refuse (e.g. the volatile opening window before 10am, or the
        # last-30-minutes window after 3pm).
        entry_time = pd.Timestamp(sig["entry_ts"]).time()
        if entry_time < CONFIG.auto_trade.no_entry_before or entry_time >= CONFIG.auto_trade.no_entry_after:
            i += step_bars
            continue

        future = df.iloc[i + 1 :].reset_index(drop=True)
        future_vix = vix_series.iloc[i + 1 :].reset_index(drop=True) if vix_series is not None else None
        levels = sig["levels"]
        entry_ts = slice_df.iloc[-1]["timestamp"]

        eval_hold = hold_minutes if hold_minutes is not None else int(sig["max_hold"])
        # Coarse 5m SCALP: evaluate over 30–60m so T1 (~ATR*3.6) can print
        if hold_minutes is None and bar_minutes >= 5:
            eval_hold = 60 if mode == "SCALP" else 90

        max_bars = max(1, int(round(eval_hold / bar_minutes)))

        strike = _atm_strike(levels["entry"])
        expiry_iso = expiry_date_iso_for(symbol, pd.Timestamp(entry_ts))
        vix_now = float(vix_series.iloc[i]) if vix_series is not None and i < len(vix_series) else None
        entry_premium = _premium_at(levels["entry"], strike, sig["side"], vix_now, expiry_iso, entry_ts, iv_mult)

        if vix_series is not None and entry_premium is not None:
            if sl_premium_points is not None:
                sl_premium = round(max(0.05, entry_premium - sl_premium_points), 2)
            else:
                sl_premium = _premium_at(levels["stop_loss"], strike, sig["side"], vix_now, expiry_iso, entry_ts, iv_mult)
            if sl_premium is None or sl_premium >= entry_premium:
                # Can't price (e.g. past expiry at this instant) - skip this signal.
                i += step_bars
                continue
            # Mirror trade_recommendation.py / live_capture.py's mode-based
            # target rule exactly, so backtest win/loss reflects what live
            # auto-capture would actually do:
            #   - GBB and SCALP: RATIO target off THIS trade's actual
            #     premium stop distance (entry + (entry - sl) * rr_multiple).
            #     GBB was already ratio-based live but this backtest used to
            #     apply the flat points rule to it too - fixed here as part
            #     of the same change.
            #   - SMART_TRADE: unchanged, flat target_premium_points.
            # T2 is informational only (same as live_capture.py / the
            # dashboard "Target 2" - never simulated as a separate exit),
            # so it mirrors each mode's index-level T1->T2 ratio rather than
            # being backtested on its own:
            #   - GBB/SCALP: ratio target, one rr_multiple step further out
            #     than T1 (matches gbb_levels()/scalp T2 in modes.py, which
            #     use rr_multiple + 1.0 for T2 vs rr_multiple for T1).
            #   - SMART_TRADE: flat target_pts scaled by the same
            #     target2_atr_mult/target1_atr_mult ratio used for its
            #     index-level T1/T2 (9.0/5.0 = 1.8x), since SMART_TRADE has
            #     no ratio rule of its own to extend.
            if mode == "GBB":
                t1_premium = round(entry_premium + (entry_premium - sl_premium) * CONFIG.gbb.rr_multiple, 2)
                t2_premium = round(entry_premium + (entry_premium - sl_premium) * (CONFIG.gbb.rr_multiple + 1.0), 2)
            elif mode == "SCALP":
                t1_premium = round(entry_premium + (entry_premium - sl_premium) * CONFIG.scalp.rr_multiple, 2)
                t2_premium = round(entry_premium + (entry_premium - sl_premium) * (CONFIG.scalp.rr_multiple + 1.0), 2)
            else:
                t1_premium = round(entry_premium + target_pts, 2)
                t2_premium = round(
                    entry_premium + target_pts * (CONFIG.smart.target2_atr_mult / CONFIG.smart.target1_atr_mult), 2
                )
            outcome = _simulate_premium_outcome(
                sig["side"], entry_premium, t1_premium, sl_premium, strike, expiry_iso,
                future, future_vix, iv_mult, max_bars=max_bars, mark_to_market=mark_to_market,
            )
            pricing = "premium (Black-Scholes estimate)"
            entry_display, t1_display, sl_display = entry_premium, t1_premium, sl_premium
            t2_display = t2_premium
        else:
            outcome = _simulate_outcome(
                sig["side"], levels["entry"], levels["target1"], levels["stop_loss"],
                future, max_bars=max_bars, mark_to_market=mark_to_market,
            )
            pricing = "index (no VIX history supplied)"
            entry_display, t1_display, sl_display = levels["entry"], levels["target1"], levels["stop_loss"]
            t2_display = levels.get("target2")

        exit_ts = outcome.get("exit_ts")
        pnl_points = outcome.get("pnl_points")
        trades.append({
            "entry_ts": sig["entry_ts"],
            "entry_time_ist": _fmt_ist(slice_df.iloc[-1]["timestamp"]),
            "exit_ts": str(exit_ts) if exit_ts is not None else None,
            "exit_time_ist": _fmt_ist(exit_ts),
            "timezone": "Asia/Kolkata",
            "verdict": sig["verdict"],
            "side": sig["side"],
            "pricing": pricing,
            "strike": strike if vix_series is not None else None,
            "expiry": expiry_iso if vix_series is not None else None,
            "entry": entry_display,
            "target1": t1_display,
            "target2": t2_display,
            "stop_loss": sl_display,
            "index_entry": levels["entry"],
            "index_target1": levels["target1"],
            "index_target2": levels.get("target2"),
            "index_stop_loss": levels["stop_loss"],
            "rr": levels["rr"],
            "score": sig["score"],
            "result": outcome["result"],
            "bars_held": outcome["bars_held"],
            "reason": outcome["reason"],
            "exit_price": outcome.get("exit_price"),
            "index_exit_price": outcome.get("index_exit_price"),
            "pnl_points": pnl_points,
            "pnl_rupees": round(pnl_points * lot_size, 2) if pnl_points is not None else None,
            "invested_rupees": round(entry_display * lot_size, 2) if entry_display is not None else None,
            "lot_size": lot_size,
            "t1_distance": round(abs(t1_display - entry_display), 1) if t1_display is not None and entry_display is not None else None,
            "bar_index": i,
            "exit_bar_index": i + outcome["bars_held"],
        })
        i += max(step_bars, outcome["bars_held"])

    return trades


def run_backtest(df, symbol: str = "NIFTY", mode: str = "SCALP",
                 step_minutes: int = 15, min_history: int = 60,
                 bar_minutes: int = 1,
                 hold_minutes: int | None = None,
                 mark_to_market: bool = True,
                 min_score: int = 5,
                 vix_series=None, iv_multiplier: float | None = None,
                 target_premium_points: float | None = None,
                 sl_premium_points: float | None = None) -> dict:
    """
    df: OHLCV with columns timestamp, open, high, low, close, volume.
    bar_minutes: candle size of df (1 for 1m, 5 for 5m backtests).
    hold_minutes: override evaluation window (default = mode max_hold,
                  bumped on coarse bars).
    mark_to_market: if True, TIMEOUT becomes WIN/LOSS at hold-end close.
    min_score: skip weak signals (default 5, on the raw base score out of
               7 - matches auto_trade.py's live capture gate exactly).
    vix_series: India VIX closes, position-aligned to df (same length,
               same bars). Pass this to price OPTION PREMIUM P&L (what
               live auto-capture actually trades) instead of raw index
               points - strongly recommended, see module docstring.
    iv_multiplier: overrides CONFIG.option_pricing.iv_multiplier for this run.
    """
    mode = mode.upper()
    if mode not in ("SCALP", "SMART_TRADE", "GBB"):
        raise ValueError("mode must be SCALP, SMART_TRADE, or GBB")

    bar_minutes = max(1, int(bar_minutes))
    n = len(df)
    trades = _generate_trades(
        df, symbol, mode, step_minutes, min_history,
        bar_minutes, hold_minutes, mark_to_market, min_score,
        vix_series=vix_series, iv_multiplier=iv_multiplier,
        target_premium_points=target_premium_points, sl_premium_points=sl_premium_points,
    )

    wins = sum(1 for t in trades if t["result"] == "WIN")
    losses = sum(1 for t in trades if t["result"] == "LOSS")
    timeouts = sum(1 for t in trades if t["result"] == "TIMEOUT")
    breakevens = sum(1 for t in trades if t["result"] == "BREAKEVEN")
    decided = wins + losses
    win_rate = round(100.0 * wins / decided, 1) if decided else None
    timeout_rate = round(100.0 * timeouts / len(trades), 1) if trades else None
    avg_pnl = round(sum(t.get("pnl_points") or 0 for t in trades) / len(trades), 2) if trades else None
    avg_pnl_rupees = round(sum(t.get("pnl_rupees") or 0 for t in trades) / len(trades), 2) if trades else None
    total_pnl_rupees = round(sum(t.get("pnl_rupees") or 0 for t in trades), 2) if trades else None
    avg_t1 = round(
        sum(t["t1_distance"] for t in trades if t.get("t1_distance") is not None)
        / max(1, sum(1 for t in trades if t.get("t1_distance") is not None)), 1
    ) if trades else None
    pricing_mode = trades[0]["pricing"] if trades else ("premium (Black-Scholes estimate)" if vix_series is not None else "index (no VIX history supplied)")

    first_ts = str(df.iloc[0]["timestamp"]) if n else None
    last_ts = str(df.iloc[-1]["timestamp"]) if n else None

    return {
        "symbol": symbol,
        "mode": mode,
        "timezone": "Asia/Kolkata",
        "bar_minutes": bar_minutes,
        "eval_hold_minutes": hold_minutes if hold_minutes is not None else (
            60 if bar_minutes >= 5 and mode == "SCALP" else (90 if bar_minutes >= 5 else None)
        ),
        "mark_to_market": mark_to_market,
        "min_score": min_score,
        "pricing": pricing_mode,
        "candles": n,
        "from": first_ts,
        "to": last_ts,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "breakevens": breakevens,
        "decided": decided,
        "win_rate_pct": win_rate,
        "timeout_rate_pct": timeout_rate,
        "avg_pnl_points": avg_pnl,
        "avg_pnl_rupees": avg_pnl_rupees,
        "total_pnl_rupees": total_pnl_rupees,
        "avg_t1_distance": avg_t1,
        "accuracy_notes": [
            "Premium is a Black-Scholes ESTIMATE calibrated on real VIX "
            "history, not live bid-ask quotes." if vix_series is not None else
            "INDEX points only — not option premium P&L / ATM-OTM decay (no VIX history supplied).",
            "SL/target priced off the signal-time VIX and expiry — not re-estimated for IV changes mid-trade.",
            "No slippage, spread, or gap modeling.",
            "5m multi-month history ≠ live 1m SCALP fills.",
            "Win rate ignores TIMEOUT unless mark_to_market is on.",
            "Use as a filter sanity check, not a guaranteed expectancy.",
        ],
        "sample_warning": (
            None if decided >= 10
            else "Few decided trades — win_rate is not meaningful yet."
        ),
        "note": (
            f"WIN=T1 first, LOSS=SL first"
            + (", TIMEOUT→MTM at hold end" if mark_to_market else ", TIMEOUT=no hit in hold")
            + f". Bars={bar_minutes}m. min_score>={min_score}."
        ),
        "samples": trades[:200],
    }


def build_backtest_trade_chart(df, symbol: str, mode: str, trade_index: int,
                               step_minutes: int = 15, min_history: int = 60,
                               bar_minutes: int = 1,
                               hold_minutes: int | None = None,
                               mark_to_market: bool = True,
                               min_score: int = 5,
                               pad_bars: int = 12,
                               vix_series=None, iv_multiplier: float | None = None) -> dict:
    """
    Re-runs the same walk-forward simulation as run_backtest (identical
    params => identical trades) and returns candles + entry/exit/target/stop
    overlay data for trade `trade_index`, in the same shape the journal's
    /chart page (chart.html) already knows how to render.

    The chart candles are always INDEX-scale (that's the only OHLC data
    available), so the overlay lines use index_entry/index_target1/
    index_stop_loss - plotting the premium-scale entry/target/stop directly
    on an index candle chart would be nonsense (wrong order of magnitude).
    Premium P&L is still reported separately via pnl_points/pnl_rupees.
    """
    mode = mode.upper()
    bar_minutes = max(1, int(bar_minutes))
    trades = _generate_trades(
        df, symbol, mode, step_minutes, min_history,
        bar_minutes, hold_minutes, mark_to_market, min_score,
        vix_series=vix_series, iv_multiplier=iv_multiplier,
    )
    if trade_index < 0 or trade_index >= len(trades):
        return {"ok": False, "error": f"Trade index {trade_index} out of range (0..{len(trades)-1})"}

    t = trades[trade_index]
    n = len(df)
    win_start = max(0, t["bar_index"] - pad_bars)
    win_end = min(n, t["exit_bar_index"] + pad_bars + 1)
    window = df.iloc[win_start:win_end].reset_index(drop=True)
    candles = candles_to_list(window)

    entry_dt = pd.Timestamp(t["entry_ts"])
    exit_dt = pd.Timestamp(t["exit_ts"]) if t["exit_ts"] else None

    title = f"{symbol} {mode.replace('_', ' ').title()} · {t['side']} (backtest)"
    return {
        "ok": True,
        "title": title,
        "symbol": symbol,
        "side": t["side"],
        "mode": mode,
        "timezone": "Asia/Kolkata",
        "pricing": t.get("pricing"),
        "strike": t.get("strike"),
        "expiry": t.get("expiry"),
        "entry": {
            "price": t["index_entry"],
            "time": entry_dt.isoformat(),
            "time_label": t["entry_time_ist"],
        },
        "exit": {
            # index_exit_price when premium-priced (real chart scale);
            # falls back to exit_price itself when there's no VIX series
            # (that branch's exit_price is already index-scale).
            "price": t.get("index_exit_price") if t.get("index_exit_price") is not None else t["exit_price"],
            "time": exit_dt.isoformat() if exit_dt is not None else None,
            "time_label": t["exit_time_ist"],
        },
        "target": t["index_target1"],
        "stop": t["index_stop_loss"],
        "premium_entry": t.get("entry"),
        "premium_target": t.get("target1"),
        "premium_stop": t.get("stop_loss"),
        "pnl_points": t["pnl_points"],
        "pnl_rupees": t.get("pnl_rupees"),
        "pnl_pct": (
            round((t["pnl_points"] / t["entry"]) * 100, 2)
            if t["pnl_points"] is not None and t.get("entry") else None
        ),
        "rr": t["rr"],
        "result": t["result"],
        "candles": candles,
        "candle_source": "index",
        "note": (
            "Index-level OHLC for the chart lines (only data available); "
            "P&L is simulated OPTION PREMIUM (Black-Scholes estimate), see premium_entry/target/stop."
        ),
    }
