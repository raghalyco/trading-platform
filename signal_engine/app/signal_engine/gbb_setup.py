"""
GBB Setup — ported from the "Setup Scanner [GBB]" Pine Script indicator
into a new, independent THIRD mode (alongside SCALP/SMART_TRADE) for
NIFTY/SENSEX intraday scalping.

v1 scope (deliberately not all 6 Pine setups at once — quality over
completeness, matching the spec's own "acceptable to miss trades" rule):
  - VWAP reclaim
  - EMA pullback (9/21/50)
  - Break & Retest (the highest-priority setup — a real multi-bar state
    machine replayed over history, same technique as darvax.py's box
    detection, NOT a single-candle-crossing check)
  - Liquidity sweep
  - Chop filter (repeated VWAP crossings + EMA compression + low ATR)
  - Extended-move filter ("don't chase" — RSI + distance from VWAP/EMA)
  - Weighted scoring -> A+/A/B/C/NO TRADE grade

NOT yet ported (deferred to a later pass): RSI Divergence, Opening Range
Break, TP2 pyramiding/trailing, per-setup backtest breakdown table. These
were cut to keep v1 buildable and verifiable in one pass rather than
attempting all 37 spec sections simultaneously.

Timeframe architecture: the 5-minute frame decides DIRECTION/STRUCTURE
(the functions below), the 1-minute frame only REFINES entry timing
(confirm_1m below) — a weak 1m read can block an entry but can never
invent one the 5m setup didn't already produce.

R:R is intentionally NOT computed here. This module only produces an
INDEX-level structural stop-loss (the broken level / retest extreme).
The premium math and the 1:1.5 ratio live in trade_recommendation.py's
GBB branch — same separation SCALP/SMART_TRADE already use (index levels
here, premium conversion there).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators.core import ema, rsi, macd, atr as atr_series

STATE_NO_SETUP = "NO_SETUP"
STATE_FORMING = "FORMING"
STATE_BREAKOUT = "BREAKOUT"
STATE_WAITING_RETEST = "WAITING_FOR_RETEST"
STATE_RETEST = "RETEST"
STATE_CONFIRMED = "CONFIRMED"
STATE_INVALIDATED = "INVALIDATED"
STATE_FALSE_BREAKOUT = "FALSE_BREAKOUT"
STATE_CHOP = "CHOP"
STATE_EXTENDED = "EXTENDED"


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative typical-price VWAP, resetting at every calendar-day
    boundary found in df["timestamp"] - correct whether called with a
    single day's worth of bars (live, ~400min lookback) or a multi-day
    historical slice (backtest, which accumulates many sessions into one
    df) - a plain global cumsum would otherwise blend VWAP across days in
    the backtest case, which is not a session VWAP at all."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    if "timestamp" in df.columns:
        day = pd.to_datetime(df["timestamp"]).dt.date
    else:
        day = pd.Series(0, index=df.index)  # single unknown session - treat as one day
    pv = typical * df["volume"]
    cum_pv = pv.groupby(day).cumsum()
    # np.nan, NOT pd.NA: replacing with pd.NA silently upcasts this whole
    # Series from float64 to object dtype, and pandas' vectorized `<`/`>`
    # comparisons against an object-dtype Series containing pd.NA raise
    # "boolean value of NA is ambiguous" immediately - before any downstream
    # pd.isna()/.fillna() guard even gets a chance to run. np.nan keeps the
    # dtype float64, so `close < vwap` and `a and b`-style boolean
    # expressions all behave normally (a NaN comparison just evaluates to
    # False), and pd.isna() still catches it exactly the same as pd.NA.
    cum_vol = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return cum_pv / cum_vol


def _confirmed_pivots(high: pd.Series, low: pd.Series, length: int = 5):
    """Same idea as Pine's ta.pivothigh/pivotlow: a bar is a confirmed
    swing high/low only once `length` bars exist on both sides of it."""
    n = len(high)
    ph = [None] * n
    pl = [None] * n
    for i in range(length, n - length):
        window_h = high.iloc[i - length: i + length + 1]
        if high.iloc[i] == window_h.max():
            ph[i] = float(high.iloc[i])
        window_l = low.iloc[i - length: i + length + 1]
        if low.iloc[i] == window_l.min():
            pl[i] = float(low.iloc[i])
    return ph, pl


def vwap_reclaim(df: pd.DataFrame, vwap: pd.Series, min_bars_away: int = 6,
                  vol_ok: bool = True) -> dict:
    """Price spends >= min_bars_away bars on one side of session VWAP,
    then closes back across it this bar. vol_ok mirrors the Pine source's
    shared `volOk` gate (volume >= 1x 20-bar SMA) - Pine requires it for
    VWAP reclaim/EMA pullback/Liquidity sweep (not for Break & Retest,
    which is deliberately volume-agnostic there)."""
    close = df["close"]
    # Guard BOTH the last bar and the one before it - crossed_up/crossed_dn
    # below reads vwap.iloc[-2] as well as iloc[-1], and the original check
    # here only covered iloc[-1].
    if len(close) < min_bars_away + 2 or pd.isna(vwap.iloc[-1]) or pd.isna(vwap.iloc[-2]):
        return {"long": False, "short": False, "forming": False}

    # vwap can be pd.NA at ANY earlier bar too (session_vwap() replaces a
    # zero-cumulative-volume denominator with NA rather than dividing by
    # zero) - comparing a Series against a Series containing pd.NA produces
    # pd.NA entries in the result, and the consecutive-run loop below does
    # `if v:` on every entry, which crashes ("boolean value of NA is
    # ambiguous") the moment it hits one. Treat "relationship unknown at
    # this bar" as "doesn't extend the run" rather than letting it crash
    # the whole GBB signal.
    below = (close < vwap).fillna(False)
    above = (close > vwap).fillna(False)
    # Consecutive-run length ending at the PREVIOUS bar (before today's cross)
    run_below = 0
    for v in reversed(below.iloc[:-1].tolist()):
        if v:
            run_below += 1
        else:
            break
    run_above = 0
    for v in reversed(above.iloc[:-1].tolist()):
        if v:
            run_above += 1
        else:
            break

    crossed_up = close.iloc[-2] <= vwap.iloc[-2] and close.iloc[-1] > vwap.iloc[-1]
    crossed_dn = close.iloc[-2] >= vwap.iloc[-2] and close.iloc[-1] < vwap.iloc[-1]

    long_sig = crossed_up and run_below >= min_bars_away and vol_ok
    short_sig = crossed_dn and run_above >= min_bars_away and vol_ok
    forming = not long_sig and not short_sig and (
        (run_below >= min_bars_away and close.iloc[-1] > close.iloc[-2]) or
        (run_above >= min_bars_away and close.iloc[-1] < close.iloc[-2])
    )
    return {"long": bool(long_sig), "short": bool(short_sig), "forming": bool(forming)}


def ema_pullback(df: pd.DataFrame, ema9: pd.Series, ema21: pd.Series, ema50: pd.Series,
                  touch_window: int = 3, vol_ok: bool = True) -> dict:
    """Trend by 21/50, price pulls back into the 9/21 band, then closes
    away again with a bullish/bearish bar breaking the prior bar's
    extreme - same conditions as the Pine EMA pullback setup. vol_ok
    mirrors Pine's shared volOk gate (see vwap_reclaim's docstring)."""
    if len(df) < 55:
        return {"long": False, "short": False, "forming": False}

    trend_up = bool(ema21.iloc[-1] > ema50.iloc[-1] and ema50.iloc[-1] > ema50.iloc[-4])
    trend_dn = bool(ema21.iloc[-1] < ema50.iloc[-1] and ema50.iloc[-1] < ema50.iloc[-4])

    # Pine's ta.barssince(cond) <= touch_window is true when cond held on the
    # CURRENT bar or any of the touch_window bars before it - touch_window+1
    # bars total. Slicing [-touch_window:] here used to only cover
    # touch_window bars (missing the oldest one Pine would still count).
    window = touch_window + 1
    recent_low = df["low"].iloc[-window:]
    recent_high = df["high"].iloc[-window:]
    touched_up = bool((recent_low <= ema21.iloc[-window:]).any())
    touched_dn = bool((recent_high >= ema21.iloc[-window:]).any())

    close = df["close"]
    bull_bar = close.iloc[-1] > df["open"].iloc[-1]
    bear_bar = close.iloc[-1] < df["open"].iloc[-1]

    long_sig = (trend_up and touched_up and close.iloc[-1] > ema9.iloc[-1]
                and bull_bar and close.iloc[-1] > df["high"].iloc[-2] and vol_ok)
    short_sig = (trend_dn and touched_dn and close.iloc[-1] < ema9.iloc[-1]
                 and bear_bar and close.iloc[-1] < df["low"].iloc[-2] and vol_ok)
    forming = not long_sig and not short_sig and (
        (trend_up and df["low"].iloc[-1] <= ema21.iloc[-1] and close.iloc[-1] < ema9.iloc[-1]) or
        (trend_dn and df["high"].iloc[-1] >= ema21.iloc[-1] and close.iloc[-1] > ema9.iloc[-1])
    )
    return {"long": bool(long_sig), "short": bool(short_sig), "forming": bool(forming)}


def liquidity_sweep(df: pd.DataFrame, lookback: int = 20, vol_ok: bool = True) -> dict:
    """Wick takes out a recent extreme, body closes back inside - a sweep
    ALONE never fires an entry (per spec, section 11); it only feeds the
    score as a confirming component. vol_ok mirrors Pine's shared volOk
    gate (see vwap_reclaim's docstring)."""
    if len(df) < lookback + 2:
        return {"long": False, "short": False}
    sw_lo = df["low"].iloc[-lookback - 1:-1].min()
    sw_hi = df["high"].iloc[-lookback - 1:-1].max()
    last = df.iloc[-1]
    bull_bar = last["close"] > last["open"]
    bear_bar = last["close"] < last["open"]
    long_sig = (last["low"] < sw_lo and last["close"] > sw_lo and bull_bar
                and (last["close"] - last["low"]) > (last["high"] - last["close"]) and vol_ok)
    short_sig = (last["high"] > sw_hi and last["close"] < sw_hi and bear_bar
                 and (last["high"] - last["close"]) > (last["close"] - last["low"]) and vol_ok)
    return {"long": bool(long_sig), "short": bool(short_sig)}


def break_and_retest(df: pd.DataFrame, pivot_length: int = 5, retest_tolerance_atr: float = 0.3,
                      retest_window: int = 20) -> dict:
    """Full state-machine replay over history (same technique as
    darvax.py's find_darvas_boxes) instead of a single-candle-crossing
    check: BREAKOUT -> WAITING_FOR_RETEST -> RETEST -> CONFIRMED, with an
    ATR-based retest ZONE (not an exact price). Returns the state as of
    the LAST bar only - this isn't a list of historical trades, it's
    "where is the current setup right now."

    The breakout trigger is intentionally unconditional (close beyond the
    level, nothing else) - matching the actual Pine source exactly, which
    has NO body-strength or volume filter on Break & Retest at all (Pine's
    own comment: "a valid retest is often quiet"). An earlier version of
    this port added a min_body_atr/min_volume_mult false-breakout filter
    here that doesn't exist in the Pine indicator - it was silently
    discarding real breakout levels (any breakout candle with a moderate
    body or below-average volume got thrown away before the retest cycle
    ever started), causing this to miss setups the Pine reference actually
    confirmed. Removed to match the source of truth.
    """
    n = len(df)
    empty = {"state": STATE_NO_SETUP, "direction": None, "level": None,
              "retest_zone": None, "entry": None, "stop": None}
    if n < pivot_length * 2 + retest_window + 2:
        return empty

    high, low, close = df["high"], df["low"], df["close"]
    atr_v = atr_series(df).bfill()
    ph_list, pl_list = _confirmed_pivots(high, low, pivot_length)

    # Replay state
    hi_level = hi_break_bar = None
    lo_level = lo_break_bar = None
    hi_confirmed_bar = lo_confirmed_bar = None
    state = STATE_NO_SETUP
    direction = None
    level = None

    for i in range(n):
        # Check breakout against the level as it stood BEFORE this bar -
        # updating the level to a fresh pivot first (as this used to do)
        # meant a breakout bar that also happens to register as the new
        # local pivot immediately overwrote the level it just broke,
        # before the comparison ever ran against the old one.

        # Fresh breakout above the last confirmed pivot high - unconditional,
        # matching Pine's `close > brtHiLvl` exactly (no body/volume filter).
        if hi_level is not None and hi_break_bar is None and close.iloc[i] > hi_level:
            hi_break_bar = i

        if lo_level is not None and lo_break_bar is None and close.iloc[i] < lo_level:
            lo_break_bar = i

        # Retest + confirmation, within the window. Once confirmed, keep
        # checking for a LATER invalidation (spec section 22: a decisive
        # close back below/above the level kills the setup even after it
        # was confirmed) - a confirmation isn't a one-shot event, it's a
        # standing state until either invalidated or the level's replaced.
        tol = atr_v.iloc[i] * retest_tolerance_atr
        if hi_break_bar is not None and i > hi_break_bar:
            if hi_confirmed_bar is None:
                if i - hi_break_bar > retest_window:
                    hi_level, hi_break_bar = None, None  # retest window expired, drop it
                elif low.iloc[i] <= hi_level + tol and close.iloc[i] > hi_level and close.iloc[i] > df["open"].iloc[i]:
                    hi_confirmed_bar = i
                elif close.iloc[i] < hi_level - tol:
                    hi_level, hi_break_bar = None, None  # lost the level entirely - invalidated
            elif close.iloc[i] < hi_level:
                hi_level, hi_break_bar, hi_confirmed_bar = None, None, None  # invalidated post-confirmation

        if lo_break_bar is not None and i > lo_break_bar:
            if lo_confirmed_bar is None:
                if i - lo_break_bar > retest_window:
                    lo_level, lo_break_bar = None, None
                elif high.iloc[i] >= lo_level - tol and close.iloc[i] < lo_level and close.iloc[i] < df["open"].iloc[i]:
                    lo_confirmed_bar = i
                elif close.iloc[i] > lo_level + tol:
                    lo_level, lo_break_bar = None, None
            elif close.iloc[i] > lo_level:
                lo_level, lo_break_bar, lo_confirmed_bar = None, None, None

        # Only now adopt a fresh pivot as the tracked level - and only
        # while not already mid-cycle on an active break/retest, so a new
        # pivot forming during the retest window can't silently replace
        # the level a live setup is still waiting to confirm against.
        if ph_list[i] is not None and hi_break_bar is None:
            hi_level = ph_list[i]
        if pl_list[i] is not None and lo_break_bar is None:
            lo_level = pl_list[i]

    last = n - 1
    # hi_confirmed_bar/lo_confirmed_bar being non-None here means "confirmed
    # and still valid as of the last bar" - the loop above already nulls
    # both out the moment a post-confirmation invalidation happens, so
    # there's no need to require the confirmation to have landed exactly
    # on the final bar (that was the bug: a setup confirmed several bars
    # ago and still running fell through to NO_SETUP instead of CONFIRMED).
    if hi_confirmed_bar is not None:
        state, direction, level = STATE_CONFIRMED, "long", hi_level
    elif lo_confirmed_bar is not None:
        state, direction, level = STATE_CONFIRMED, "short", lo_level
    elif hi_break_bar is not None:
        state, direction, level = STATE_WAITING_RETEST, "long", hi_level
    elif lo_break_bar is not None:
        state, direction, level = STATE_WAITING_RETEST, "short", lo_level

    if state == STATE_NO_SETUP:
        return empty

    tol_last = atr_v.iloc[last] * retest_tolerance_atr
    if direction == "long":
        retest_zone = (round(level - tol_last, 2), round(level + tol_last, 2))
        entry = float(close.iloc[last]) if state == STATE_CONFIRMED else None
        stop = float(low.iloc[hi_break_bar: last + 1].min()) if state == STATE_CONFIRMED else None
    else:
        retest_zone = (round(level - tol_last, 2), round(level + tol_last, 2))
        entry = float(close.iloc[last]) if state == STATE_CONFIRMED else None
        stop = float(high.iloc[lo_break_bar: last + 1].max()) if state == STATE_CONFIRMED else None

    return {"state": state, "direction": direction, "level": round(level, 2),
            "retest_zone": retest_zone, "entry": entry, "stop": stop}


def chop_filter(df: pd.DataFrame, vwap: pd.Series, ema9: pd.Series, ema21: pd.Series,
                 ema50: pd.Series, atr_v: pd.Series, cross_window: int = 20,
                 max_crosses: int = 5, compression_atr_mult: float = 0.5,
                 low_atr_percentile: float = 0.25) -> bool:
    """NIFTY frequently produces false signals chopping around VWAP - this
    flags that condition so the pipeline can force NO TRADE instead of
    acting on a setup fired during chop (spec section 13)."""
    if len(df) < cross_window + 2:
        return False
    close = df["close"].iloc[-cross_window:]
    v = vwap.iloc[-cross_window:]
    crosses = int(((close > v) != (close.shift(1) > v)).sum())
    vwap_choppy = crosses >= max_crosses

    ema_spread = abs(ema9.iloc[-1] - ema50.iloc[-1])
    ema_compressed = bool(ema_spread < atr_v.iloc[-1] * compression_atr_mult)

    recent_atr = atr_v.iloc[-60:] if len(atr_v) >= 60 else atr_v
    atr_rank = float((recent_atr < atr_v.iloc[-1]).mean()) if len(recent_atr) else 1.0
    low_vol = atr_rank < low_atr_percentile

    return bool(vwap_choppy or (ema_compressed and low_vol))


def extended_filter(rsi_v: pd.Series, close: pd.Series, vwap: pd.Series, ema21: pd.Series,
                     atr_v: pd.Series, side: str, overbought: float = 75.0,
                     oversold: float = 25.0, distance_atr_mult: float = 2.0) -> bool:
    """'Don't chase' - if the move is already this extended, wait for a
    pullback/retest instead of a fresh entry (spec section 20)."""
    if pd.isna(rsi_v.iloc[-1]) or pd.isna(vwap.iloc[-1]):
        return False
    dist_from_vwap = abs(close.iloc[-1] - vwap.iloc[-1])
    dist_from_ema = abs(close.iloc[-1] - ema21.iloc[-1])
    far = (dist_from_vwap > atr_v.iloc[-1] * distance_atr_mult
           or dist_from_ema > atr_v.iloc[-1] * distance_atr_mult)
    if side == "CE":
        return bool(rsi_v.iloc[-1] >= overbought and far)
    return bool(rsi_v.iloc[-1] <= oversold and far)


def confirm_1m(df_1m: pd.DataFrame, direction: str) -> dict:
    """1-minute EXECUTION confirmation only - refines entry timing after a
    5-minute setup, never invents a new signal on its own (spec section 6).
    Scored 0-4 (candle direction, higher-low/lower-high, RSI side, MACD side,
    volume expansion) - not all conditions are required (spec explicitly
    says don't make this too restrictive)."""
    if len(df_1m) < 30:
        return {"confirmed": False, "score": 0, "max_score": 4}

    close = df_1m["close"]
    rsi_v = rsi(close)
    macd_line, signal_line, _hist = macd(close)
    vol_sma = df_1m["volume"].rolling(20).mean()

    bull_candle = close.iloc[-1] > df_1m["open"].iloc[-1]
    bear_candle = close.iloc[-1] < df_1m["open"].iloc[-1]
    higher_low = df_1m["low"].iloc[-1] > df_1m["low"].iloc[-2]
    lower_high = df_1m["high"].iloc[-1] < df_1m["high"].iloc[-2]
    vol_expand = bool(pd.isna(vol_sma.iloc[-1]) or df_1m["volume"].iloc[-1] >= vol_sma.iloc[-1])

    score = 0
    if direction == "long":
        score += 1 if bull_candle else 0
        score += 1 if higher_low else 0
        score += 1 if (not pd.isna(rsi_v.iloc[-1]) and rsi_v.iloc[-1] > 50) else 0
        score += 1 if (not pd.isna(macd_line.iloc[-1]) and macd_line.iloc[-1] > signal_line.iloc[-1]) else 0
    else:
        score += 1 if bear_candle else 0
        score += 1 if lower_high else 0
        score += 1 if (not pd.isna(rsi_v.iloc[-1]) and rsi_v.iloc[-1] < 50) else 0
        score += 1 if (not pd.isna(macd_line.iloc[-1]) and macd_line.iloc[-1] < signal_line.iloc[-1]) else 0

    return {"confirmed": score >= 2, "score": score, "max_score": 4, "volume_expansion": vol_expand}


def compute_gbb_signal(df_5m: pd.DataFrame, df_1m: pd.DataFrame, min_grade_score: float = 40.0) -> dict:
    """Top-level entry point. Returns a dict compatible with what
    orchestrator.py's GBB branch needs to feed into the rest of the
    existing pipeline (side/score/max_score/votes/state/structure levels)."""
    if len(df_5m) < 60:
        return {"side": None, "state": STATE_NO_SETUP, "grade": "NO TRADE", "score": 0,
                "max_score": 10, "confidence_pct": 0, "votes": {}, "structure_stop": None,
                "confirm_1m": None}

    close = df_5m["close"]
    vwap = session_vwap(df_5m)
    ema9, ema21, ema50 = ema(close, 9), ema(close, 21), ema(close, 50)
    rsi_v = rsi(close)
    macd_line, signal_line, hist = macd(close)
    atr_v = atr_series(df_5m).bfill()
    vol_sma = df_5m["volume"].rolling(20).mean()
    vol_ok = bool(pd.isna(vol_sma.iloc[-1]) or df_5m["volume"].iloc[-1] >= vol_sma.iloc[-1])

    vwap_sig = vwap_reclaim(df_5m, vwap, vol_ok=vol_ok)
    ema_sig = ema_pullback(df_5m, ema9, ema21, ema50, vol_ok=vol_ok)
    sweep_sig = liquidity_sweep(df_5m, vol_ok=vol_ok)
    brt = break_and_retest(df_5m)

    # Direction: Break & Retest is the highest-priority setup (spec
    # section 3) - it decides direction if it's live at all; otherwise
    # fall back to whichever of VWAP/EMA fired.
    side = None
    if brt["state"] in (STATE_CONFIRMED, STATE_WAITING_RETEST, STATE_RETEST):
        side = "CE" if brt["direction"] == "long" else "PE"
    elif vwap_sig["long"] or ema_sig["long"]:
        side = "CE"
    elif vwap_sig["short"] or ema_sig["short"]:
        side = "PE"

    if side is None:
        # No live setup fired in either direction - side stays None (no
        # trade), but give the caller a best-effort directional BIAS from
        # close-vs-VWAP so a placeholder side downstream (e.g. orchestrator
        # needing a strike to display) reflects actual current price
        # action instead of an arbitrary hardcoded default.
        vwap_last = vwap.iloc[-1]
        bias = None
        if not pd.isna(vwap_last):
            bias = "CE" if close.iloc[-1] > vwap_last else "PE" if close.iloc[-1] < vwap_last else None
        return {"side": None, "bias": bias, "state": STATE_NO_SETUP, "grade": "NO TRADE", "score": 0,
                "max_score": 10, "confidence_pct": 0, "votes": {}, "structure_stop": None,
                "confirm_1m": None}

    is_chop = chop_filter(df_5m, vwap, ema9, ema21, ema50, atr_v)
    if is_chop:
        return {"side": side, "state": STATE_CHOP, "grade": "NO TRADE", "score": 0,
                "max_score": 10, "confidence_pct": 0, "votes": {"chop": True}, "structure_stop": None,
                "confirm_1m": None}

    if extended_filter(rsi_v, close, vwap, ema21, atr_v, side):
        return {"side": side, "state": STATE_EXTENDED, "grade": "EXTENDED — AVOID CHASING",
                "score": 0, "max_score": 10, "confidence_pct": 0, "votes": {"extended": True},
                "structure_stop": None, "confirm_1m": None}

    # ---- Weighted scoring (spec section 14) ----
    votes = {}
    score = 0.0
    if brt["state"] == STATE_CONFIRMED:
        votes["break_retest"] = True
        score += 2
    elif brt["state"] == STATE_WAITING_RETEST:
        votes["break_retest_forming"] = True
    is_long = side == "CE"
    # session_vwap() deliberately replaces any zero-cumulative-volume bar
    # with pd.NA (not NaN) to avoid a divide-by-zero - that's correct there,
    # but it means vwap.iloc[-1] can legitimately BE pd.NA (a zero-volume
    # bar, a feed gap, or very early in the session before volume has
    # accumulated). Using it directly in a boolean `or`/`==` expression
    # forces Python to coerce pd.NA to True/False to short-circuit the `or`,
    # which pd.NA refuses to do ("boolean value of NA is ambiguous") - this
    # was crashing the whole GBB signal instead of just skipping that one
    # vote. Guard it the same way rsi/macd already are below.
    vwap_last = vwap.iloc[-1]
    above_vwap_matches_side = (
        bool((close.iloc[-1] > vwap_last) == is_long) if not pd.isna(vwap_last) else False
    )
    votes["vwap"] = bool(vwap_sig["long" if is_long else "short"] or above_vwap_matches_side)
    score += 2 if votes["vwap"] else 0
    votes["ema"] = bool((ema9.iloc[-1] > ema21.iloc[-1]) == is_long and (ema21.iloc[-1] > ema50.iloc[-1]) == is_long)
    score += 2 if votes["ema"] else 0
    votes["rsi"] = bool((rsi_v.iloc[-1] > 50) == is_long) if not pd.isna(rsi_v.iloc[-1]) else False
    score += 1 if votes["rsi"] else 0
    votes["macd"] = bool((macd_line.iloc[-1] > signal_line.iloc[-1]) == is_long) if not pd.isna(macd_line.iloc[-1]) else False
    score += 1 if votes["macd"] else 0
    votes["volume"] = vol_ok
    score += 1 if vol_ok else 0
    votes["liquidity_sweep"] = bool(sweep_sig["long" if is_long else "short"])
    score += 1 if votes["liquidity_sweep"] else 0

    max_score = 9.0  # 2+2+2+1+1+1 (ORB's +1 not included - not ported in v1)
    confidence_pct = round(score / max_score * 100)
    grade = ("A+" if confidence_pct >= 85 else "A" if confidence_pct >= 70 else
             "B" if confidence_pct >= 55 else "C" if confidence_pct >= min_grade_score else "NO TRADE")

    conf1m = confirm_1m(df_1m, "long" if is_long else "short")

    # Overall state for display: CONFIRMED only once both the 5m setup AND
    # the 1m execution timing agree - a strong 5m read with a weak 1m
    # candle stays at RETEST/WAITING, not a false CONFIRMED.
    state = brt["state"] if brt["state"] != STATE_NO_SETUP else (
        STATE_FORMING if (vwap_sig["forming"] or ema_sig["forming"]) else STATE_BREAKOUT
    )
    if state == STATE_CONFIRMED and not conf1m["confirmed"]:
        state = STATE_RETEST  # 5m confirmed the retest, 1m hasn't confirmed entry timing yet

    structure_stop = brt.get("stop")

    return {
        "side": side,
        "state": state,
        "grade": grade if grade != "NO TRADE" or state == STATE_CONFIRMED else "NO TRADE",
        "score": round(score, 1),
        "max_score": max_score,
        "confidence_pct": confidence_pct,
        "votes": votes,
        "break_retest": brt,
        "structure_stop": structure_stop,
        "confirm_1m": conf1m,
        "vwap_value": round(float(vwap.iloc[-1]), 2) if not pd.isna(vwap.iloc[-1]) else None,
    }
