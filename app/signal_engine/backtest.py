"""
Historical win/loss report for emitted SCALP / SMART TRADE levels.

Walks 1m candles, generates a signal on each sample bar, then checks whether
T1 or SL is hit first within the mode's hold window (SCALP: max_hold_minutes;
SMART TRADE: default 30 minutes of subsequent 1m bars).
"""

from __future__ import annotations

from app.indicators.core import atr
from app.indicators.multi_tf import resample_ohlcv
from app.price_action.patterns import detect_pattern, pa_bonus_points
from app.signal_engine.scorer import score_components, compute_score, verdict_label
from app.signal_engine.smart_scorer import score_smart_trade, compute_smart_score
from app.signal_engine.modes import scalp_levels, smart_trade_levels
from app.signal_engine.regime import classify_regime
from app.config import CONFIG


def _simulate_outcome(side: str, entry: float, t1: float, sl: float,
                      future_bars, max_bars: int) -> dict:
    """
    Mark WIN if T1 is touched before SL; LOSS if SL first; TIMEOUT otherwise.
    Uses candle high/low so intrabar hits count.
    """
    bars = future_bars.head(max_bars).reset_index(drop=True)
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
            # Same bar ambiguity — treat as LOSS (conservative)
            return {"result": "LOSS", "bars_held": held, "reason": "same_bar_sl_and_t1"}
        if hit_sl:
            return {"result": "LOSS", "bars_held": held, "reason": "sl_hit"}
        if hit_t1:
            return {"result": "WIN", "bars_held": held, "reason": "t1_hit"}

    return {"result": "TIMEOUT", "bars_held": len(bars), "reason": "max_hold_reached"}


def _signal_at(df_slice, symbol: str, mode: str) -> dict | None:
    if len(df_slice) < 40:
        return None

    df_5m = resample_ohlcv(df_slice, "5min")
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

    atr_value = float(atr(df_5m).iloc[-1]) if len(df_5m) > 14 else float(atr(df_slice).iloc[-1])
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
        "entry_ts": str(df_slice.iloc[-1]["timestamp"]),
    }


def run_backtest(df, symbol: str = "NIFTY", mode: str = "SCALP",
                 step_minutes: int = 5, min_history: int = 60) -> dict:
    """
    df: 1m OHLCV with columns timestamp, open, high, low, close, volume.
    Samples every step_minutes bars after min_history.
    """
    mode = mode.upper()
    if mode not in ("SCALP", "SMART_TRADE"):
        raise ValueError("mode must be SCALP or SMART_TRADE")

    trades = []
    i = min_history
    n = len(df)
    while i < n - 2:
        slice_df = df.iloc[: i + 1].reset_index(drop=True)
        sig = _signal_at(slice_df, symbol, mode)
        if sig is None:
            i += step_minutes
            continue

        future = df.iloc[i + 1 :].reset_index(drop=True)
        levels = sig["levels"]
        outcome = _simulate_outcome(
            sig["side"], levels["entry"], levels["target1"], levels["stop_loss"],
            future, max_bars=int(sig["max_hold"]),
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
        })
        # Skip ahead by hold window to avoid heavily overlapping trades
        i += max(step_minutes, outcome["bars_held"])

    wins = sum(1 for t in trades if t["result"] == "WIN")
    losses = sum(1 for t in trades if t["result"] == "LOSS")
    timeouts = sum(1 for t in trades if t["result"] == "TIMEOUT")
    decided = wins + losses
    win_rate = round(100.0 * wins / decided, 1) if decided else None
    timeout_rate = round(100.0 * timeouts / len(trades), 1) if trades else None

    return {
        "symbol": symbol,
        "mode": mode,
        "candles": n,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "decided": decided,
        "win_rate_pct": win_rate,
        "timeout_rate_pct": timeout_rate,
        "sample_warning": (
            None if decided >= 10
            else "Few trades resolved inside the hold window — win_rate is not meaningful yet."
        ),
        "note": (
            "WIN = T1 touched before SL within hold window on subsequent 1m "
            "candles. Same-bar SL+T1 counted as LOSS. TIMEOUT = neither hit "
            "within hold (5m SCALP / 30m SMART TRADE). Index levels only "
            "(not option premium P&L)."
        ),
        "samples": trades[:50],  # cap payload size
    }
