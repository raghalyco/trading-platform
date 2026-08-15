"""
Intraday support/resistance zone detection - the horizontal lines a
discretionary trader draws by eye on a 5-min chart (recent swing highs/
lows that price has reacted to more than once), computed automatically.
Also computes each day's high/low (PDH/PDL - Previous Day High/Low - a
classic intraday reference level, plus today's running high/low).

retest.py's check_retest() has always accepted an `sr_zones` parameter,
but nothing in the codebase ever populated it - this module is what
finally fills that gap.
"""
from __future__ import annotations

import pandas as pd
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _confirmed_pivot_highs(df: pd.DataFrame, length: int) -> list[tuple[int, float]]:
    high = df["high"].astype(float)
    ph = high.shift(length)
    is_ph = pd.Series(True, index=df.index)
    for i in range(0, length + 1):
        if i == length:
            continue
        is_ph &= ph >= high.shift(i)
    for i in range(length + 1, 2 * length + 1):
        is_ph &= ph > high.shift(i)
    out = []
    for i in range(len(df)):
        if bool(is_ph.iloc[i]) and pd.notna(ph.iloc[i]):
            pivot_i = i - length
            if pivot_i >= 0:
                out.append((pivot_i, float(ph.iloc[i])))
    return out


def _confirmed_pivot_lows(df: pd.DataFrame, length: int) -> list[tuple[int, float]]:
    low = df["low"].astype(float)
    pl = low.shift(length)
    is_pl = pd.Series(True, index=df.index)
    for i in range(0, length + 1):
        if i == length:
            continue
        is_pl &= pl <= low.shift(i)
    for i in range(length + 1, 2 * length + 1):
        is_pl &= pl < low.shift(i)
    out = []
    for i in range(len(df)):
        if bool(is_pl.iloc[i]) and pd.notna(pl.iloc[i]):
            pivot_i = i - length
            if pivot_i >= 0:
                out.append((pivot_i, float(pl.iloc[i])))
    return out


def _cluster_mixed(highs: list[tuple[int, float]], lows: list[tuple[int, float]],
                    tolerance_pct: float, min_touches: int) -> list[dict]:
    """Clusters swing highs AND lows together into "key levels" - a level
    touched by both (e.g. support that later got broken and is now being
    retested as resistance, like NIFTY ~24,354-24,358 on 14-17 Aug) shows
    up as ONE zone with role="flip", instead of being split into a
    same-level "support" entry and "resistance" entry that hide the fact
    they're the same real level."""
    tagged = [(i, p, "high") for i, p in highs] + [(i, p, "low") for i, p in lows]
    if not tagged:
        return []
    tagged.sort(key=lambda t: t[1])
    clusters: list[list[tuple[int, float, str]]] = []
    for idx, price, kind in tagged:
        placed = False
        for cluster in clusters:
            level = sum(p for _, p, _ in cluster) / len(cluster)
            if abs(price - level) / level * 100 <= tolerance_pct:
                cluster.append((idx, price, kind))
                placed = True
                break
        if not placed:
            clusters.append([(idx, price, kind)])

    zones = []
    for cluster in clusters:
        if len(cluster) < min_touches:
            continue
        prices = sorted(p for _, p, _ in cluster)
        level = prices[len(prices) // 2]
        kinds = {k for _, _, k in cluster}
        role = "flip" if kinds == {"high", "low"} else ("resistance" if "high" in kinds else "support")
        zones.append({
            "level": round(level, 2),
            "role": role,
            "touches": len(cluster),
            "last_touch_bar": max(i for i, _, _ in cluster),
        })
    return zones


def _day_levels(df_5m: pd.DataFrame) -> list[dict]:
    """Per-calendar-day (IST) high/low - PDH/PDL for prior days, running
    high/low for today. Classic intraday reference levels independent of
    the swing-pivot zones above."""
    df = df_5m.copy()
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    df["_date"] = ts.dt.date

    dates = sorted(df["_date"].unique())
    today = dates[-1] if dates else None
    levels = []
    for d in dates:
        day_df = df[df["_date"] == d]
        is_today = d == today
        label_high = "Today's High" if is_today else "Prev Day High"
        label_low = "Today's Low" if is_today else "Prev Day Low"
        levels.append({"level": round(float(day_df["high"].max()), 2),
                        "role": "resistance", "label": label_high, "date": str(d)})
        levels.append({"level": round(float(day_df["low"].min()), 2),
                        "role": "support", "label": label_low, "date": str(d)})
    return levels


def find_intraday_zones(df_5m: pd.DataFrame, pivot_length: int = 3,
                        touch_tolerance_pct: float = 0.15, min_touches: int = 2) -> dict:
    """
    df_5m: 5-minute OHLCV, most recent bar last. Typically fed 2-5 days of
    history so multi-day zones (like a level touched Monday AND Thursday)
    are found, not just today's.

    Returns swing-pivot key_levels (support/resistance/flip), day_levels
    (PDH/PDL + today's running high/low), a merged all_levels list (what
    should feed check_retest's sr_zones param), and the nearest
    resistance/support to current price from that merged set - only the
    zone right above/below spot is a live retest candidate.
    """
    empty = {"key_levels": [], "day_levels": [], "all_levels": [],
              "nearest_resistance": None, "nearest_support": None, "price": None}
    if len(df_5m) < pivot_length * 2 + 5:
        return empty

    df = df_5m.reset_index(drop=True)
    highs = _confirmed_pivot_highs(df, pivot_length)
    lows = _confirmed_pivot_lows(df, pivot_length)
    key_levels = _cluster_mixed(highs, lows, touch_tolerance_pct, min_touches)
    key_levels.sort(key=lambda z: z["level"])

    day_levels = _day_levels(df)

    price = float(df["close"].iloc[-1])
    all_levels = key_levels + day_levels
    all_levels.sort(key=lambda z: z["level"])

    above = [z for z in all_levels if z["level"] >= price]
    below = [z for z in all_levels if z["level"] <= price]
    nearest_resistance = min(above, key=lambda z: z["level"] - price) if above else None
    nearest_support = max(below, key=lambda z: z["level"]) if below else None

    return {
        "key_levels": key_levels,
        "day_levels": day_levels,
        "all_levels": all_levels,
        "nearest_resistance": nearest_resistance,
        "nearest_support": nearest_support,
        "price": round(price, 2),
    }
