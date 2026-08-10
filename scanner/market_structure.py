"""
Shared swing-structure (HH / HL / LH / LL) labeling for chart pages.

Confirmed swing pivots (same lag-based algorithm the pattern detectors use),
each pivot labeled relative to the previous pivot of the same type: a swing
high is a Higher High (HH) if it tops the prior swing high, else a Lower
High (LH); a swing low is a Higher Low (HL) if it sits above the prior swing
low, else a Lower Low (LL). This is the classic market-structure zigzag
(HH/HL in an uptrend, LH/LL in a downtrend) drawn on every chart page so
trend structure is visible at a glance, independent of whichever
support/resistance pattern that chart is otherwise showing.
"""
from __future__ import annotations

import pandas as pd


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


def label_structure_points(df: pd.DataFrame, pivot_length: int) -> list[dict]:
    """Confirmed swing highs/lows in time order, each tagged HH/LH/HL/LL
    relative to the previous pivot of the same type (H/L for the first of
    each kind, since there's nothing yet to compare it to)."""
    if len(df) < pivot_length * 3:
        return []
    highs = _confirmed_pivot_highs(df, pivot_length)
    lows = _confirmed_pivot_lows(df, pivot_length)
    points = [(i, p, "high") for i, p in highs] + [(i, p, "low") for i, p in lows]
    points.sort(key=lambda x: x[0])

    labeled = []
    last_high: float | None = None
    last_low: float | None = None
    for i, p, kind in points:
        if kind == "high":
            if last_high is None:
                label = "H"
            else:
                label = "HH" if p > last_high else "LH"
            last_high = p
        else:
            if last_low is None:
                label = "L"
            else:
                label = "HL" if p > last_low else "LL"
            last_low = p
        labeled.append({"bar_index": i, "price": round(p, 2), "kind": kind, "label": label})
    return labeled


def structure_chart_extras(df: pd.DataFrame, pivot_length: int) -> tuple[list[dict], list[dict]]:
    """(markers, zigzag line points) ready to hand to a Lightweight Charts
    page — markers go on the candlestick series, line points on a dashed
    overlay line series connecting the pivots in sequence."""
    points = label_structure_points(df, pivot_length)
    n = len(df)
    markers = []
    line_points = []
    for pt in points:
        i = pt["bar_index"]
        if i < 0 or i >= n:
            continue
        t = pd.to_datetime(df["date"].iloc[i]).strftime("%Y-%m-%d")
        if pt["label"] in ("HH", "HL"):
            color = "#22c58a"
        elif pt["label"] in ("LH", "LL"):
            color = "#f0554a"
        else:
            color = "#9a998f"
        pos = "aboveBar" if pt["kind"] == "high" else "belowBar"
        markers.append({
            "time": t, "position": pos, "color": color, "shape": "circle", "text": pt["label"],
        })
        line_points.append({"time": t, "value": pt["price"]})
    return markers, line_points
