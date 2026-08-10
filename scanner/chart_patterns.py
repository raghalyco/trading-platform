"""
Classic chart patterns for Previous Support Bounce.

Detects (bullish / bounce-oriented):
  - Ascending / descending / symmetrical triangles
  - Bull flag & pennant (after an up-pole)
  - Rising wedge (touch/bounce at rising support)
  - Cup and handle

Falling wedge, double/triple bottom, trendlines stay in support_bounce.py.
"""
from __future__ import annotations

from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd

import config
import support_bounce as sb


def _pivots(df: pd.DataFrame) -> tuple[list, list]:
    max_piv = getattr(config, "SUPPORT_BOUNCE_PATTERN_MAX_PIVOTS", 8)
    highs = sb._major_swing_highs(df)[-max_piv:]
    lows = sb._major_swing_lows(df)[-max_piv:]
    return highs, lows


def _count_line_touches(
    pivots: list[tuple[int, float]],
    slope: float,
    intercept: float,
    atr: float,
    start_i: int,
    end_i: int,
) -> int:
    n = 0
    for idx, price in pivots:
        if idx < start_i or idx > end_i:
            continue
        lvl = sb._line_at(slope, intercept, idx)
        if abs(price - lvl) <= sb._touch_band(lvl, atr):
            n += 1
    return n


def _two_line_event(
    df: pd.DataFrame,
    atr: float,
    *,
    support_type: str,
    kind: str,
    lslope: float,
    lintercept: float,
    uslope: float,
    uintercept: float,
    start_i: int,
    end_i: int,
    lower_touches: int,
    upper_touches: int,
    quality_bonus: float = 0.0,
    allow_breakout: bool = True,
) -> Optional[dict]:
    """Shared bounce/breakout gate for converging / channel patterns."""
    n = len(df)
    if start_i >= end_i or end_i >= n:
        return None

    u0 = sb._line_at(uslope, uintercept, start_i)
    l0 = sb._line_at(lslope, lintercept, start_i)
    u1 = sb._line_at(uslope, uintercept, end_i)
    l1 = sb._line_at(lslope, lintercept, end_i)
    if u0 <= l0 or u1 <= l1:
        return None

    if sb._line_broken(df, lslope, lintercept, atr, end_i + 1):
        return None

    # Pattern must still be "live" (last anchor within bounce lookback).
    if (n - 1 - end_i) > config.SUPPORT_BOUNCE_BOUNCE_BARS:
        return None

    support_series = pd.Series(
        [sb._line_at(lslope, lintercept, i) for i in range(n)],
        index=df.index,
    )
    resist_series = pd.Series(
        [sb._line_at(uslope, uintercept, i) for i in range(n)],
        index=df.index,
    )
    support_now = float(support_series.iloc[-1])
    resist_now = float(resist_series.iloc[-1])
    if support_now <= 0 or resist_now <= support_now * 0.995:
        return None

    touching, bounce, dist_pct, probe_age = sb._recent_probe_and_bounce(
        df, support_series, atr
    )
    close = float(df["close"].iloc[-1])

    breakout = False
    breakout_i = -1
    if allow_breakout:
        look = max(0, n - config.SUPPORT_BOUNCE_BOUNCE_BARS)
        for i in range(look, n):
            r = float(resist_series.iloc[i])
            c = float(df["close"].iloc[i])
            o = float(df["open"].iloc[i])
            if c > r + atr * 0.05 and c >= o:
                breakout = True
                breakout_i = i
                break
        if close > resist_now:
            breakout = True
            if breakout_i < 0:
                breakout_i = n - 1

    if not touching and not bounce and not breakout:
        return None
    if not breakout:
        if dist_pct < -config.SUPPORT_BOUNCE_TOUCH_PCT:
            return None
        if dist_pct > config.SUPPORT_BOUNCE_MAX_DIST_PCT and not bounce:
            return None
        if bounce and dist_pct > config.SUPPORT_BOUNCE_MAX_BOUNCE_DIST_PCT:
            return None

    retest = False
    retest_i = -1
    volume_ratio = 0.0
    close_strength = 0.5
    dist_from_breakout_pct = 0.0
    if breakout:
        dist_from_breakout_pct = (close - resist_now) / resist_now * 100.0
        retest, retest_i = sb._detect_retest_after_breakout(df, resist_now, breakout_i, atr)
        volume_ratio = sb._volume_ratio_at(df, breakout_i)
        close_strength = sb._close_strength_at(df, breakout_i)
        # Already moved too far past the breakout with no re-test to anchor
        # a stop against — avoid surfacing it as a fresh, tradeable setup.
        if not retest and dist_from_breakout_pct > config.SUPPORT_BOUNCE_MAX_BREAKOUT_DIST_PCT:
            return None

    if breakout:
        event = f"{support_type} breakout"
        dist_pct = (close - support_now) / support_now * 100.0
    else:
        event = (
            "Touching support" if touching and not bounce else
            ("Bullish bounce" if bounce and not touching else "Touch + bounce")
        )

    quality = (
        (upper_touches + lower_touches) * 7
        + (12 if breakout else 0)
        + (8 if bounce else 0)
        + (5 if touching else 0)
        + max(0, 10 - abs(dist_pct))
        + min((end_i - start_i) / 25.0, 8)
        + quality_bonus
        + (14 if retest else 0)
        + min(volume_ratio, 4.0) * 4
        + (8 if close_strength >= config.SUPPORT_BOUNCE_MIN_CLOSE_STRENGTH else 0)
    )
    return {
        "support_type": support_type,
        "support_price": round(support_now, 2),
        "support_bar_index": end_i,
        "support_age_bars": n - 1 - end_i,
        "distance_from_support_pct": round(dist_pct, 2),
        "distance_from_breakout_pct": round(dist_from_breakout_pct, 2) if breakout else None,
        "touch_count": upper_touches + lower_touches,
        "event": event,
        "touching": touching,
        "bounce": bounce or breakout,
        "probe_age_bars": probe_age,
        "retest": retest,
        "retest_bar_index": retest_i if retest_i >= 0 else None,
        "volume_ratio": volume_ratio if breakout else None,
        "close_strength": close_strength if breakout else None,
        "quality": quality,
        "geometry": {
            "kind": kind,
            "support": {"slope": lslope, "intercept": lintercept, "i_start": start_i},
            "resistance": {"slope": uslope, "intercept": uintercept, "i_start": start_i},
            "support_label": "Support",
            "resistance_label": "Resistance",
        },
    }


