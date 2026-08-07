"""
Historical win/loss report for emitted SCALP / SMART TRADE levels.

Honest limitations (read before trusting win_rate):
  - Uses INDEX levels, not option premium P&L.
  - Multi-month runs use 5-minute bars (Kite 1m history is capped) while
    live SCALP is designed on 1-minute — coarser fills.
  - No slippage, gap risk, or bid-ask.
  - Default scoring: T1 before SL = WIN, SL first = LOSS.
  - Optional mark_to_market: at hold end, close vs entry decides WIN/LOSS
    instead of TIMEOUT (more trades "decided", still approximate).
"""

from __future__ import annotations

from app.indicators.core import atr
from app.indicators.multi_tf import resample_ohlcv
from app.price_action.patterns import detect_pattern, pa_bonus_points
from app.signal_engine.scorer import score_components, compute_score, verdict_label
from app.signal_engine.smart_scorer import score_smart_trade, compute_smart_score
from app.signal_engine.modes import scalp_levels, smart_trade_levels
from app.signal_engine.regime import classify_regime


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
                "exit_price": entry, "pnl_points": 0.0}

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
            }
        if hit_sl:
            return {
                "result": "LOSS", "bars_held": held, "reason": "sl_hit",
                "exit_price": sl, "pnl_points": -abs(entry - sl),
            }
        if hit_t1:
            return {
                "result": "WIN", "bars_held": held, "reason": "t1_hit",
                "exit_price": t1, "pnl_points": abs(t1 - entry),
            }

    last_close = float(bars.iloc[-1]["close"])
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
        }

    return {
        "result": "TIMEOUT",
        "bars_held": len(bars),
        "reason": "max_hold_reached",
        "exit_price": last_close,
        "pnl_points": round(pnl, 2),
    }


def _signal_at(df_slice, symbol: str, mode: str,
               min_score: int | None = None) -> dict | None:
    if len(df_slice) < 40:
        return None

    # If bars are already ~5m, resampling again is a no-op / weak; still OK.
    df_5m = resample_ohlcv(df_slice, "5min")
    if len(df_5m) < 5:
        df_5m = df_slice

    if mode == "SCALP":
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
    verdict = verdict_label(total_score, side, max_components)

    if "WAIT" in verdict:
        return None
    if min_score is not None and total_score < min_score:
        return None

    atr_value = float(atr(df_5m).iloc[-1]) if len(df_5m) > 14 else float(atr(df_slice).iloc[-1])
    if atr_value != atr_value:  # NaN
        return None

    entry = float(df_slice.iloc[-1]["close"])
    if mode == "SCALP":
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


def run_backtest(df, symbol: str = "NIFTY", mode: str = "SCALP",
                 step_minutes: int = 15, min_history: int = 60,
                 bar_minutes: int = 1,
                 hold_minutes: int | None = None,
                 mark_to_market: bool = True,
                 min_score: int = 4) -> dict:
    """
    df: OHLCV with columns timestamp, open, high, low, close, volume.
    bar_minutes: candle size of df (1 for 1m, 5 for 5m backtests).
    hold_minutes: override evaluation window (default = mode max_hold,
                  bumped on coarse bars).
    mark_to_market: if True, TIMEOUT becomes WIN/LOSS at hold-end close.
    min_score: skip weak signals (default 4 ≈ moderate+).
    """
    mode = mode.upper()
    if mode not in ("SCALP", "SMART_TRADE"):
        raise ValueError("mode must be SCALP or SMART_TRADE")

    bar_minutes = max(1, int(bar_minutes))
    step_bars = max(1, int(round(step_minutes / bar_minutes)))
    min_bars = max(40, int(round(min_history / bar_minutes)))

    trades = []
    i = min_bars
    n = len(df)
    while i < n - 2:
        slice_df = df.iloc[: i + 1].reset_index(drop=True)
        sig = _signal_at(slice_df, symbol, mode, min_score=min_score)
        if sig is None:
            i += step_bars
            continue

        future = df.iloc[i + 1 :].reset_index(drop=True)
        levels = sig["levels"]

        eval_hold = hold_minutes if hold_minutes is not None else int(sig["max_hold"])
        # Coarse 5m SCALP: evaluate over 30–60m so T1 (~ATR*3.6) can print
        if hold_minutes is None and bar_minutes >= 5:
            eval_hold = 60 if mode == "SCALP" else 90

        max_bars = max(1, int(round(eval_hold / bar_minutes)))

        outcome = _simulate_outcome(
            sig["side"], levels["entry"], levels["target1"], levels["stop_loss"],
            future, max_bars=max_bars, mark_to_market=mark_to_market,
        )
        trades.append({
            "entry_ts": sig["entry_ts"],
            "verdict": sig["verdict"],
            "side": sig["side"],
            "entry": levels["entry"],
            "target1": levels["target1"],
            "stop_loss": levels["stop_loss"],
            "rr": levels["rr"],
            "score": sig["score"],
            "result": outcome["result"],
            "bars_held": outcome["bars_held"],
            "reason": outcome["reason"],
            "exit_price": outcome.get("exit_price"),
            "pnl_points": outcome.get("pnl_points"),
            "t1_distance": round(abs(levels["target1"] - levels["entry"]), 1),
        })
        i += max(step_bars, outcome["bars_held"])

    wins = sum(1 for t in trades if t["result"] == "WIN")
    losses = sum(1 for t in trades if t["result"] == "LOSS")
    timeouts = sum(1 for t in trades if t["result"] == "TIMEOUT")
    breakevens = sum(1 for t in trades if t["result"] == "BREAKEVEN")
    decided = wins + losses
    win_rate = round(100.0 * wins / decided, 1) if decided else None
    timeout_rate = round(100.0 * timeouts / len(trades), 1) if trades else None
    avg_pnl = round(sum(t.get("pnl_points") or 0 for t in trades) / len(trades), 2) if trades else None
    avg_t1 = round(
        sum(t["t1_distance"] for t in trades) / len(trades), 1
    ) if trades else None

    first_ts = str(df.iloc[0]["timestamp"]) if n else None
    last_ts = str(df.iloc[-1]["timestamp"]) if n else None

    return {
        "symbol": symbol,
        "mode": mode,
        "bar_minutes": bar_minutes,
        "eval_hold_minutes": hold_minutes if hold_minutes is not None else (
            60 if bar_minutes >= 5 and mode == "SCALP" else (90 if bar_minutes >= 5 else None)
        ),
        "mark_to_market": mark_to_market,
        "min_score": min_score,
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
        "avg_t1_distance": avg_t1,
        "accuracy_notes": [
            "INDEX points only — not option premium P&L / ATM-OTM decay.",
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
