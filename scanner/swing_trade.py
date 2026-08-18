"""
Swing Trade (Weekly) scanner.

Mirrors support_bounce.py's trendline/horizontal detection, but flipped:
  - Looks for RESISTANCE (declining trendline through swing highs, or a flat
    horizontal box top / cup rim) instead of support.
  - Runs on WEEKLY candles (resampled from daily) instead of daily.
  - Requires a breakout candle that clears resistance AND fires on a volume
    spike (the "VOL SPIKE" callout in the reference setup) — this gate is
    mandatory, not optional.
  - Two-stage status: CANDIDATE (breakout just happened, price hasn't yet
    cleared the breakout candle's high) -> TRIGGERED (it has — the actual
    "wait to break the breakout candle's high" entry).
  - Detects an optional RE-TEST of the broken level after the breakout.
  - Reports weekly EMA(10/20) crossover as informational context only —
    it never gates a result.

Plot geometry + a volume series are cached per-symbol so the dashboard's
weekly chart page can redraw the resistance line/box and BREAKOUT / RE-TEST /
CONTINUATION markers exactly like the reference screenshots.
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
import telegram_alerts
import trade_tracker
import universe as universe_mod
from charts import tradingview_chart_url

# Per-symbol weekly OHLCV + drawn lines/markers for the Swing Trade chart page.
_CHART_CACHE: dict[str, dict] = {}

# Dedup so the same breakout doesn't re-alert on every re-scan within this
# process's lifetime (resets on restart, same as the other scanners'
# in-memory dedup - e.g. smart_money_pipeline.py's _seen_signal_keys).
_alerted_breakouts: set[str] = set()


def local_chart_url(symbol: str) -> str:
    """URL-safe local chart path (handles symbols like M&M)."""
    return f"/swing-chart/{quote(str(symbol).upper(), safe='')}"


def get_chart_payload(symbol: str) -> Optional[dict]:
    return _CHART_CACHE.get(str(symbol).upper())


def store_chart_payload(symbol: str, payload: dict) -> None:
    _CHART_CACHE[str(symbol).upper()] = payload


def clear_chart_cache() -> None:
    _CHART_CACHE.clear()


def rehydrate_charts(charts: Optional[dict]) -> None:
    if not charts:
        return
    for sym, payload in charts.items():
        if isinstance(payload, dict):
            store_chart_payload(sym, payload)


def charts_snapshot(symbols: Optional[list] = None) -> dict:
    if symbols is None:
        return dict(_CHART_CACHE)
    out = {}
    for sym in symbols:
        key = str(sym).upper()
        payload = _CHART_CACHE.get(key)
        if payload is not None:
            out[key] = payload
    return out


def _weekly_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    return sms._resample_ohlcv(daily, "W-FRI")


def _bar_time(df: pd.DataFrame, i: int) -> str:
    return pd.to_datetime(df["date"].iloc[i]).strftime("%Y-%m-%d")


def _line_at(slope: float, intercept: float, x: float) -> float:
    return slope * x + intercept


def _fit_line(i1: int, p1: float, i2: int, p2: float) -> tuple[float, float]:
    if i2 == i1:
        return 0.0, p1
    slope = (p2 - p1) / (i2 - i1)
    intercept = p1 - slope * i1
    return slope, intercept


def _touch_band(price: float, atr: float) -> float:
    pct_band = price * (config.SWING_TRADE_TOUCH_PCT / 100.0)
    atr_band = atr * config.SWING_TRADE_ATR_TOUCH_MULT
    return max(pct_band, atr_band)


def _line_points(
    df: pd.DataFrame,
    slope: float,
    intercept: float,
    i_start: int,
    i_end: Optional[int] = None,
) -> list[dict]:
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


def _confirmed_pivot_highs(df: pd.DataFrame, length: int) -> list[tuple[int, float]]:
    """Return (bar_index, price) for confirmed pivot highs, oldest -> newest."""
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
    """Multi-scale weekly swing highs, de-duplicated by bar."""
    lengths = sorted({
        config.SWING_TRADE_PIVOT_LENGTH,
        max(config.SWING_TRADE_PIVOT_LENGTH, 4),
        config.SWING_TRADE_MAJOR_PIVOT_LENGTH,
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
        if merged and idx - merged[-1][0] <= 2:
            if price > merged[-1][1]:
                merged[-1] = (idx, price)
            continue
        merged.append((idx, price))
    return merged


def _volume_spike(vol: float, avg: Optional[float]) -> tuple[bool, float]:
    if not avg or avg <= 0:
        return False, 0.0
    ratio = vol / avg
    return ratio >= config.SWING_TRADE_VOLUME_MULT, ratio


def _find_breakout_bar(
    df: pd.DataFrame,
    resistance_series: pd.Series,
    vol_sma: pd.Series,
) -> Optional[dict]:
    """Most recent bullish candle (within the lookback window) that closes
    above resistance on a volume spike."""
    n = len(df)
    look = min(config.SWING_TRADE_BREAKOUT_LOOKBACK_WEEKS, n - 1)
    start = max(1, n - look)
    best = None
    for i in range(start, n):
        level = float(resistance_series.iloc[i])
        if level <= 0:
            continue
        close_i = float(df["close"].iloc[i])
        open_i = float(df["open"].iloc[i])
        buffer = level * (config.SWING_TRADE_BREAKOUT_BUFFER_PCT / 100.0)
        if close_i < level + buffer or close_i < open_i:
            continue
        spike_ok, ratio = _volume_spike(
            float(df["volume"].iloc[i]),
            float(vol_sma.iloc[i]) if pd.notna(vol_sma.iloc[i]) else None,
        )
        if not spike_ok:
            continue
        if best is None or i > best["bar_index"]:
            best = {
                "bar_index": i,
                "resistance_at_break": level,
                "breakout_high": float(df["high"].iloc[i]),
                "breakout_low": float(df["low"].iloc[i]),
                "breakout_close": close_i,
                "volume_ratio": ratio,
            }
    return best


def _status_after_breakout(df: pd.DataFrame, breakout: dict) -> tuple[str, Optional[int]]:
    n = len(df)
    bi = breakout["bar_index"]
    for i in range(bi + 1, n):
        if float(df["close"].iloc[i]) > breakout["breakout_high"]:
            return "TRIGGERED", i
    return "CANDIDATE", None


def _detect_retest(df: pd.DataFrame, breakout: dict, atr: float) -> tuple[bool, Optional[int]]:
    n = len(df)
    bi = breakout["bar_index"]
    level = breakout["resistance_at_break"]
    band = max(_touch_band(level, atr), level * (config.SWING_TRADE_RETEST_BAND_PCT / 100.0))
    max_i = min(n, bi + 1 + config.SWING_TRADE_RETEST_MAX_WEEKS)
    for i in range(bi + 1, max_i):
        low_i = float(df["low"].iloc[i])
        close_i = float(df["close"].iloc[i])
        if low_i <= level + band and close_i >= level - band * 0.5:
            return True, i
    return False, None


def _pressing_resistance(df: pd.DataFrame, resistance_series: pd.Series, atr: float) -> Optional[dict]:
    """No qualifying breakout yet, but the latest weekly bar is testing this
    resistance from below (or has poked into it without a volume-confirmed
    close) — the "still forming, not yet broken out" case."""
    level = float(resistance_series.iloc[-1])
    if level <= 0:
        return None
    close_now = float(df["close"].iloc[-1])
    high_now = float(df["high"].iloc[-1])
    band = _touch_band(level, atr)
    watch_band = max(band, level * (config.SWING_TRADE_WATCH_BAND_PCT / 100.0))
    near = (close_now >= level - watch_band) and (close_now <= level + band)
    touched = high_now >= level - band
    if not (near or touched):
        return None
    return {
        "resistance_at_break": level,
        "distance_pct": round((close_now - level) / level * 100.0, 2),
    }


def _evaluate_resistance(
    df: pd.DataFrame,
    vol_sma: pd.Series,
    resistance_series: pd.Series,
    atr: float,
    *,
    resistance_type: str,
    kind: str,
    touch_count: int,
    i_start: int,
    quality_bonus: float,
) -> Optional[dict]:
    n = len(df)
    breakout = _find_breakout_bar(df, resistance_series, vol_sma)
    if breakout is not None and (n - 1 - breakout["bar_index"]) > config.SWING_TRADE_TRIGGER_MAX_WEEKS:
        breakout = None  # too stale to trade — fall through to the "still pressing" check below

    if breakout is not None:
        bi = breakout["bar_index"]
        status, trigger_i = _status_after_breakout(df, breakout)
        retest, retest_i = _detect_retest(df, breakout, atr)
        close_now = float(df["close"].iloc[-1])
        dist_from_high_pct = (close_now - breakout["breakout_high"]) / breakout["breakout_high"] * 100.0

        # Already chased too far past the breakout — avoid showing it as if
        # it were still a fresh, tradeable setup.
        if status == "TRIGGERED" and dist_from_high_pct > config.SWING_TRADE_MAX_EXTENSION_PCT:
            return None

        quality = (
            min(touch_count, 6) * 10
            + min(breakout["volume_ratio"], 6.0) * 6
            + (15 if status == "TRIGGERED" else 8)
            + (10 if retest else 0)
            + max(0, 10 - (n - 1 - bi))
            + quality_bonus
        )
        quality = max(0.0, min(100.0, quality))

        return {
            "resistance_type": resistance_type,
            "kind": kind,
            "resistance_price": round(breakout["resistance_at_break"], 2),
            "breakout_bar_index": bi,
            "breakout_week": _bar_time(df, bi),
            "breakout_high": round(breakout["breakout_high"], 2),
            "breakout_low": round(breakout["breakout_low"], 2),
            "volume_ratio": round(breakout["volume_ratio"], 2),
            "status": status,
            "trigger_bar_index": trigger_i,
            "trigger_week": _bar_time(df, trigger_i) if trigger_i is not None else None,
            "retest": retest,
            "retest_bar_index": retest_i,
            "retest_week": _bar_time(df, retest_i) if retest_i is not None else None,
            "distance_from_breakout_high_pct": round(dist_from_high_pct, 2),
            "touch_count": touch_count,
            "quality": quality,
            "geometry": {"kind": kind, "i_start": i_start},
        }

    # No qualifying (or fresh-enough) breakout — surface it as WATCHING if
    # price is currently pressing against this resistance without volume
    # confirmation yet.
    pressing = _pressing_resistance(df, resistance_series, atr)
    if pressing is None:
        return None

    quality = min(100.0, touch_count * 8 + max(0, 8 - abs(pressing["distance_pct"])) + quality_bonus * 0.5)

    return {
        "resistance_type": resistance_type,
        "kind": kind,
        "resistance_price": round(pressing["resistance_at_break"], 2),
        "breakout_bar_index": None,
        "breakout_week": None,
        "breakout_high": None,
        "breakout_low": None,
        "volume_ratio": None,
        "status": "WATCHING",
        "trigger_bar_index": None,
        "trigger_week": None,
        "retest": False,
        "retest_bar_index": None,
        "retest_week": None,
        "distance_from_breakout_high_pct": pressing["distance_pct"],
        "touch_count": touch_count,
        "quality": quality,
        "geometry": {"kind": kind, "i_start": i_start},
    }


def find_horizontal_resistances(df: pd.DataFrame, vol_sma: pd.Series, atr: float) -> list[dict]:
    """Cluster swing highs into a flat resistance zone (also covers the
    rounded/cup-top shape — it's a horizontal level touched repeatedly)."""
    pivots = _major_swing_highs(df)
    if len(pivots) < 2:
        return []

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
        if len(members) >= config.SWING_TRADE_MIN_HORIZONTAL_TOUCHES:
            clusters.append(members)

    hits = []
    for members in clusters:
        level = float(np.median([m[1] for m in members]))
        resistance_series = pd.Series(level, index=df.index)
        event = _evaluate_resistance(
            df, vol_sma, resistance_series, atr,
            resistance_type="Horizontal resistance",
            kind="horizontal",
            touch_count=len(members),
            i_start=min(m[0] for m in members),
            quality_bonus=len(members) * 4,
        )
        if event:
            event["geometry"]["level"] = level
            hits.append(event)
    return hits


def find_descending_trendline_resistances(df: pd.DataFrame, vol_sma: pd.Series, atr: float) -> list[dict]:
    pivots = _major_swing_highs(df)
    if len(pivots) < 2:
        return []

    recent = pivots[-config.SWING_TRADE_MAX_PIVOTS_FOR_LINE:]
    n = len(df)
    hits = []
    for (i1, p1), (i2, p2) in combinations(recent, 2):
        if i2 <= i1 or (i2 - i1) < config.SWING_TRADE_MIN_TREND_SEP:
            continue
        slope, intercept = _fit_line(i1, p1, i2, p2)
        if slope > -(p1 * 0.0002):  # must be meaningfully declining
            continue

        resistance_series = pd.Series(
            [_line_at(slope, intercept, i) for i in range(n)], index=df.index
        )
        touches = [(i1, p1), (i2, p2)]
        for idx, price in pivots:
            if idx in (i1, i2) or idx < i1 or idx > n - 1:
                continue
            level = _line_at(slope, intercept, idx)
            if abs(price - level) <= _touch_band(level, atr):
                touches.append((idx, price))
        if len(touches) < 2:
            continue

        event = _evaluate_resistance(
            df, vol_sma, resistance_series, atr,
            resistance_type="Descending trendline",
            kind="descending",
            touch_count=len(touches),
            i_start=i1,
            quality_bonus=12.0,
        )
        if event:
            event["geometry"]["slope"] = slope
            event["geometry"]["intercept"] = intercept
            hits.append(event)
    return hits


def detect_resistance_event(df: pd.DataFrame) -> Optional[dict]:
    """Best resistance-breakout event (horizontal box/cup-top or descending
    trendline) among the last few weeks of weekly candles."""
    if len(df) < 30:
        return None
    atr_s = ind.atr(df["high"], df["low"], df["close"], 14)
    atr = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else float(
        df["high"].iloc[-1] - df["low"].iloc[-1]
    )
    vol_sma = ind.sma(df["volume"].astype(float), config.SWING_TRADE_VOLUME_SMA_WEEKS)

    candidates: list[dict] = []
    candidates.extend(find_horizontal_resistances(df, vol_sma, atr))
    candidates.extend(find_descending_trendline_resistances(df, vol_sma, atr))
    if not candidates:
        return None

    status_rank = {"TRIGGERED": 2, "CANDIDATE": 1, "WATCHING": 0}
    candidates.sort(
        key=lambda c: (
            status_rank.get(c.get("status"), 0),
            c.get("quality", 0),
            c.get("touch_count", 0),
        ),
        reverse=True,
    )
    return candidates[0]


def _ema_cross_info(df: pd.DataFrame) -> dict:
    fast_p = config.SWING_TRADE_EMA_FAST
    slow_p = config.SWING_TRADE_EMA_SLOW
    if len(df) < slow_p + 2:
        return {"ema_fast": None, "ema_slow": None, "ema_bullish_cross": False, "price_above_emas": False}

    fast = ind.ema(df["close"], fast_p)
    slow = ind.ema(df["close"], slow_p)
    f0 = float(fast.iloc[-1])
    s0 = float(slow.iloc[-1])
    close_now = float(df["close"].iloc[-1])

    lookback = min(len(df) - 1, config.SWING_TRADE_BREAKOUT_LOOKBACK_WEEKS + 2)
    recent_cross = False
    for i in range(len(df) - lookback, len(df)):
        if i <= 0:
            continue
        if float(fast.iloc[i - 1]) <= float(slow.iloc[i - 1]) and float(fast.iloc[i]) > float(slow.iloc[i]):
            recent_cross = True
            break

    return {
        "ema_fast": round(f0, 2),
        "ema_slow": round(s0, 2),
        "ema_bullish_cross": bool(recent_cross or f0 > s0),
        "price_above_emas": bool(close_now > f0 and close_now > s0),
    }


def _trade_levels(event: dict, atr_val: float) -> dict:
    # WATCHING rows have no breakout candle yet — the level to watch for is
    # the resistance itself ("wait for a weekly close above ₹X").
    entry = event.get("breakout_high") or event["resistance_price"]
    raw_stop = entry - config.SWING_TRADE_SL_ATR_MULT * atr_val
    stop = min(raw_stop, event["resistance_price"] * 0.995)
    target = entry + config.SWING_TRADE_TP_ATR_MULT * atr_val
    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0.0
    return {
        "entry_price": round(entry, 2),
        "stop_loss": round(stop, 2),
        "target": round(target, 2),
        "risk_reward": rr,
    }


def build_swing_chart_payload(symbol: str, df: pd.DataFrame, event: Optional[dict] = None) -> dict:
    """Weekly OHLC candles + volume + resistance line/box + BREAKOUT / RE-TEST
    / CONTINUATION markers."""
    event = event or {}
    candles = []
    volume = []
    for i in range(len(df)):
        o = float(df["open"].iloc[i])
        c = float(df["close"].iloc[i])
        t = _bar_time(df, i)
        candles.append({
            "time": t,
            "open": round(o, 2),
            "high": round(float(df["high"].iloc[i]), 2),
            "low": round(float(df["low"].iloc[i]), 2),
            "close": round(c, 2),
        })
        volume.append({
            "time": t,
            "value": float(df["volume"].iloc[i]),
            "color": "#22c58a" if c >= o else "#f0554a",
        })

    lines = []
    geom = event.get("geometry") or {}
    kind = geom.get("kind") or event.get("kind")
    if kind == "horizontal":
        level = geom.get("level") or event.get("resistance_price")
        i_start = int(geom.get("i_start") or 0)
        if level:
            lines.append({
                "name": "Resistance",
                "color": "#f0554a",
                "points": _line_points(df, 0.0, float(level), i_start),
            })
    elif kind == "descending":
        slope = geom.get("slope")
        intercept = geom.get("intercept")
        i_start = int(geom.get("i_start") or 0)
        if slope is not None:
            lines.append({
                "name": "Resistance trendline",
                "color": "#f0554a",
                "points": _line_points(df, slope, intercept, i_start),
            })

    markers = []
    i_start = geom.get("i_start")
    if i_start is not None and 0 <= int(i_start) < len(df):
        markers.append({
            "time": _bar_time(df, int(i_start)), "position": "aboveBar",
            "color": "#f0554a", "shape": "circle", "text": "Resistance",
        })
    bi = event.get("breakout_bar_index")
    if bi is not None:
        vol_ratio = event.get("volume_ratio")
        vol_txt = f" ⚡{vol_ratio}x VOL" if vol_ratio else ""
        markers.append({
            "time": _bar_time(df, bi), "position": "belowBar",
            "color": "#f0b429", "shape": "arrowUp", "text": f"BREAKOUT{vol_txt}",
        })
    ri = event.get("retest_bar_index")
    if ri is not None:
        markers.append({
            "time": _bar_time(df, ri), "position": "belowBar",
            "color": "#3d8bfd", "shape": "circle", "text": "RE-TEST",
        })
    ti = event.get("trigger_bar_index")
    if ti is not None:
        markers.append({
            "time": _bar_time(df, ti), "position": "aboveBar",
            "color": "#22c58a", "shape": "arrowUp", "text": "CONTINUATION",
        })

    breakout_high = event.get("breakout_high")
    resistance_price = event.get("resistance_price")
    status = event.get("status")
    if status == "WATCHING" and resistance_price and candles:
        markers.append({
            "time": candles[-1]["time"], "position": "belowBar",
            "color": "#9a998f", "shape": "circle", "text": "WATCHING",
        })
        status_message = f"Pressing resistance ₹{resistance_price} — no breakout/volume spike yet"
    elif status == "CANDIDATE" and breakout_high:
        status_message = f"Wait to break the breakout candle's high — ₹{breakout_high}"
    elif status == "TRIGGERED" and breakout_high:
        status_message = f"Triggered — closed above breakout high ₹{breakout_high}"
    else:
        status_message = None

    structure_markers, structure_line = market_structure.structure_chart_extras(
        df, config.SWING_TRADE_PIVOT_LENGTH
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
        "resistance_type": event.get("resistance_type"),
        "status": status,
        "status_message": status_message,
        "resistance_price": event.get("resistance_price"),
        "breakout_high": breakout_high,
        "volume_ratio": event.get("volume_ratio"),
        "retest": event.get("retest", False),
        "candles": candles,
        "volume": volume,
        "lines": lines,
        "markers": markers,
        "structure_markers": structure_markers,
        "tv_chart_url": tradingview_chart_url(symbol, interval="W"),
    }


def rebuild_chart_for_symbol(symbol: str, daily: pd.DataFrame) -> Optional[dict]:
    """Re-detect the resistance event and build the chart payload (used when
    the cache is cold, e.g. after a reloader restart)."""
    weekly = _weekly_from_daily(daily)
    if weekly.empty or len(weekly) < 30:
        return None
    event = detect_resistance_event(weekly) or {}
    payload = build_swing_chart_payload(symbol, weekly, event)
    store_chart_payload(symbol, payload)
    return payload


def evaluate_symbol_swing_trade(symbol: str, daily: pd.DataFrame) -> Optional[dict]:
    if daily is None or daily.empty:
        return None
    weekly = _weekly_from_daily(daily)
    if weekly.empty or len(weekly) < 30:
        return None

    event = detect_resistance_event(weekly)
    if event is None:
        return None
    min_score = (
        config.SWING_TRADE_MIN_SCORE_WATCHING if event.get("status") == "WATCHING"
        else config.SWING_TRADE_MIN_SCORE
    )
    if event.get("quality", 0) < min_score:
        return None

    atr_s = ind.atr(weekly["high"], weekly["low"], weekly["close"], 14)
    atr_val = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else float(
        weekly["high"].iloc[-1] - weekly["low"].iloc[-1]
    )
    ema_info = _ema_cross_info(weekly)
    levels = _trade_levels(event, atr_val)

    chart_payload = build_swing_chart_payload(symbol, weekly, event)
    store_chart_payload(symbol, chart_payload)

    return {
        "symbol": symbol,
        "resistance_type": event["resistance_type"],
        "status": event["status"],
        "resistance_price": event["resistance_price"],
        "current_price": round(float(weekly["close"].iloc[-1]), 2),
        "breakout_week": event["breakout_week"],
        "breakout_high": event["breakout_high"],
        "trigger_week": event["trigger_week"],
        "distance_from_breakout_high_pct": event["distance_from_breakout_high_pct"],
        "volume_ratio": event["volume_ratio"],
        "touch_count": event["touch_count"],
        "retest": event["retest"],
        "retest_week": event["retest_week"],
        "ema_fast": ema_info["ema_fast"],
        "ema_slow": ema_info["ema_slow"],
        "ema_bullish_cross": ema_info["ema_bullish_cross"],
        "quality": round(event["quality"], 1),
        **levels,
        "timestamp": pd.to_datetime(weekly["date"].iloc[-1]).isoformat(),
        "chart_url": local_chart_url(symbol),
        "tv_chart_url": tradingview_chart_url(symbol, interval="W"),
    }


def scan_swing_trade(
    kite_client,
    universe_df=None,
    universe_mode: Optional[str] = None,
) -> dict:
    try:
        mode = universe_mod.normalize_nifty_mode(
            universe_mode or config.SWING_TRADE_UNIVERSE or "nifty200"
        )
    except ValueError:
        mode = "nifty200"

    try:
        scan_df = universe_mod.build_nifty_index_universe(kite_client, mode)
    except Exception as e:
        print(f"  [warn] swing-trade universe {mode} failed ({e}) — falling back")
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
            "charts": {},
        }

    today = datetime.now().date()
    from_date = today - timedelta(days=config.SWING_TRADE_LOOKBACK_DAYS)
    results = []
    scanned = 0
    clear_chart_cache()
    print(f"Swing trade: scanning {label} ({len(scan_df)} symbols)...")

    for _, row in tqdm(scan_df.iterrows(), total=len(scan_df), desc=f"Swing trade ({label})"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        scanned += 1
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, today)
            if daily.empty or len(daily) < 150:
                continue
            hit = evaluate_symbol_swing_trade(symbol, daily)
            if hit:
                results.append(hit)
                if hit.get("status") == "TRIGGERED":
                    if config.AUTO_TRACK_ENABLED:
                        try:
                            trade_tracker.auto_track_if_new(
                                symbol=symbol, source="swing_trade",
                                entry_price=hit.get("entry_price") or hit.get("current_price"),
                                stop_loss=hit.get("stop_loss"), target=hit.get("target"),
                                chart_url=hit.get("chart_url"),
                            )
                        except Exception as e:
                            print(f"  [warn] swing-trade auto-track failed for {symbol}: {e}")
                    if config.SWING_TRADE_SEND_TELEGRAM:
                        key = f"{symbol}|{hit.get('breakout_week')}|{hit.get('resistance_type')}"
                        if key not in _alerted_breakouts:
                            _alerted_breakouts.add(key)
                            try:
                                telegram_alerts.send_telegram_message(
                                    telegram_alerts.format_swing_trade_alert(hit)
                                )
                            except Exception as e:
                                print(f"  [warn] swing-trade telegram alert failed for {symbol}: {e}")
        except Exception as e:
            print(f"  [warn] swing-trade skipped {symbol}: {e}")
            continue

    status_rank = {"TRIGGERED": 2, "CANDIDATE": 1, "WATCHING": 0}
    results.sort(
        key=lambda r: (
            status_rank.get(r.get("status"), 0),
            r.get("quality") or 0,
            r.get("volume_ratio") or 0,
        ),
        reverse=True,
    )
    charts = {
        str(r["symbol"]).upper(): get_chart_payload(r["symbol"])
        for r in results
        if get_chart_payload(r["symbol"]) is not None
    }
    print(f"Swing trade: {len(results)} hit(s) of {scanned} scanned ({label})")
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