def _iter_boundary_pairs(
    highs: list[tuple[int, float]],
    lows: list[tuple[int, float]],
    min_sep: int,
):
    for (hi1, hp1), (hi2, hp2) in combinations(highs, 2):
        if hi2 <= hi1 or (hi2 - hi1) < min_sep:
            continue
        uslope, uintercept = sb._fit_line(hi1, hp1, hi2, hp2)
        for (li1, lp1), (li2, lp2) in combinations(lows, 2):
            if li2 <= li1 or (li2 - li1) < min_sep:
                continue
            if li2 < hi1 or li1 > hi2:
                continue
            lslope, lintercept = sb._fit_line(li1, lp1, li2, lp2)
            start_i = min(hi1, li1)
            end_i = max(hi2, li2)
            yield (hi1, hp1, hi2, hp2, uslope, uintercept,
                   li1, lp1, li2, lp2, lslope, lintercept, start_i, end_i)


def find_triangles(df: pd.DataFrame, atr: float) -> list[dict]:
    highs, lows = _pivots(df)
    if len(highs) < 2 or len(lows) < 2:
        return []

    min_sep = getattr(config, "SUPPORT_BOUNCE_PATTERN_MIN_SEP", 18)
    flat = getattr(config, "SUPPORT_BOUNCE_TRIANGLE_FLAT_SLOPE", 0.00012)
    hits = []

    for (hi1, hp1, hi2, hp2, uslope, uintercept,
         li1, lp1, li2, lp2, lslope, lintercept, start_i, end_i) in _iter_boundary_pairs(
        highs, lows, min_sep
    ):
        # Converging width.
        w0 = sb._line_at(uslope, uintercept, start_i) - sb._line_at(lslope, lintercept, start_i)
        w1 = sb._line_at(uslope, uintercept, end_i) - sb._line_at(lslope, lintercept, end_i)
        if w0 <= 0 or w1 >= w0 * 0.95:
            continue

        mid_p = (hp1 + lp1) / 2.0
        abs_flat = mid_p * flat

        label = None
        kind = None
        bonus = 10.0
        # Ascending: flat-ish resistance, rising support.
        if abs(uslope) <= abs_flat and lslope > mid_p * 0.0001:
            label, kind = "Ascending triangle", "ascending_triangle"
            bonus = 12.0
        # Descending: flat-ish support, falling resistance.
        elif abs(lslope) <= abs_flat and uslope < -mid_p * 0.0001:
            label, kind = "Descending triangle", "descending_triangle"
            bonus = 10.0
        # Symmetrical: falling highs + rising lows.
        elif uslope < -mid_p * 0.00008 and lslope > mid_p * 0.00008:
            label, kind = "Symmetrical triangle", "symmetrical_triangle"
            bonus = 11.0
        else:
            continue

        ut = _count_line_touches(highs, uslope, uintercept, atr, start_i, end_i)
        lt = _count_line_touches(lows, lslope, lintercept, atr, start_i, end_i)
        if ut < 2 or lt < 2:
            continue

        hit = _two_line_event(
            df, atr,
            support_type=label,
            kind=kind,
            lslope=lslope, lintercept=lintercept,
            uslope=uslope, uintercept=uintercept,
            start_i=start_i, end_i=end_i,
            lower_touches=lt, upper_touches=ut,
            quality_bonus=bonus,
        )
        if hit:
            hits.append(hit)
    return hits


