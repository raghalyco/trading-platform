"""
Previous Support Bounce scanner.

Detects chart-style supports & classic patterns:
  1. Ascending trendline / horizontal multi-touch support
  2. Falling & rising wedges
  3. Double / triple bottoms (fresh bounce ≤5%)
  4. Ascending / descending / symmetrical triangles
  5. Bull flags & pennants
  6. Cup and handle

Then keeps Smart Money buy-zone / BUY setups and ranks them.
Plot geometry is stored so the dashboard chart page can draw the lines.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from itertools import combinations
from typing import Optional
from urllib.parse import quote

import numpy as np
import pandas as pd
from tqdm import tqdm

import config
import indicators as ind
import market_structure
import smart_money_strategy as sms
import trade_tracker
import universe as universe_mod
from charts import tradingview_chart_url

# Per-symbol OHLC + drawn lines for the Support Bounce chart page.
_CHART_CACHE: dict[str, dict] = {}


def local_chart_url(symbol: str) -> str:
    """URL-safe local chart path (handles symbols like M&M)."""
    return f"/support-chart/{quote(str(symbol).upper(), safe='')}"


def get_chart_payload(symbol: str) -> Optional[dict]:
    return _CHART_CACHE.get(str(symbol).upper())


def store_chart_payload(symbol: str, payload: dict) -> None:
    _CHART_CACHE[str(symbol).upper()] = payload


def clear_chart_cache() -> None:
    _CHART_CACHE.clear()


def rehydrate_charts(charts: Optional[dict]) -> None:
    """Restore chart payloads into the in-memory cache (after process warm/API)."""
    if not charts:
        return
    for sym, payload in charts.items():
        if isinstance(payload, dict):
            store_chart_payload(sym, payload)


def charts_snapshot(symbols: Optional[list] = None) -> dict:
    """Copy of cached chart payloads (optionally filtered to symbols)."""
    if symbols is None:
        return dict(_CHART_CACHE)
    out = {}
    for sym in symbols:
        key = str(sym).upper()
        payload = _CHART_CACHE.get(key)
        if payload is not None:
            out[key] = payload
    return out


def annotate_result_with_pattern(
    symbol: str,
    daily: pd.DataFrame,
    result: Optional[dict] = None,
) -> dict:
    """Detect best chart pattern, attach fields, and store plot geometry.

    Mutates ``result`` in place when provided. Used by Support Bounce,
    Stock for day, breakout / EMA10 / N-day scanners, etc.
    """
    out = result if result is not None else {}
    out["symbol"] = symbol
    out["chart_url"] = local_chart_url(symbol)
    out["tv_chart_url"] = tradingview_chart_url(symbol)

    if daily is None or daily.empty or len(daily) < 80:
        out.setdefault("pattern", None)
        out.setdefault("support_type", out.get("support_type"))
        out.setdefault("pattern_event", None)
        if daily is not None and not daily.empty and len(daily) >= 30:
            sms.attach_smart_money_label(symbol, daily, out)
        return out

    # Pine Smart Money BUY / SELL / READY / NEUTRAL on the same daily frame.
    sms.attach_smart_money_label(symbol, daily, out)

    event = detect_support_event(daily)
    if event is None:
        out["pattern"] = None
        out.setdefault("support_type", None)
        out["pattern_event"] = None
        store_chart_payload(symbol, build_chart_payload(symbol, daily, {
            "support_type": None,
            "event": None,
            "support_price": None,
            "geometry": {},
            "smart_money_signal": out.get("smart_money_signal"),
        }))
        return out

    store_chart_payload(symbol, build_chart_payload(symbol, daily, {
        **event,
        "smart_money_signal": out.get("smart_money_signal"),
    }))
    out["pattern"] = event.get("support_type")
    out["support_type"] = event.get("support_type")
    out["pattern_event"] = event.get("event")
    out["pattern_support"] = event.get("support_price")
    out["distance_from_support_pct"] = event.get("distance_from_support_pct")
    return out


def _weekly_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    return sms._resample_ohlcv(daily, "W-FRI")


def _volume_trend_label(df: pd.DataFrame) -> str:
    vol = df["volume"].astype(float)
    short_n = config.SMART_MONEY_VOLUME_SHORT
    long_n = config.SMART_MONEY_VOLUME_LONG
    if len(vol) < long_n + 2:
        return "Unknown"
    short = ind.sma(vol, short_n)
    long = ind.sma(vol, long_n)
    s0, s1 = float(short.iloc[-1]), float(short.iloc[-2])
    l0 = float(long.iloc[-1])
    last = float(vol.iloc[-1])
    rising = s0 > s1 and last >= l0 * 0.9
    falling = s0 < s1 and last < l0
    if rising:
        return "Rising"
    if falling:
        return "Falling"
    return "Flat"


def _structure_status(df: pd.DataFrame) -> str:
    length = config.SUPPORT_BOUNCE_PIVOT_LENGTH
    last_high, last_low = sms._pivot_levels(df["high"], df["low"], length)
    choch_buy, choch_sell, bos_buy, bos_sell = sms._structure_flags(df, last_high, last_low)
    lookback = config.SMART_MONEY_STRUCTURE_LOOKBACK
    if bool(choch_buy.iloc[-lookback:].any()):
        return "Bullish CHoCH"
    if bool(bos_buy.iloc[-lookback:].any()):
        return "Bullish BOS"
    if bool(choch_sell.iloc[-lookback:].any()):
        return "Bearish CHoCH"
    if bool(bos_sell.iloc[-lookback:].any()):
        return "Bearish BOS"
    return "Neutral"


def _confirmed_pivot_lows(df: pd.DataFrame, length: int) -> list[tuple[int, float]]:
    """Return (bar_index, price) for confirmed pivot lows, oldest → newest."""
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


def _major_swing_lows(df: pd.DataFrame) -> list[tuple[int, float]]:
    """Multi-scale swing lows (short + medium pivots), de-duplicated by bar."""
    lengths = sorted({
        config.SUPPORT_BOUNCE_PIVOT_LENGTH,
        max(config.SUPPORT_BOUNCE_PIVOT_LENGTH, 8),
        config.SUPPORT_BOUNCE_MAJOR_PIVOT_LENGTH,
    })
    by_idx: dict[int, float] = {}
    for length in lengths:
        for idx, price in _confirmed_pivot_lows(df, length):
            prev = by_idx.get(idx)
            if prev is None or price < prev:
                by_idx[idx] = price

    # Merge pivots that land within a few bars of each other (keep deeper low).
    ordered = sorted(by_idx.items(), key=lambda x: x[0])
    merged: list[tuple[int, float]] = []
    for idx, price in ordered:
        if merged and idx - merged[-1][0] <= 3:
            if price < merged[-1][1]:
                merged[-1] = (idx, price)
            continue
        merged.append((idx, price))
    return merged


def _confirmed_pivot_highs(df: pd.DataFrame, length: int) -> list[tuple[int, float]]:
    """Return (bar_index, price) for confirmed pivot highs, oldest → newest."""
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


def _major_swing_highs(df: pd.DataFrame) -> list[tuple[int, float]]:
    """Multi-scale swing highs, de-duplicated by bar."""
    lengths = sorted({
        config.SUPPORT_BOUNCE_PIVOT_LENGTH,
        max(config.SUPPORT_BOUNCE_PIVOT_LENGTH, 8),
        config.SUPPORT_BOUNCE_MAJOR_PIVOT_LENGTH,
    })
    by_idx: dict[int, float] = {}
    for length in lengths:
        for idx, price in _confirmed_pivot_highs(df, length):
            prev = by_idx.get(idx)
            if prev is None or price > prev:
                by_idx[idx] = price

    ordered = sorted(by_idx.items(), key=lambda x: x[0])
    merged: list[tuple[int, float]] = []
    for idx, price in ordered:
        if merged and idx - merged[-1][0] <= 3:
            if price > merged[-1][1]:
                merged[-1] = (idx, price)
            continue
        merged.append((idx, price))
    return merged


def _bar_time(df: pd.DataFrame, i: int) -> str:
    return pd.to_datetime(df["date"].iloc[i]).strftime("%Y-%m-%d")


def _line_points(
    df: pd.DataFrame,
    slope: float,
    intercept: float,
    i_start: int,
    i_end: Optional[int] = None,
) -> list[dict]:
    """Sample a straight line into Lightweight Charts {time, value} points."""
    n = len(df)
    i_start = max(0, min(int(i_start), n - 1))
    i_end = n - 1 if i_end is None else max(i_start, min(int(i_end), n - 1))
    step = max(1, (i_end - i_start) // 120)
    points = []
    for i in range(i_start, i_end + 1, step):
        points.append({
            "time": _bar_time(df, i),
            "value": round(_line_at(slope, intercept, i), 2),
        })
    last_t = _bar_time(df, i_end)
    if not points or points[-1]["time"] != last_t:
        points.append({
            "time": last_t,
            "value": round(_line_at(slope, intercept, i_end), 2),
        })
    return points


def build_chart_payload(symbol: str, df: pd.DataFrame, event: Optional[dict] = None) -> dict:
    """OHLC candles + support/resistance lines + Smart Money BUY/SELL marker."""
    event = event or {}
    candles = []
    for i in range(len(df)):
        candles.append({
            "time": _bar_time(df, i),
            "open": round(float(df["open"].iloc[i]), 2),
            "high": round(float(df["high"].iloc[i]), 2),
            "low": round(float(df["low"].iloc[i]), 2),
            "close": round(float(df["close"].iloc[i]), 2),
        })

    lines = []
    geom = event.get("geometry") or {}
    kind = geom.get("kind") or ""

    two_line_kinds = {
        "falling_wedge", "rising_wedge",
        "ascending_triangle", "descending_triangle", "symmetrical_triangle",
        "bull_flag", "bull_pennant",
    }
    if kind == "ascending" or event.get("support_type") == "Ascending trendline":
        g = geom.get("support") or {}
        if g.get("slope") is not None:
            lines.append({
                "name": "Ascending support",
                "color": "#3d8bfd",
                "points": _line_points(df, g["slope"], g["intercept"], g["i_start"]),
            })
    elif kind == "horizontal" or event.get("support_type") == "Horizontal support":
        level = float(geom.get("level") or event.get("support_price") or 0)
        i_start = int(geom.get("i_start") or 0)
        if level > 0:
            lines.append({
                "name": "Horizontal support",
                "color": "#22c58a",
                "points": _line_points(df, 0.0, level, i_start),
            })
    elif kind in ("double_bottom", "triple_bottom"):
        level = float(geom.get("level") or event.get("support_price") or 0)
        i_start = int(geom.get("i_start") or 0)
        if level > 0:
            lines.append({
                "name": "Bottom support",
                "color": "#22c58a",
                "points": _line_points(df, 0.0, level, i_start),
            })
        neck = geom.get("neckline")
        if neck is not None:
            lines.append({
                "name": "Neckline",
                "color": "#f0a020",
                "points": _line_points(df, 0.0, float(neck), i_start),
            })
    elif kind == "cup_and_handle":
        i_start = int(geom.get("i_start") or 0)
        handle = float(geom.get("level") or event.get("support_price") or 0)
        if handle > 0:
            lines.append({
                "name": "Handle support",
                "color": "#22c58a",
                "points": _line_points(df, 0.0, handle, i_start),
            })
        rim = geom.get("rim")
        if rim is not None:
            lines.append({
                "name": "Cup rim",
                "color": "#f0a020",
                "points": _line_points(df, 0.0, float(rim), i_start),
            })
        cup_low = geom.get("cup_low")
        if cup_low is not None:
            lines.append({
                "name": "Cup low",
                "color": "#5b8cff",
                "points": _line_points(df, 0.0, float(cup_low), i_start),
            })
    elif kind in two_line_kinds or (geom.get("support") and geom.get("resistance")):
        lower = geom.get("support") or {}
        upper = geom.get("resistance") or {}
        if lower.get("slope") is not None and upper.get("slope") is not None:
            i0 = min(int(lower.get("i_start", 0)), int(upper.get("i_start", 0)))
            lines.append({
                "name": geom.get("support_label") or "Support",
                "color": "#3d8bfd",
                "points": _line_points(df, lower["slope"], lower["intercept"], i0),
            })
            lines.append({
                "name": geom.get("resistance_label") or "Resistance",
                "color": "#f0a020",
                "points": _line_points(df, upper["slope"], upper["intercept"], i0),
            })

    # Always stamp Pine Smart Money BUY/SELL/READY on every opened chart.
    sm_signal = event.get("smart_money_signal")
    sm_side = event.get("smart_money_side")
    structure = event.get("structure") or event.get("market_structure")
    confidence = event.get("confidence")
    if not sm_signal or sm_signal == "NEUTRAL" or structure is None:
        try:
            weekly = _weekly_from_daily(df)
            label = sms.classify_symbol(
                symbol=symbol,
                signal_df=df,
                htf_df=weekly,
                ltf_df=df,
                daily_df=df,
            )
            sm_signal = label.get("smart_money_signal") or sm_signal or "NEUTRAL"
            sm_side = label.get("smart_money_side") or sm_side
            structure = label.get("structure") or structure
            confidence = label.get("confidence") if confidence is None else confidence
        except Exception:
            sm_signal = sm_signal or "NEUTRAL"

    markers = []
    if candles:
        last = candles[-1]
        t = last["time"]
        if sm_signal == "BUY":
            markers.append({
                "time": t,
                "position": "belowBar",
                "color": "#22c58a",
                "shape": "arrowUp",
                "text": "BUY",
            })
        elif sm_signal == "SELL":
            markers.append({
                "time": t,
                "position": "aboveBar",
                "color": "#f0554a",
                "shape": "arrowDown",
                "text": "SELL",
            })
        elif sm_signal == "READY":
            side = sm_side or ""
            markers.append({
                "time": t,
                "position": "belowBar" if side != "SELL" else "aboveBar",
                "color": "#f0b429",
                "shape": "circle",
                "text": f"READY {side}".strip(),
            })

    structure_markers, structure_line = market_structure.structure_chart_extras(
        df, config.SUPPORT_BOUNCE_PIVOT_LENGTH
    )
    if structure_line:
        lines.append({
            "name": "Market structure",
            "color": "#8a93a8",
            "points": structure_line,
            "dashed": True,
            "lineWidth": 1,
        })

    return {
        "symbol": symbol,
        "support_type": event.get("support_type"),
        "event": event.get("event"),
        "support_price": event.get("support_price"),
        "smart_money_signal": sm_signal,
        "smart_money_side": sm_side,
        "structure": structure,
        "confidence": confidence,
        "entry_price": event.get("entry_price"),
        "stop_loss": event.get("stop_loss"),
        "target": event.get("target"),
        "risk_reward": event.get("risk_reward"),
        "candles": candles,
        "lines": lines,
        "markers": markers,
        "structure_markers": structure_markers,
        "tv_chart_url": tradingview_chart_url(symbol),
    }


def _line_at(slope: float, intercept: float, x: float) -> float:
    return slope * x + intercept


def _fit_line(i1: int, p1: float, i2: int, p2: float) -> tuple[float, float]:
    if i2 == i1:
        return 0.0, p1
    slope = (p2 - p1) / (i2 - i1)
    intercept = p1 - slope * i1
    return slope, intercept


def _touch_band(price: float, atr: float) -> float:
    pct_band = price * (config.SUPPORT_BOUNCE_TOUCH_PCT / 100.0)
    atr_band = atr * config.SUPPORT_BOUNCE_ATR_TOUCH_MULT
    return max(pct_band, atr_band)


def _recent_probe_and_bounce(
    df: pd.DataFrame,
    support_series: pd.Series,
    atr: float,
) -> tuple[bool, bool, float, int]:
    """Check last N bars for a probe of support and a subsequent bullish bounce.

    Returns (touching_now, bounced, distance_pct_now, probe_bar_offset).
    """
    n = len(df)
    bounce_bars = config.SUPPORT_BOUNCE_BOUNCE_BARS
    start = max(0, n - bounce_bars)
    close = float(df["close"].iloc[-1])
    support_now = float(support_series.iloc[-1])
    band_now = _touch_band(support_now, atr)
    dist_now = ((close - support_now) / support_now * 100.0) if support_now else 0.0

    touching_now = (
        float(df["low"].iloc[-1]) <= support_now + band_now
        and close >= support_now - band_now * 0.25
    )

    probed = False
    probe_i = -1
    bounce = False
    for i in range(start, n):
        s = float(support_series.iloc[i])
        band = _touch_band(s, atr)
        low_i = float(df["low"].iloc[i])
        close_i = float(df["close"].iloc[i])
        open_i = float(df["open"].iloc[i])
        if low_i <= s + band:
            probed = True
            probe_i = i
        if probed and probe_i >= 0 and i > probe_i:
            # Bounce = recovered above support with a bullish close after the probe.
            if close_i > s and (close_i > open_i or close_i > float(df["low"].iloc[probe_i]) * 1.01):
                bounce = True

    # Held-above after a probe still counts as bounce even if candles mixed.
    if probed and probe_i >= 0 and not bounce and close >= support_now:
        if float(df["close"].iloc[-1]) > float(df["low"].iloc[probe_i]):
            bounce = True

    return touching_now, bounce, dist_now, (n - 1 - probe_i) if probe_i >= 0 else -1


def _line_broken(df: pd.DataFrame, slope: float, intercept: float, atr: float, from_i: int) -> bool:
    """True if price closed meaningfully below the line after the last anchor."""
    break_buffer = max(atr * 0.35, float(df["close"].iloc[-1]) * 0.008)
    closes_below = 0
    for i in range(from_i, len(df)):
        level = _line_at(slope, intercept, i)
        if float(df["close"].iloc[i]) < level - break_buffer:
            closes_below += 1
            if closes_below >= config.SUPPORT_BOUNCE_BREAK_CLOSES:
                return True
        else:
            closes_below = 0
    return False


def _detect_retest_after_breakout(
    df: pd.DataFrame, level: float, breakout_i: int, atr: float
) -> tuple[bool, int]:
    """After a breakout bar, did price pull back to test the broken level
    and hold, within SUPPORT_BOUNCE_RETEST_MAX_BARS?"""
    n = len(df)
    band = max(_touch_band(level, atr), level * (config.SUPPORT_BOUNCE_RETEST_BAND_PCT / 100.0))
    max_i = min(n, breakout_i + 1 + config.SUPPORT_BOUNCE_RETEST_MAX_BARS)
    for i in range(breakout_i + 1, max_i):
        low_i = float(df["low"].iloc[i])
        close_i = float(df["close"].iloc[i])
        if low_i <= level + band and close_i >= level - band * 0.5:
            return True, i
    return False, -1


def _volume_ratio_at(df: pd.DataFrame, i: int) -> float:
    vol = df["volume"].astype(float)
    sma = ind.sma(vol, config.SUPPORT_BOUNCE_VOLUME_SMA_PERIOD)
    avg = float(sma.iloc[i]) if pd.notna(sma.iloc[i]) else None
    v = float(vol.iloc[i])
    if not avg or avg <= 0:
        return 0.0
    return round(v / avg, 2)


def _close_strength_at(df: pd.DataFrame, i: int) -> float:
    """Where the close sits within the bar's range (1.0 = closed at the
    high, 0.0 = closed at the low) — a proxy for buying pressure."""
    high_i = float(df["high"].iloc[i])
    low_i = float(df["low"].iloc[i])
    close_i = float(df["close"].iloc[i])
    rng = high_i - low_i
    if rng <= 0:
        return 0.5
    return round((close_i - low_i) / rng, 2)


def _score_trendline(
    df: pd.DataFrame,
    pivots: list[tuple[int, float]],
    i1: int,
    p1: float,
    i2: int,
    p2: float,
    atr: float,
) -> Optional[dict]:
    """Score an ascending support line defined by two swing lows."""
    if i2 <= i1:
        return None
    slope, intercept = _fit_line(i1, p1, i2, p2)
    # Ascending only (chart examples are rising support).
    min_slope = (p1 * 0.00015)  # ~0.015%/bar ≈ gentle rise
    if slope < min_slope:
        return None

    # Anchors must be separated enough (multi-week / multi-month).
    min_sep = config.SUPPORT_BOUNCE_MIN_TRENDLINE_SEP
    if (i2 - i1) < min_sep:
        return None

    n = len(df)
    support_now = _line_at(slope, intercept, n - 1)
    if support_now <= 0:
        return None

    # Count additional swing-low touches on the line.
    touches = [(i1, p1), (i2, p2)]
    for idx, price in pivots:
        if idx in (i1, i2):
            continue
        if idx < i1 or idx > n - 1:
            continue
        level = _line_at(slope, intercept, idx)
        if abs(price - level) <= _touch_band(level, atr):
            touches.append((idx, price))

    # Also count any bar low that tags the line between anchors (wick tests).
    wick_hits = 0
    for i in range(i1, n):
        level = _line_at(slope, intercept, i)
        if abs(float(df["low"].iloc[i]) - level) <= _touch_band(level, atr) * 0.85:
            wick_hits += 1

    if len(touches) < 2:
        return None

    # Prefer lines that haven't been broken since the later anchor.
    if _line_broken(df, slope, intercept, atr, i2 + 1):
        return None

    support_series = pd.Series(
        [_line_at(slope, intercept, i) for i in range(n)],
        index=df.index,
    )
    touching, bounce, dist_pct, probe_age = _recent_probe_and_bounce(df, support_series, atr)
    if not touching and not bounce:
        return None
    if dist_pct < -config.SUPPORT_BOUNCE_TOUCH_PCT:
        return None
    if dist_pct > config.SUPPORT_BOUNCE_MAX_DIST_PCT and not bounce:
        return None
    if bounce and dist_pct > config.SUPPORT_BOUNCE_MAX_BOUNCE_DIST_PCT:
        return None

    quality = (
        len(touches) * 12
        + min(wick_hits, 8) * 2
        + (8 if bounce else 0)
        + (5 if touching else 0)
        + max(0, 10 - abs(dist_pct))
        + min((i2 - i1) / 20.0, 8)
    )
    event = "Touching support" if touching and not bounce else (
        "Bullish bounce" if bounce and not touching else "Touch + bounce"
    )
    return {
        "support_type": "Ascending trendline",
        "support_price": round(support_now, 2),
        "support_bar_index": i2,
        "support_age_bars": n - 1 - i2,
        "distance_from_support_pct": round(dist_pct, 2),
        "touch_count": len(touches),
        "event": event,
        "touching": touching,
        "bounce": bounce,
        "probe_age_bars": probe_age,
        "quality": quality,
        "slope": slope,
        "anchors": [(i1, round(p1, 2)), (i2, round(p2, 2))],
        "geometry": {
            "kind": "ascending",
            "support": {
                "slope": slope,
                "intercept": intercept,
                "i_start": i1,
            },
        },
    }


def find_ascending_trendline_supports(df: pd.DataFrame, atr: float) -> list[dict]:
    pivots = _major_swing_lows(df)
    if len(pivots) < 2:
        return []

    # Use the more recent major lows as candidate anchors (chart: months apart).
    recent = pivots[-config.SUPPORT_BOUNCE_MAX_PIVOTS_FOR_LINE:]
    hits = []
    for (i1, p1), (i2, p2) in combinations(recent, 2):
        scored = _score_trendline(df, pivots, i1, p1, i2, p2, atr)
        if scored:
            hits.append(scored)

    # Prefer three-touch rising lines (first/mid/last all near the same slope).
    if len(recent) >= 3:
        for a, b, c in combinations(range(len(recent)), 3):
            i1, p1 = recent[a]
            i2, p2 = recent[b]
            i3, p3 = recent[c]
            slope, intercept = _fit_line(i1, p1, i3, p3)
            if slope < (p1 * 0.00015):
                continue
            mid = _line_at(slope, intercept, i2)
            if abs(p2 - mid) > _touch_band(mid, atr) * 1.25:
                continue
            scored = _score_trendline(df, pivots, i1, p1, i3, p3, atr)
            if not scored:
                continue
            scored = dict(scored)
            scored["touch_count"] = max(scored["touch_count"], 3)
            scored["quality"] += 12
            scored["anchors"] = [
                (i1, round(p1, 2)), (i2, round(p2, 2)), (i3, round(p3, 2))
            ]
            hits.append(scored)
    return hits


def find_horizontal_supports(df: pd.DataFrame, atr: float) -> list[dict]:
    """Cluster swing lows into horizontal multi-touch support zones."""
    pivots = _major_swing_lows(df)
    if len(pivots) < 2:
        return []

    n = len(df)
    used = set()
    clusters = []
    for i, (idx, price) in enumerate(pivots):
        if i in used:
            continue
        members = [(idx, price)]
        used.add(i)
        band = _touch_band(price, atr)
        for j in range(i + 1, len(pivots)):
            if j in used:
                continue
            jdx, jprice = pivots[j]
            level = float(np.median([m[1] for m in members]))
            if abs(jprice - level) <= max(band, _touch_band(level, atr)):
                members.append((jdx, jprice))
                used.add(j)
        if len(members) >= config.SUPPORT_BOUNCE_MIN_HORIZONTAL_TOUCHES:
            clusters.append(members)

    hits = []
    for members in clusters:
        level = float(np.median([m[1] for m in members]))
        last_touch_i = max(m[0] for m in members)
        # Broken if several closes well below the zone after last cluster touch.
        break_buffer = max(atr * 0.4, level * 0.01)
        closes_below = 0
        broken = False
        for i in range(last_touch_i + 1, n):
            if float(df["close"].iloc[i]) < level - break_buffer:
                closes_below += 1
                if closes_below >= config.SUPPORT_BOUNCE_BREAK_CLOSES:
                    broken = True
                    break
            else:
                closes_below = 0
        if broken:
            continue

        support_series = pd.Series(level, index=df.index)
        touching, bounce, dist_pct, probe_age = _recent_probe_and_bounce(
            df, support_series, atr
        )
        if not touching and not bounce:
            continue
        if dist_pct < -config.SUPPORT_BOUNCE_TOUCH_PCT:
            continue
        if dist_pct > config.SUPPORT_BOUNCE_MAX_DIST_PCT and not bounce:
            continue
        if bounce and dist_pct > config.SUPPORT_BOUNCE_MAX_BOUNCE_DIST_PCT:
            continue

        event = "Touching support" if touching and not bounce else (
            "Bullish bounce" if bounce and not touching else "Touch + bounce"
        )
        quality = (
            len(members) * 14
            + (8 if bounce else 0)
            + (5 if touching else 0)
            + max(0, 10 - abs(dist_pct))
        )
        hits.append({
            "support_type": "Horizontal support",
            "support_price": round(level, 2),
            "support_bar_index": last_touch_i,
            "support_age_bars": n - 1 - last_touch_i,
            "distance_from_support_pct": round(dist_pct, 2),
            "touch_count": len(members),
            "event": event,
            "touching": touching,
            "bounce": bounce,
            "probe_age_bars": probe_age,
            "quality": quality,
            "anchors": [(i, round(p, 2)) for i, p in members],
            "geometry": {
                "kind": "horizontal",
                "level": level,
                "i_start": min(m[0] for m in members),
            },
        })
    return hits


def find_falling_wedges(df: pd.DataFrame, atr: float) -> list[dict]:
    """Falling wedge: downward-sloping converging resistance + support (bullish)."""
    highs = _major_swing_highs(df)
    lows = _major_swing_lows(df)
    max_piv = config.SUPPORT_BOUNCE_WEDGE_MAX_PIVOTS
    highs = highs[-max_piv:]
    lows = lows[-max_piv:]
    if len(highs) < 2 or len(lows) < 2:
        return []

    n = len(df)
    min_sep = config.SUPPORT_BOUNCE_WEDGE_MIN_SEP
    hits = []

    for (hi1, hp1), (hi2, hp2) in combinations(highs, 2):
        if hi2 <= hi1 or (hi2 - hi1) < min_sep:
            continue
        uslope, uintercept = _fit_line(hi1, hp1, hi2, hp2)
        # Upper resistance must slope down.
        if uslope >= -(hp1 * 0.00008):
            continue

        for (li1, lp1), (li2, lp2) in combinations(lows, 2):
            if li2 <= li1 or (li2 - li1) < min_sep:
                continue
            # Lower and upper anchors should overlap in time.
            if li2 < hi1 or li1 > hi2:
                continue
            if abs(li1 - hi1) > max(40, (hi2 - hi1) * 0.6):
                continue

            lslope, lintercept = _fit_line(li1, lp1, li2, lp2)
            # Lower support also slopes down (or flat-ish), but less steep than upper.
            if lslope > (lp1 * 0.00005):
                continue
            # Converging: upper more negative than lower.
            if uslope >= lslope - 1e-9:
                continue

            start_i = min(hi1, li1)
            end_i = max(hi2, li2)
            u0 = _line_at(uslope, uintercept, start_i)
            l0 = _line_at(lslope, lintercept, start_i)
            u1 = _line_at(uslope, uintercept, end_i)
            l1 = _line_at(lslope, lintercept, end_i)
            if u0 <= l0 or u1 <= l1:
                continue
            width0 = u0 - l0
            width1 = u1 - l1
            if width0 <= 0 or width1 >= width0 * 0.92:
                continue

            # Count touches on each boundary.
            upper_touches = 0
            lower_touches = 0
            for idx, price in highs:
                if idx < start_i or idx > n - 1:
                    continue
                lvl = _line_at(uslope, uintercept, idx)
                if abs(price - lvl) <= _touch_band(lvl, atr):
                    upper_touches += 1
            for idx, price in lows:
                if idx < start_i or idx > n - 1:
                    continue
                lvl = _line_at(lslope, lintercept, idx)
                if abs(price - lvl) <= _touch_band(lvl, atr):
                    lower_touches += 1
            if upper_touches < 2 or lower_touches < 2:
                continue

            # Invalidate if closes break below lower support after pattern formed.
            if _line_broken(df, lslope, lintercept, atr, end_i + 1):
                continue

            support_series = pd.Series(
                [_line_at(lslope, lintercept, i) for i in range(n)],
                index=df.index,
            )
            resist_series = pd.Series(
                [_line_at(uslope, uintercept, i) for i in range(n)],
                index=df.index,
            )
            support_now = float(support_series.iloc[-1])
            resist_now = float(resist_series.iloc[-1])
            if support_now <= 0 or resist_now <= support_now:
                continue

            touching, bounce, dist_pct, probe_age = _recent_probe_and_bounce(
                df, support_series, atr
            )
            close = float(df["close"].iloc[-1])

            # Upside breakout through resistance (classic falling-wedge resolution).
            breakout = False
            breakout_i = -1
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
                continue
            if not breakout:
                if dist_pct < -config.SUPPORT_BOUNCE_TOUCH_PCT:
                    continue
                if dist_pct > config.SUPPORT_BOUNCE_MAX_DIST_PCT and not bounce:
                    continue
                if bounce and dist_pct > config.SUPPORT_BOUNCE_MAX_BOUNCE_DIST_PCT:
                    continue

            retest = False
            retest_i = -1
            volume_ratio = 0.0
            close_strength = 0.5
            dist_from_breakout_pct = 0.0
            if breakout:
                dist_from_breakout_pct = (close - resist_now) / resist_now * 100.0
                retest, retest_i = _detect_retest_after_breakout(df, resist_now, breakout_i, atr)
                volume_ratio = _volume_ratio_at(df, breakout_i)
                close_strength = _close_strength_at(df, breakout_i)
                if not retest and dist_from_breakout_pct > config.SUPPORT_BOUNCE_MAX_BREAKOUT_DIST_PCT:
                    continue

            if breakout:
                event = "Falling wedge breakout"
                dist_pct = (close - support_now) / support_now * 100.0
            else:
                event = (
                    "Touching support" if touching and not bounce else
                    ("Bullish bounce" if bounce and not touching else "Touch + bounce")
                )

            support_price = round(support_now, 2)

            quality = (
                (upper_touches + lower_touches) * 8
                + (14 if breakout else 0)
                + (8 if bounce else 0)
                + (5 if touching else 0)
                + max(0, 10 - abs(dist_pct))
                + min((end_i - start_i) / 25.0, 8)
                + 10  # pattern bonus
                + (14 if retest else 0)
                + min(volume_ratio, 4.0) * 4
                + (8 if close_strength >= config.SUPPORT_BOUNCE_MIN_CLOSE_STRENGTH else 0)
            )
            hits.append({
                "support_type": "Falling wedge",
                "support_price": support_price,
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
                "anchors": [
                    (hi1, round(hp1, 2)), (hi2, round(hp2, 2)),
                    (li1, round(lp1, 2)), (li2, round(lp2, 2)),
                ],
                "geometry": {
                    "kind": "falling_wedge",
                    "support": {
                        "slope": lslope,
                        "intercept": lintercept,
                        "i_start": start_i,
                    },
                    "resistance": {
                        "slope": uslope,
                        "intercept": uintercept,
                        "i_start": start_i,
                    },
                    "support_label": "Wedge support",
                    "resistance_label": "Wedge resistance",
                },
            })
    return hits


def _neckline_between(highs: list[tuple[int, float]], i_left: int, i_right: int) -> Optional[float]:
    """Highest swing high strictly between two bottom anchors."""
    mids = [p for i, p in highs if i_left < i < i_right]
    if not mids:
        return None
    return float(max(mids))


def find_double_triple_bottoms(df: pd.DataFrame, atr: float) -> list[dict]:
    """Double / triple bottoms that just bounced and are still ≤5% above the lows."""
    lows = _major_swing_lows(df)
    highs = _major_swing_highs(df)
    max_piv = config.SUPPORT_BOUNCE_BOTTOM_MAX_PIVOTS
    lows = lows[-max_piv:]
    if len(lows) < 2:
        return []

    n = len(df)
    min_sep = config.SUPPORT_BOUNCE_BOTTOM_MIN_SEP
    tol_pct = config.SUPPORT_BOUNCE_BOTTOM_TOL_PCT / 100.0
    max_up = config.SUPPORT_BOUNCE_BOTTOM_MAX_UP_PCT
    hits = []

    def _near(a: float, b: float) -> bool:
        mid = (a + b) / 2.0
        band = max(mid * tol_pct, _touch_band(mid, atr))
        return abs(a - b) <= band

    # Double bottoms: two lows; triple: three lows (prefer triple when valid).
    combos: list[tuple[str, list[tuple[int, float]]]] = []
    for (i1, p1), (i2, p2) in combinations(lows, 2):
        if i2 <= i1 or (i2 - i1) < min_sep:
            continue
        if not _near(p1, p2):
            continue
        combos.append(("Double bottom", [(i1, p1), (i2, p2)]))

    if len(lows) >= 3:
        for (i1, p1), (i2, p2), (i3, p3) in combinations(lows, 3):
            if not (i1 < i2 < i3):
                continue
            if (i2 - i1) < min_sep or (i3 - i2) < min_sep:
                continue
            if not (_near(p1, p2) and _near(p2, p3) and _near(p1, p3)):
                continue
            combos.append(("Triple bottom", [(i1, p1), (i2, p2), (i3, p3)]))

    for label, members in combos:
        members = sorted(members, key=lambda x: x[0])
        level = float(np.median([m[1] for m in members]))
        first_i = members[0][0]
        last_i = members[-1][0]

        # Require a reaction high between first and last bottom (the "W" peak).
        neck = _neckline_between(highs, first_i, last_i)
        if neck is None:
            # Fall back to bar highs between bottoms if no swing high marked.
            seg = df["high"].iloc[first_i + 1:last_i]
            if len(seg) == 0:
                continue
            neck = float(seg.max())
        if neck <= level * 1.01:
            continue  # peak must clear the bottoms meaningfully

        # Pattern invalid if price closed well below bottoms after last low.
        break_buffer = max(atr * 0.4, level * 0.01)
        closes_below = 0
        broken = False
        for i in range(last_i + 1, n):
            if float(df["close"].iloc[i]) < level - break_buffer:
                closes_below += 1
                if closes_below >= config.SUPPORT_BOUNCE_BREAK_CLOSES:
                    broken = True
                    break
            else:
                closes_below = 0
        if broken:
            continue

        support_series = pd.Series(level, index=df.index)
        touching, bounce, dist_pct, probe_age = _recent_probe_and_bounce(
            df, support_series, atr
        )
        # User ask: just bounced, still within max 5% of the bottoms.
        if not bounce:
            continue
        if dist_pct < -config.SUPPORT_BOUNCE_TOUCH_PCT:
            continue
        if dist_pct > max_up:
            continue
        # Last bottom should be the recent probe (fresh pattern completion).
        if (n - 1 - last_i) > config.SUPPORT_BOUNCE_BOUNCE_BARS:
            continue

        event = "Touch + bounce" if touching else "Bullish bounce"
        quality = (
            len(members) * 16
            + 12  # pattern bonus
            + (8 if bounce else 0)
            + (5 if touching else 0)
            + max(0, 12 - abs(dist_pct) * 1.5)
            + min((last_i - first_i) / 20.0, 8)
        )
        if label == "Triple bottom":
            quality += 10

        hits.append({
            "support_type": label,
            "support_price": round(level, 2),
            "support_bar_index": last_i,
            "support_age_bars": n - 1 - last_i,
            "distance_from_support_pct": round(dist_pct, 2),
            "touch_count": len(members),
            "event": event,
            "touching": touching,
            "bounce": True,
            "probe_age_bars": probe_age,
            "quality": quality,
            "anchors": [(i, round(p, 2)) for i, p in members],
            "geometry": {
                "kind": "triple_bottom" if label == "Triple bottom" else "double_bottom",
                "level": level,
                "neckline": round(neck, 2),
                "i_start": first_i,
            },
        })
    return hits


def detect_support_event(df: pd.DataFrame) -> Optional[dict]:
    """Best support/pattern event among trendlines, bottoms, wedges, triangles, flags, cup."""
    if len(df) < 80:
        return None
    atr_s = ind.atr(df["high"], df["low"], df["close"], 14)
    atr = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else float(
        df["high"].iloc[-1] - df["low"].iloc[-1]
    )

    # Late import avoids circular dependency (chart_patterns imports this module).
    from chart_patterns import collect_classic_patterns

    candidates = []
    candidates.extend(find_ascending_trendline_supports(df, atr))
    candidates.extend(find_horizontal_supports(df, atr))
    candidates.extend(find_falling_wedges(df, atr))
    candidates.extend(find_double_triple_bottoms(df, atr))
    candidates.extend(collect_classic_patterns(df, atr))
    if not candidates:
        return None

    type_rank = {
        "Cup and handle": 9,
        "Bull pennant": 8,
        "Bull flag": 8,
        "Ascending triangle": 7,
        "Symmetrical triangle": 6,
        "Triple bottom": 6,
        "Double bottom": 5,
        "Falling wedge": 5,
        "Descending triangle": 4,
        "Rising wedge": 3,
        "Ascending trendline": 2,
        "Horizontal support": 1,
    }
    candidates.sort(
        key=lambda c: (
            c.get("quality", 0),
            1 if c.get("bounce") else 0,
            c.get("touch_count", 0),
            type_rank.get(c.get("support_type"), 0),
            -abs(c.get("distance_from_support_pct") or 99),
        ),
        reverse=True,
    )
    return candidates[0]


def _buy_zone_context(df: pd.DataFrame, htf_df: pd.DataFrame, support_event: dict) -> dict:
    """Soft Smart Money buy-zone score when a full BUY signal is not present."""
    length = config.SMART_MONEY_PIVOT_LENGTH
    atr_s = ind.atr(df["high"], df["low"], df["close"], 14)
    atr_val = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else float(
        df["high"].iloc[-1] - df["low"].iloc[-1]
    )
    close = float(df["close"].iloc[-1])
    support = float(support_event["support_price"])

    last_high, last_low = sms._pivot_levels(df["high"], df["low"], length)
    choch_buy, _, bos_buy, _ = sms._structure_flags(df, last_high, last_low)
    lookback = config.SMART_MONEY_STRUCTURE_LOOKBACK
    structure_buy = bool((choch_buy | bos_buy).iloc[-lookback:].any())

    trend_htf = int(sms._trend_series(htf_df).iloc[-1]) if len(htf_df) else 0
    trend_sig = int(sms._trend_series(df).iloc[-1])
    confidence = sms._confidence_from_trends([trend_sig, trend_htf, trend_sig])

    vol = df["volume"].astype(float)
    vol_avg = ind.sma(vol, config.SMART_MONEY_VOLUME_LONG)
    vol_short = ind.sma(vol, config.SMART_MONEY_VOLUME_SHORT)
    vol_ok = False
    if (
        pd.notna(vol_avg.iloc[-1]) and pd.notna(vol_short.iloc[-1])
        and pd.notna(vol_short.iloc[-2])
    ):
        vol_ok = bool(vol.iloc[-1] > vol_avg.iloc[-1] and (vol_short.iloc[-1] - vol_short.iloc[-2]) > 0)

    dist_now_pct = (close - support) / support * 100.0 if support else 99.0
    near_support = abs(dist_now_pct) <= config.SUPPORT_BOUNCE_BUY_ZONE_MAX_PCT

    score = float(confidence)
    if support_event.get("bounce"):
        score += 12
    if support_event.get("touching"):
        score += 6
    if support_event.get("support_type") == "Ascending trendline":
        score += 8
    elif support_event.get("support_type") == "Falling wedge":
        score += 12
    elif support_event.get("support_type") == "Rising wedge":
        score += 8
    elif support_event.get("support_type") == "Double bottom":
        score += 14
    elif support_event.get("support_type") == "Triple bottom":
        score += 16
    elif support_event.get("support_type") in (
        "Ascending triangle", "Symmetrical triangle", "Descending triangle",
    ):
        score += 13
    elif support_event.get("support_type") in ("Bull flag", "Bull pennant"):
        score += 14
    elif support_event.get("support_type") == "Cup and handle":
        score += 15
    score += min(support_event.get("touch_count", 0), 4) * 3
    # Proximity to support is the primary lever the user asked to prioritize
    # ("closer to support" beats "better pattern but already extended") — a
    # steep, uncapped-below-zero penalty so distance dominates the ranking
    # rather than getting drowned out by pattern-type bonuses.
    score -= abs(dist_now_pct) * config.SUPPORT_BOUNCE_PROXIMITY_PENALTY_MULT
    if abs(support_event.get("distance_from_support_pct") or 99) <= config.SUPPORT_BOUNCE_TOUCH_PCT:
        score += 5
    # Positive market structure confirmation - weighted up from 10 -> 18,
    # since this is one of the explicit priorities: prefer setups where
    # structure has actually turned (CHoCH/BOS bullish), not just "near a
    # line" with no confirmation.
    if structure_buy:
        score += 18
    if trend_htf == 1:
        score += 10
    elif trend_htf == -1:
        score -= 20
    if vol_ok:
        score += 8
    score = max(0.0, min(100.0, score))

    entry = close
    stop = min(support * 0.992, entry - config.SMART_MONEY_SL_ATR_MULT * atr_val)
    target = entry + config.SMART_MONEY_TP_ATR_MULT * atr_val
    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    non_bearish = trend_htf >= 0 or not config.SUPPORT_BOUNCE_REQUIRE_NON_BEARISH_TREND
    buy_zone = (
        near_support
        and non_bearish
        and score >= config.SUPPORT_BOUNCE_MIN_SCORE
        and (
            structure_buy
            or support_event.get("bounce")
            or support_event.get("touch_count", 0) >= 2
            or vol_ok
        )
    )

    return {
        "buy_zone": buy_zone,
        "near_support": near_support,
        "distance_from_support_now_pct": round(dist_now_pct, 2),
        "score": round(score, 1),
        "confidence": confidence,
        "structure_buy": structure_buy,
        "trend_htf": trend_htf,
        "volume_ok": vol_ok,
        "entry_price": round(entry, 2),
        "stop_loss": round(stop, 2),
        "target": round(target, 2),
        "risk_reward": rr,
        "atr": round(atr_val, 2),
    }


def rebuild_chart_for_symbol(symbol: str, daily: pd.DataFrame) -> Optional[dict]:
    """Re-detect support and build chart payload (used when cache is cold)."""
    event = detect_support_event(daily) or {}
    label = sms.classify_symbol(
        symbol=symbol,
        signal_df=daily,
        htf_df=_weekly_from_daily(daily),
        ltf_df=daily,
        daily_df=daily,
    )
    event = {
        **event,
        "smart_money_signal": label.get("smart_money_signal"),
        "smart_money_side": label.get("smart_money_side"),
        "structure": label.get("structure"),
        "confidence": label.get("confidence"),
    }
    payload = build_chart_payload(symbol, daily, event)
    store_chart_payload(symbol, payload)
    return payload


def evaluate_symbol_support_bounce(
    symbol: str,
    daily: pd.DataFrame,
) -> Optional[dict]:
    event = detect_support_event(daily)
    if event is None:
        return None

    weekly = _weekly_from_daily(daily)
    sm = sms.evaluate_symbol(
        symbol=symbol,
        signal_df=daily,
        sector="",
        htf_df=weekly,
        ltf_df=daily,
        daily_df=daily,
    )
    label = sms.classify_symbol(
        symbol=symbol,
        signal_df=daily,
        htf_df=weekly,
        ltf_df=daily,
        daily_df=daily,
    )
    zone = _buy_zone_context(daily, weekly, event)

    # Hard, uniform gate applied to EVERY path — including a raw Pine Smart
    # Money BUY/SELL signal, which has no notion of "distance from previous
    # support" on its own. Without this, a stock Pine likes for unrelated
    # momentum reasons but that's already 20%+ past its support could still
    # slip through; this is what the user asked to fix ("avoid stocks that
    # already had a large breakout/move").
    if not zone["near_support"]:
        return None

    sm_buy = sm is not None and sm.signal == "BUY"
    sm_sell = sm is not None and sm.signal == "SELL"
    if config.SUPPORT_BOUNCE_REQUIRE_SM_BUY and not sm_buy:
        return None
    # Bounce scanner is long-biased; keep buy-zone / BUY, still surface SELL if Pine fires.
    if not sm_buy and not sm_sell and not zone["buy_zone"]:
        return None

    # A "Bearish CHoCH" structure label directly contradicts a long/buy-zone
    # thesis - drop it. Not applied to sm_sell rows, where a bearish CHoCH
    # is the CONFIRMING signal, not noise to filter out.
    structure_label = label.get("structure") or _structure_status(daily)
    if structure_label == "Bearish CHoCH" and not sm_sell:
        return None

    if sm_buy or sm_sell:
        entry = sm.entry_price
        stop = sm.stop_loss
        target = sm.target
        rr = sm.risk_reward
        score = max(zone["score"], float(sm.confidence) + 15)
        sm_signal = sm.signal
        confidence = sm.confidence
    else:
        entry = zone["entry_price"]
        stop = zone["stop_loss"]
        target = zone["target"]
        rr = zone["risk_reward"]
        score = zone["score"]
        # Prefer Pine READY over generic BUY_ZONE when classify says so.
        sm_signal = label.get("smart_money_signal") if label.get("smart_money_signal") in (
            "READY", "BUY", "SELL"
        ) else "READY"
        confidence = label.get("confidence") or zone["confidence"]

    # Reward breakout-type events that re-tested the broken level, broke out
    # on high volume, and closed strong — these are the setups worth
    # surfacing first; a bare breakout with no re-test and a weak close
    # ranks lower even if it otherwise cleared the buy-zone bar.
    retest_bonus = 0.0
    if event.get("retest"):
        retest_bonus += 10.0
    if (event.get("volume_ratio") or 0) >= config.SUPPORT_BOUNCE_VOLUME_SPIKE_MULT:
        retest_bonus += 8.0
    if (event.get("close_strength") or 0) >= config.SUPPORT_BOUNCE_MIN_CLOSE_STRENGTH:
        retest_bonus += 5.0
    score = min(100.0, score + retest_bonus)

    chart_payload = build_chart_payload(symbol, daily, {
        **event,
        "smart_money_signal": sm_signal,
        "entry_price": entry,
        "stop_loss": stop,
        "target": target,
        "risk_reward": rr,
    })
    store_chart_payload(symbol, chart_payload)

    return {
        "symbol": symbol,
        "event": event["event"],
        "support_type": event.get("support_type", "Support"),
        "previous_support": event["support_price"],
        "current_price": round(float(daily["close"].iloc[-1]), 2),
        "distance_from_support_pct": event["distance_from_support_pct"],
        "near_support": zone["near_support"],
        "structure_confirmed": zone["structure_buy"],
        "touch_count": event.get("touch_count", 1),
        "retest": event.get("retest", False),
        "breakout_volume_ratio": event.get("volume_ratio"),
        "close_strength": event.get("close_strength"),
        "volume_trend": _volume_trend_label(daily),
        "market_structure": label.get("structure") or _structure_status(daily),
        "structure": label.get("structure") or _structure_status(daily),
        "smart_money_signal": sm_signal,
        "smart_money_side": label.get("smart_money_side"),
        "smart_money_score": round(score, 1),
        "confidence": confidence,
        "risk_reward": rr,
        "entry_price": entry,
        "stop_loss": stop,
        "target": target,
        "support_age_bars": event["support_age_bars"],
        "timestamp": pd.to_datetime(daily["date"].iloc[-1]).isoformat(),
        "chart_url": local_chart_url(symbol),
        "tv_chart_url": tradingview_chart_url(symbol),
    }


def scan_support_bounce(
    kite_client,
    universe_df=None,
    universe_mode: Optional[str] = None,
) -> dict:
    try:
        mode = universe_mod.normalize_nifty_mode(
            universe_mode or config.SUPPORT_BOUNCE_UNIVERSE or "nifty100"
        )
    except ValueError:
        mode = "nifty100"

    try:
        scan_df = universe_mod.build_nifty_index_universe(kite_client, mode)
    except Exception as e:
        print(f"  [warn] support-bounce universe {mode} failed ({e}) — falling back")
        scan_df = universe_df

    label = universe_mod.nifty_mode_label(mode)
    if scan_df is None or scan_df.empty:
        return {
            "generated_at": datetime.now().isoformat(),
            "universe_mode": mode,
            "universe_label": label,
            "universe_size": 0,
            "scanned": 0,
            "num_results": 0,
            "results": [],
        }

    today = datetime.now().date()
    from_date = today - timedelta(days=config.SUPPORT_BOUNCE_LOOKBACK_DAYS)
    results = []
    scanned = 0
    clear_chart_cache()
    print(f"Support bounce: scanning {label} ({len(scan_df)} symbols)...")

    for _, row in tqdm(scan_df.iterrows(), total=len(scan_df), desc=f"Support bounce ({label})"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        scanned += 1
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, today)
            if daily.empty or len(daily) < 80:
                continue
            hit = evaluate_symbol_support_bounce(symbol, daily)
            if hit:
                results.append(hit)
                if config.AUTO_TRACK_ENABLED and hit.get("smart_money_signal") == "BUY":
                    try:
                        trade_tracker.auto_track_if_new(
                            symbol=symbol, source="support_bounce",
                            entry_price=hit.get("entry_price") or hit.get("current_price"),
                            stop_loss=hit.get("stop_loss"), target=hit.get("target"),
                            chart_url=hit.get("chart_url"),
                        )
                    except Exception as e:
                        print(f"  [warn] support-bounce auto-track failed for {symbol}: {e}")
        except Exception as e:
            print(f"  [warn] support-bounce skipped {symbol}: {e}")
            continue

    results.sort(
        key=lambda r: (
            1 if r.get("structure_confirmed") else 0,
            1 if r.get("smart_money_signal") == "BUY" else 0,
            r.get("smart_money_score") or 0,
            -(abs(r.get("distance_from_support_pct") or 99)),
            r.get("touch_count") or 0,
            r.get("risk_reward") or 0,
        ),
        reverse=True,
    )
    charts = {
        str(r["symbol"]).upper(): get_chart_payload(r["symbol"])
        for r in results
        if get_chart_payload(r["symbol"]) is not None
    }
    print(f"Support bounce: {len(results)} buy-zone hit(s) of {scanned} scanned ({label})")
    return {
        "generated_at": datetime.now().isoformat(),
        "universe_mode": mode,
        "universe_label": label,
        "universe_size": len(scan_df),
        "scanned": scanned,
        "num_results": len(results),
        "results": results,
        "charts": charts,
    }