def find_rising_wedges(df: pd.DataFrame, atr: float) -> list[dict]:
    """Rising wedge: both boundaries up, converging (often late-trend; we take support bounce)."""
    highs, lows = _pivots(df)
    if len(highs) < 2 or len(lows) < 2:
        return []

    min_sep = config.SUPPORT_BOUNCE_WEDGE_MIN_SEP
    hits = []
    for (hi1, hp1, hi2, hp2, uslope, uintercept,
         li1, lp1, li2, lp2, lslope, lintercept, start_i, end_i) in _iter_boundary_pairs(
        highs, lows, min_sep
    ):
        mid_p = (hp1 + lp1) / 2.0
        if lslope <= mid_p * 0.0001 or uslope <= mid_p * 0.0001:
            continue
        # Upper steeper than lower → converging rising wedge.
        if uslope <= lslope:
            continue
        w0 = sb._line_at(uslope, uintercept, start_i) - sb._line_at(lslope, lintercept, start_i)
        w1 = sb._line_at(uslope, uintercept, end_i) - sb._line_at(lslope, lintercept, end_i)
        if w0 <= 0 or w1 >= w0 * 0.92:
            continue

        ut = _count_line_touches(highs, uslope, uintercept, atr, start_i, end_i)
        lt = _count_line_touches(lows, lslope, lintercept, atr, start_i, end_i)
        if ut < 2 or lt < 2:
            continue

        hit = _two_line_event(
            df, atr,
            support_type="Rising wedge",
            kind="rising_wedge",
            lslope=lslope, lintercept=lintercept,
            uslope=uslope, uintercept=uintercept,
            start_i=start_i, end_i=end_i,
            lower_touches=lt, upper_touches=ut,
            quality_bonus=9.0,
            allow_breakout=False,  # rising-wedge upside breakout is less classic; prefer support bounce
        )
        if hit:
            hit["geometry"]["support_label"] = "Wedge support"
            hit["geometry"]["resistance_label"] = "Wedge resistance"
            hits.append(hit)
    return hits


def _find_pole_end(df: pd.DataFrame, atr: float) -> Optional[int]:
    """Index where a sharp bullish pole likely ended (start of flag/pennant)."""
    n = len(df)
    look = min(n - 30, getattr(config, "SUPPORT_BOUNCE_FLAG_POLE_LOOKBACK", 60))
    if look < 15:
        return None
    min_pole_pct = getattr(config, "SUPPORT_BOUNCE_FLAG_POLE_MIN_PCT", 8.0)
    best_i = None
    best_move = 0.0
    close = df["close"].astype(float)
    for end in range(n - 25, n - 8):
        start = max(0, end - look)
        window = close.iloc[start:end + 1]
        if len(window) < 10:
            continue
        lo = float(window.min())
        hi = float(window.max())
        if lo <= 0:
            continue
        move = (hi - lo) / lo * 100.0
        # Pole should finish near the high of the window.
        if float(close.iloc[end]) < hi - atr * 0.5:
            continue
        if move >= min_pole_pct and move > best_move:
            best_move = move
            best_i = end
    return best_i


def find_flags_and_pennants(df: pd.DataFrame, atr: float) -> list[dict]:
    """Bull flag (parallel drift) / pennant (tiny triangle) after an up-pole."""
    pole_end = _find_pole_end(df, atr)
    if pole_end is None:
        return []

    highs = [h for h in sb._major_swing_highs(df) if h[0] >= pole_end - 2]
    lows = [l for l in sb._major_swing_lows(df) if l[0] >= pole_end - 2]
    # Also allow recent pivots inside consolidation only.
    highs = [h for h in highs if h[0] <= len(df) - 1][-6:]
    lows = [l for l in lows if l[0] <= len(df) - 1][-6:]
    if len(highs) < 2 or len(lows) < 2:
        return []

    min_sep = max(8, getattr(config, "SUPPORT_BOUNCE_FLAG_MIN_SEP", 8))
    hits = []
    for (hi1, hp1, hi2, hp2, uslope, uintercept,
         li1, lp1, li2, lp2, lslope, lintercept, start_i, end_i) in _iter_boundary_pairs(
        highs, lows, min_sep
    ):
        if start_i < pole_end - 5:
            continue
        # Consolidation should be shorter than the pole lookback.
        if (end_i - start_i) > getattr(config, "SUPPORT_BOUNCE_FLAG_MAX_BARS", 40):
            continue

        mid_p = (hp1 + lp1) / 2.0
        w0 = sb._line_at(uslope, uintercept, start_i) - sb._line_at(lslope, lintercept, start_i)
        w1 = sb._line_at(uslope, uintercept, end_i) - sb._line_at(lslope, lintercept, end_i)
        if w0 <= 0:
            continue

        slope_diff = abs(uslope - lslope)
        parallel = slope_diff <= mid_p * 0.00025
        both_down = uslope < mid_p * 0.00005 and lslope < mid_p * 0.00005
        converging = w1 < w0 * 0.9 and uslope < lslope

        label = kind = None
        bonus = 10.0
        if parallel and both_down and w1 >= w0 * 0.7:
            label, kind, bonus = "Bull flag", "bull_flag", 13.0
        elif converging and uslope < -mid_p * 0.00005 and lslope > -mid_p * 0.00015:
            # Tight triangle after pole = pennant (lower may be flat-ish or slight up).
            label, kind, bonus = "Bull pennant", "bull_pennant", 12.0
        else:
            continue

        ut = _count_line_touches(highs, uslope, uintercept, atr, start_i, end_i)
        lt = _count_line_touches(lows, lslope, lintercept, atr, start_i, end_i)
        if ut < 2 or lt < 2:
            continue

        hit = _two_line_event(
            df, atr,
            support_type=label,
            kind=kind,
            lslope=lslope, lintercept=lintercept,
            uslope=uslope, uintercept=uintercept,
            start_i=start_i, end_i=end_i,
            lower_touches=lt, upper_touches=ut,
            quality_bonus=bonus,
        )
        if hit:
            hit["geometry"]["support_label"] = "Flag support" if "flag" in kind else "Pennant support"
            hit["geometry"]["resistance_label"] = (
                "Flag resistance" if "flag" in kind else "Pennant resistance"
            )
            hits.append(hit)
    return hits


def find_cup_and_handle(df: pd.DataFrame, atr: float) -> list[dict]:
    """Cup & handle: rounded recovery then shallow handle pullback + bounce."""
    n = len(df)
    min_bars = getattr(config, "SUPPORT_BOUNCE_CUP_MIN_BARS", 40)
    max_bars = getattr(config, "SUPPORT_BOUNCE_CUP_MAX_BARS", 160)
    if n < min_bars + 15:
        return []

    hits = []
    # Evaluate a few window sizes ending near now.
    for width in (max_bars, (min_bars + max_bars) // 2, min_bars):
        if n < width + 10:
            continue
        start = n - width
        seg = df.iloc[start:]
        lows = seg["low"].astype(float)
        highs = seg["high"].astype(float)
        closes = seg["close"].astype(float)

        cup_rel = int(lows.values.argmin())
        # Cup bottom should sit in the middle half of the window.
        if cup_rel < width * 0.2 or cup_rel > width * 0.75:
            continue
        cup_i = start + cup_rel
        cup_low = float(lows.iloc[cup_rel])

        left = highs.iloc[:cup_rel]
        right = highs.iloc[cup_rel + 1:]
        if len(left) < 5 or len(right) < 5:
            continue
        left_rim = float(left.max())
        left_rim_i = start + int(left.values.argmax())
        right_rim = float(right.max())
        right_rim_rel = cup_rel + 1 + int(right.values.argmax())
        right_rim_i = start + right_rim_rel

        # Rims roughly equal (classic cup).
        rim = (left_rim + right_rim) / 2.0
        if abs(left_rim - right_rim) / rim > 0.06:
            continue
        depth = rim - cup_low
        if depth < max(atr * 2.0, rim * 0.06):
            continue

        # Handle: pullback after right rim, shallow vs cup depth.
        if right_rim_i >= n - 3:
            continue
        handle = df.iloc[right_rim_i:]
        if len(handle) < 3:
            continue
        handle_low = float(handle["low"].min())
        handle_low_i = right_rim_i + int(handle["low"].astype(float).values.argmin())
        handle_depth = rim - handle_low
        if handle_depth <= 0:
            continue
        # Handle typically 10–50% of cup depth, not below cup midpoint.
        if handle_depth > depth * 0.55 or handle_depth < depth * 0.08:
            continue
        if handle_low < cup_low + depth * 0.35:
            continue

        support_series = pd.Series(handle_low, index=df.index)
        touching, bounce, dist_pct, probe_age = sb._recent_probe_and_bounce(
            df, support_series, atr
        )
        if not bounce and not touching:
            continue
        if dist_pct < -config.SUPPORT_BOUNCE_TOUCH_PCT:
            continue
        max_up = getattr(config, "SUPPORT_BOUNCE_CUP_MAX_UP_PCT", 8.0)
        if dist_pct > max_up:
            continue
        # Prefer handle still recent.
        if (n - 1 - handle_low_i) > config.SUPPORT_BOUNCE_BOUNCE_BARS:
            continue

        # Broken if closes under handle support.
        if sb._line_broken(df, 0.0, handle_low, atr, handle_low_i + 1):
            continue

        event = "Touching support" if touching and not bounce else (
            "Bullish bounce" if bounce and not touching else "Touch + bounce"
        )
        quality = (
            28
            + (10 if bounce else 0)
            + (5 if touching else 0)
            + max(0, 10 - abs(dist_pct))
            + min(width / 40.0, 8)
        )
        hits.append({
            "support_type": "Cup and handle",
            "support_price": round(handle_low, 2),
            "support_bar_index": handle_low_i,
            "support_age_bars": n - 1 - handle_low_i,
            "distance_from_support_pct": round(dist_pct, 2),
            "touch_count": 2,
            "event": event,
            "touching": touching,
            "bounce": bounce,
            "probe_age_bars": probe_age,
            "quality": quality,
            "geometry": {
                "kind": "cup_and_handle",
                "level": handle_low,
                "rim": round(rim, 2),
                "cup_low": round(cup_low, 2),
                "i_start": left_rim_i,
                "cup_i": cup_i,
                "handle_i": handle_low_i,
            },
        })
        break  # one cup window is enough
    return hits


def collect_classic_patterns(df: pd.DataFrame, atr: float) -> list[dict]:
    """All classic patterns implemented in this module (best per type)."""
    out: list[dict] = []
    out.extend(find_triangles(df, atr))
    out.extend(find_rising_wedges(df, atr))
    out.extend(find_flags_and_pennants(df, atr))
    out.extend(find_cup_and_handle(df, atr))
    # Keep the strongest candidate per support_type to limit noise.
    best: dict[str, dict] = {}
    for hit in out:
        key = hit.get("support_type") or ""
        prev = best.get(key)
        if prev is None or hit.get("quality", 0) > prev.get("quality", 0):
            best[key] = hit
    return list(best.values())
