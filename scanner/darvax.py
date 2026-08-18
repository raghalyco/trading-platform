"""
DarvaX scanner — a proper Darvas Box breakout screen, daily timeframe.

Two theories, cross-checked against the source PDF (DARVAX.pdf, Amitabh
Jha) and the original Nicolas Darvas box theory (TrendSpider / Tradingsim /
Tradejini writeups):

  BOX CONSTRUCTION (classic Darvas, 1950s):
    1. Track the running high. A NEW high becomes the "candidate box top"
       and resets the confirmation counter.
    2. If DARVAX_CONFIRM_BARS consecutive sessions pass WITHOUT a new high,
       the candidate top is confirmed as the box top.
    3. From there, track the lowest low seen since the peak (including the
       stall bars that confirmed the top itself). If DARVAX_CONFIRM_BARS
       consecutive sessions pass without a new low, that low is confirmed
       as the box bottom -> the box is fully formed.
    4. BREAKOUT: close crosses above the box top -> buy signal, ideally on
       a volume surge (institutional confirmation).
    5. STOP-LOSS: just below the box bottom - not an arbitrary %. A close
       back below the box bottom invalidates the box.
    6. Pyramiding: a new, higher box forming above means the old top
       becomes the new floor/stop - never average DOWN into a falling box.

  DARVAX OVERLAY (Amitabh Jha's PDF):
    - simplified fast trigger = close above previous day's high (shown as
      secondary reference, not the primary signal here)
    - 1% flat SL, or tiered EMA(5/10/20/200) SL by holding style, shown
      alongside the box-bottom SL as secondary reference
    - bias toward stocks already near their all-time high ("uncharted
      territory") - a ranking factor here, not a hard filter, so mid-range
      valid boxes aren't excluded
    - never average down; only add on a new, higher box (pyramiding)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import pandas as pd
from tqdm import tqdm

import config
import indicators as ind
import smart_money_strategy as sms
import trade_tracker
import universe as universe_mod
from charts import tradingview_chart_url

_CHART_CACHE: dict[str, dict] = {}


def _cache_key(symbol: str, timeframe: str = "daily") -> str:
    suffix = "_W" if timeframe == "weekly" else ""
    return f"{str(symbol).upper()}{suffix}"


def local_chart_url(symbol: str, timeframe: str = "daily") -> str:
    suffix = "?tf=weekly" if timeframe == "weekly" else ""
    return f"/darvax-chart/{quote(str(symbol).upper(), safe='')}{suffix}"


def get_chart_payload(symbol: str, timeframe: str = "daily") -> Optional[dict]:
    return _CHART_CACHE.get(_cache_key(symbol, timeframe))


def store_chart_payload(symbol: str, payload: dict, timeframe: str = "daily") -> None:
    _CHART_CACHE[_cache_key(symbol, timeframe)] = payload


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


def _bar_date(df: pd.DataFrame, i: int) -> str:
    return pd.to_datetime(df["date"].iloc[i]).strftime("%Y-%m-%d")


def _weekly_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    return sms._resample_ohlcv(daily, "W-FRI")


def _timeframe_params(timeframe: str) -> dict:
    """Every threshold that differs between the daily and weekly variant,
    in one place. 'weekly' mirrors the same box mechanic but on
    week-resampled candles - the deck's own preference ("the higher the
    timeframe, the higher the respect")."""
    if timeframe == "weekly":
        return {
            "confirm_bars": config.DARVAX_WEEKLY_CONFIRM_BARS,
            "breakout_lookback": config.DARVAX_WEEKLY_BREAKOUT_LOOKBACK_WEEKS,
            "volume_sma_bars": config.DARVAX_WEEKLY_VOLUME_SMA_WEEKS,
            "volume_mult": config.DARVAX_WEEKLY_VOLUME_MULT,
            "max_box_age": config.DARVAX_WEEKLY_MAX_BOX_AGE_WEEKS,
            "min_score": config.DARVAX_WEEKLY_MIN_SCORE,
            "min_bars": 30,
            "unit": "week",
        }
    return {
        "confirm_bars": config.DARVAX_CONFIRM_BARS,
        "breakout_lookback": config.DARVAX_BREAKOUT_LOOKBACK_DAYS,
        "volume_sma_bars": config.DARVAX_VOLUME_SMA_DAYS,
        "volume_mult": config.DARVAX_VOLUME_MULT,
        "max_box_age": config.DARVAX_MAX_BOX_AGE_DAYS,
        "min_score": config.DARVAX_MIN_SCORE,
        "min_bars": 60,
        "unit": "day",
    }


def find_darvas_boxes(df: pd.DataFrame, confirm_bars: int = None) -> list[dict]:
    """Replays Darvas's box state machine over the whole series. Returns
    every completed breakout: box top/bottom + indices + breakout index.
    Timeframe-agnostic - pass a daily OR weekly-resampled df."""
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    close = df["close"].astype(float).values
    n = len(df)
    confirm_bars = confirm_bars or config.DARVAX_CONFIRM_BARS

    boxes: list[dict] = []
    candidate_top = None
    candidate_top_idx = None
    top_confirm = 0
    stall_min_low = None
    box_top = None
    box_top_idx = None
    candidate_bottom = None
    bottom_confirm = 0
    box_bottom = None
    box_bottom_idx = None

    def reset(i):
        nonlocal candidate_top, candidate_top_idx, top_confirm, stall_min_low
        nonlocal box_top, box_top_idx, candidate_bottom, bottom_confirm, box_bottom, box_bottom_idx
        candidate_top, candidate_top_idx, top_confirm = high[i], i, 0
        stall_min_low = None
        box_top, box_top_idx = None, None
        candidate_bottom, bottom_confirm, box_bottom, box_bottom_idx = None, 0, None, None

    for i in range(n):
        h, l, c = high[i], low[i], close[i]

        # Box fully formed -> a new high is the breakout itself, not just a
        # fresh candidate top. Must be checked BEFORE the "new candidate
        # top" branch below, or every breakout bar (whose high necessarily
        # exceeds candidate_top) gets silently swallowed as a re-search
        # instead of being recorded as a signal.
        if box_top is not None and box_bottom is not None:
            if c > box_top:
                boxes.append({
                    "box_top": box_top, "box_top_idx": box_top_idx,
                    "box_bottom": box_bottom, "box_bottom_idx": box_bottom_idx,
                    "breakout_idx": i, "breakout_close": c,
                })
                reset(i)
            elif c < box_bottom:
                reset(i)
            continue

        if candidate_top is None or h > candidate_top:
            reset(i)
            continue

        if box_top is None:
            top_confirm += 1
            stall_min_low = l if stall_min_low is None else min(stall_min_low, l)
            if top_confirm >= confirm_bars:
                box_top, box_top_idx = candidate_top, candidate_top_idx
                candidate_bottom, bottom_confirm = stall_min_low, 0
            continue

        if box_bottom is None:
            if l < candidate_bottom:
                candidate_bottom, bottom_confirm = l, 0
            else:
                bottom_confirm += 1
                if bottom_confirm >= confirm_bars:
                    box_bottom, box_bottom_idx = candidate_bottom, i
            continue

    return boxes


def _ema_tiers(df: pd.DataFrame) -> dict:
    close = df["close"].astype(float)
    out = {}
    for label, period in config.DARVAX_EMA_TIERS.items():
        if len(close) < period + 1:
            out[label] = None
            continue
        out[label] = round(float(ind.ema(close, period).iloc[-1]), 2)
    return out


def _entry_basis(box_height_pct: float, vol_ratio: float, dist_from_ath_pct: float,
                  box_age_bars: int, params: dict) -> list[str]:
    unit = params["unit"]
    plural = unit + ("s" if box_age_bars != 1 else "")
    basis = [
        f"Box confirmed by {params['confirm_bars']} {unit}s with no new high, then "
        f"{params['confirm_bars']} {unit}s with no new low ({box_age_bars}-{unit} base).",
        f"Box height {box_height_pct}% - tighter bases carry more conviction (VCP-style).",
        f"Breakout close cleared the box top on {vol_ratio}x the {params['volume_sma_bars']}-{unit} "
        f"average volume (min required {params['volume_mult']}x) - Darvas's institutional-interest gate.",
    ]
    if dist_from_ath_pct <= 5:
        basis.append(f"Only {dist_from_ath_pct}% off its high in this window - \"uncharted "
                      "territory\", the DarvaX selection bias.")
    else:
        basis.append(f"{dist_from_ath_pct}% off its high in this window - a mid-range box, "
                      "not a fresh-high breakout; weighted lower but not excluded.")
    return basis


def evaluate_symbol_darvax(symbol: str, daily: pd.DataFrame, timeframe: str = "daily") -> Optional[dict]:
    """timeframe: 'daily' or 'weekly'. Weekly resamples first, per the
    deck's preference for higher-timeframe boxes."""
    params = _timeframe_params(timeframe)
    if daily is None or len(daily) < (params["min_bars"] * 7 if timeframe == "weekly" else params["min_bars"]):
        return None

    df = _weekly_from_daily(daily) if timeframe == "weekly" else daily.reset_index(drop=True)
    if len(df) < params["min_bars"]:
        return None
    n = len(df)

    boxes = find_darvas_boxes(df, confirm_bars=params["confirm_bars"])
    if not boxes:
        return None

    last = boxes[-1]
    breakout_idx = last["breakout_idx"]
    if (n - 1 - breakout_idx) > params["breakout_lookback"]:
        return None
    box_age_bars = last["breakout_idx"] - last["box_top_idx"]
    if box_age_bars > params["max_box_age"]:
        return None

    vol = df["volume"].astype(float)
    vol_sma = vol.rolling(params["volume_sma_bars"]).mean()
    avg_vol = vol_sma.iloc[breakout_idx - 1] if breakout_idx > 0 else None
    if avg_vol is None or pd.isna(avg_vol) or avg_vol <= 0:
        return None
    vol_ratio = float(vol.iloc[breakout_idx] / avg_vol)
    if vol_ratio < params["volume_mult"]:
        return None

    close = df["close"].astype(float)
    today_close = float(close.iloc[-1])
    window_high = float(df["high"].astype(float).iloc[: n].max())
    dist_from_ath_pct = round((window_high - today_close) / window_high * 100.0, 2)

    box_top = float(last["box_top"])
    box_bottom = float(last["box_bottom"])
    box_height_pct = round((box_top - box_bottom) / box_bottom * 100.0, 2)
    entry = float(last["breakout_close"])

    # Post-breakout status: has price since fallen back through the box
    # bottom (Darvas's hard invalidation rule)? Stale cached data can hide
    # this - always check against whatever's actually been fetched.
    invalidated = today_close < box_bottom
    status = "INVALIDATED" if invalidated else ("TRIGGERED" if n - 1 > breakout_idx else "BREAKOUT")

    score = (
        min(vol_ratio, 6.0) * 8
        + max(0, 15 - box_height_pct) * 2
        + max(0, 10 - dist_from_ath_pct) * 3
        + max(0, 5 - (n - 1 - breakout_idx)) * 2
    )
    score = max(0.0, min(100.0, score))
    if score < params["min_score"]:
        return None

    ema_tiers = _ema_tiers(df)

    chart_payload = build_darvax_chart_payload(symbol, df, last, {
        "status": status,
        "box_top": box_top, "box_bottom": box_bottom,
        "box_height_pct": box_height_pct,
        "volume_ratio": round(vol_ratio, 2),
        "dist_from_ath_pct": dist_from_ath_pct,
        "box_age_bars": box_age_bars,
        "ema_tiers": ema_tiers,
        "params": params,
    })
    store_chart_payload(symbol, chart_payload, timeframe=timeframe)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "status": status,
        "box_top": round(box_top, 2),
        "box_bottom": round(box_bottom, 2),
        "box_height_pct": box_height_pct,
        "box_formed_bars": box_age_bars,
        "breakout_date": _bar_date(df, breakout_idx),
        "breakout_close": round(entry, 2),
        "current_close": round(today_close, 2),
        "volume_ratio": round(vol_ratio, 2),
        "dist_from_ath_pct": dist_from_ath_pct,
        "stop_loss_box_bottom": round(box_bottom, 2),
        "stop_loss_darvax_1pct": round(entry * 0.99, 2),
        "ema_sl_tiers": ema_tiers,
        "quality": round(score, 1),
        "timestamp": pd.to_datetime(df["date"].iloc[-1]).isoformat(),
        "chart_url": local_chart_url(symbol, timeframe),
        "tv_chart_url": tradingview_chart_url(symbol, interval="W" if timeframe == "weekly" else "D"),
    }


def build_darvax_chart_payload(symbol: str, df: pd.DataFrame, box: dict, info: dict) -> dict:
    """Daily OHLC + volume + box top/bottom lines + ENTRY/STOP/INVALIDATED
    markers + the plain-English basis panel the chart page renders."""
    candles, volume = [], []
    for i in range(len(df)):
        o = float(df["open"].iloc[i])
        c = float(df["close"].iloc[i])
        t = _bar_date(df, i)
        candles.append({
            "time": t, "open": round(o, 2), "high": round(float(df["high"].iloc[i]), 2),
            "low": round(float(df["low"].iloc[i]), 2), "close": round(c, 2),
        })
        volume.append({
            "time": t, "value": float(df["volume"].iloc[i]),
            "color": "#22c58a" if c >= o else "#f0554a",
        })

    top_idx = int(box["box_top_idx"])
    bottom_idx = int(box["box_bottom_idx"])
    breakout_idx = int(box["breakout_idx"])
    n = len(df)

    def line_points(value, i_start, i_end):
        return [
            {"time": _bar_date(df, i_start), "value": round(value, 2)},
            {"time": _bar_date(df, i_end), "value": round(value, 2)},
        ]

    lines = [
        {"name": "Box top (resistance)", "color": "#f0554a",
         "points": line_points(info["box_top"], top_idx, n - 1)},
        {"name": "Box bottom (stop-loss)", "color": "#3d8bfd", "dashed": True,
         "points": line_points(info["box_bottom"], bottom_idx, n - 1)},
    ]

    markers = [
        {"time": _bar_date(df, top_idx), "position": "aboveBar", "color": "#f0554a",
         "shape": "circle", "text": "Box top"},
        {"time": _bar_date(df, bottom_idx), "position": "belowBar", "color": "#3d8bfd",
         "shape": "circle", "text": "Box bottom"},
        {"time": _bar_date(df, breakout_idx), "position": "belowBar", "color": "#f0b429",
         "shape": "arrowUp",
         "text": f"ENTRY ⚡{info['volume_ratio']}x VOL"},
    ]
    if info["status"] == "INVALIDATED":
        markers.append({
            "time": _bar_date(df, n - 1), "position": "aboveBar", "color": "#f0554a",
            "shape": "arrowDown", "text": "EXIT — box bottom broken",
        })

    params = info.get("params") or _timeframe_params("daily")
    entry_basis = _entry_basis(
        info["box_height_pct"], info["volume_ratio"], info["dist_from_ath_pct"],
        info["box_age_bars"], params,
    )

    if info["status"] == "INVALIDATED":
        status_message = (
            f"INVALIDATED — close ({round(float(df['close'].iloc[-1]), 2)}) has fallen back "
            f"below the box bottom (₹{round(info['box_bottom'], 2)}). Per Darvas's rule this "
            f"exits the position; do not hold hoping it recovers."
        )
    else:
        status_message = (
            f"Active — entry ₹{round(box['breakout_close'], 2)}, stop-loss ₹{round(info['box_bottom'], 2)} "
            f"(box bottom). Exits automatically if closed below the stop."
        )

    return {
        "symbol": symbol,
        "status": info["status"],
        "status_message": status_message,
        "box_top": round(info["box_top"], 2),
        "box_bottom": round(info["box_bottom"], 2),
        "box_height_pct": info["box_height_pct"],
        "breakout_close": round(box["breakout_close"], 2),
        "volume_ratio": info["volume_ratio"],
        "dist_from_ath_pct": info["dist_from_ath_pct"],
        "candles": candles,
        "volume": volume,
        "lines": lines,
        "markers": markers,
        "entry_basis": entry_basis,
        "entry_rule": (
            f"Enter on a close above the box top (₹{round(info['box_top'], 2)}), confirmed by "
            f"volume ≥ {params['volume_mult']}x the {params['volume_sma_bars']}-{params['unit']} average. "
            f"DarvaX's faster/noisier alternative: enter above the previous day's high with a flat 1% stop."
        ),
        "exit_rule": (
            f"Stop-loss / exit: a close back below the box bottom (₹{round(info['box_bottom'], 2)}) "
            f"invalidates the setup — exit immediately, no exceptions. DarvaX's alternative "
            f"tiered reference: exit below the {'/'.join(str(v) for v in config.DARVAX_EMA_TIERS.values())} "
            f"EMA depending on how long you meant to hold (5=very short term, 10=swing, "
            f"20=positional, 200=investor)."
        ),
        "averaging_rule": (
            "Never average down into a falling box — that is explicitly against Darvas's theory. "
            "The only allowed addition is pyramiding UP: once a NEW, higher box forms above this "
            "breakout, you may add there, and the old box top becomes the new stop-loss floor."
        ),
        "ema_sl_tiers": info["ema_tiers"],
        "tv_chart_url": tradingview_chart_url(symbol, interval="W" if params["unit"] == "week" else "D"),
    }


def rebuild_chart_for_symbol(symbol: str, daily: pd.DataFrame, timeframe: str = "daily") -> Optional[dict]:
    """Re-detect the box + rebuild the chart payload (used when the cache
    is cold, e.g. after a reloader restart)."""
    params = _timeframe_params(timeframe)
    if daily is None or daily.empty or len(daily) < params["min_bars"]:
        return None
    df = _weekly_from_daily(daily) if timeframe == "weekly" else daily.reset_index(drop=True)
    if len(df) < params["min_bars"]:
        return None
    boxes = find_darvas_boxes(df, confirm_bars=params["confirm_bars"])
    if not boxes:
        return None
    last = boxes[-1]
    n = len(df)
    breakout_idx = last["breakout_idx"]

    vol = df["volume"].astype(float)
    vol_sma = vol.rolling(params["volume_sma_bars"]).mean()
    avg_vol = vol_sma.iloc[breakout_idx - 1] if breakout_idx > 0 else None
    vol_ratio = float(vol.iloc[breakout_idx] / avg_vol) if avg_vol and avg_vol > 0 else 0.0

    close = df["close"].astype(float)
    today_close = float(close.iloc[-1])
    window_high = float(df["high"].astype(float).iloc[:n].max())
    dist_from_ath_pct = round((window_high - today_close) / window_high * 100.0, 2)
    box_top = float(last["box_top"])
    box_bottom = float(last["box_bottom"])
    box_height_pct = round((box_top - box_bottom) / box_bottom * 100.0, 2)
    invalidated = today_close < box_bottom
    status = "INVALIDATED" if invalidated else ("TRIGGERED" if n - 1 > breakout_idx else "BREAKOUT")
    box_age_bars = last["breakout_idx"] - last["box_top_idx"]

    payload = build_darvax_chart_payload(symbol, df, last, {
        "status": status, "box_top": box_top, "box_bottom": box_bottom,
        "box_height_pct": box_height_pct, "volume_ratio": round(vol_ratio, 2),
        "dist_from_ath_pct": dist_from_ath_pct, "box_age_bars": box_age_bars,
        "ema_tiers": _ema_tiers(df), "params": params,
    })
    store_chart_payload(symbol, payload, timeframe=timeframe)
    return payload


def scan_darvax(
    kite_client,
    universe_df=None,
    universe_mode: Optional[str] = None,
    timeframe: str = "daily",
) -> dict:
    try:
        mode = universe_mod.normalize_nifty_mode(universe_mode or config.DARVAX_UNIVERSE or "nifty200")
    except ValueError:
        mode = "nifty200"

    try:
        scan_df = universe_mod.build_nifty_index_universe(kite_client, mode)
    except Exception as e:
        print(f"  [warn] darvax universe {mode} failed ({e}) — falling back")
        scan_df = universe_df

    label = universe_mod.nifty_mode_label(mode)
    if scan_df is None or scan_df.empty:
        return {
            "generated_at": datetime.now().isoformat(), "universe_mode": mode,
            "universe_label": label, "universe_size": 0, "scanned": 0,
            "num_results": 0, "results": [], "charts": {}, "timeframe": timeframe,
        }

    lookback_days = config.DARVAX_WEEKLY_LOOKBACK_DAYS if timeframe == "weekly" else config.DARVAX_LOOKBACK_DAYS
    min_daily_bars = 300 if timeframe == "weekly" else 60
    today = datetime.now().date()
    from_date = today - timedelta(days=lookback_days)
    results = []
    scanned = 0
    clear_chart_cache()
    print(f"DarvaX ({timeframe}): scanning {label} ({len(scan_df)} symbols)...")

    for _, row in tqdm(scan_df.iterrows(), total=len(scan_df), desc=f"DarvaX {timeframe} ({label})"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        scanned += 1
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, today)
            if daily.empty or len(daily) < min_daily_bars:
                continue
            hit = evaluate_symbol_darvax(symbol, daily, timeframe=timeframe)
            if hit:
                results.append(hit)
        except Exception as e:
            print(f"  [warn] darvax skipped {symbol}: {e}")
            continue

    status_rank = {"INVALIDATED": 0, "BREAKOUT": 1, "TRIGGERED": 2}
    results.sort(key=lambda r: (status_rank.get(r.get("status"), 0), r.get("quality") or 0), reverse=True)

    # Auto-track every non-invalidated breakout so Strategy Performance
    # reflects EVERY signal this scanner produced, not just the ones
    # manually clicked "Track" - dedups on (symbol, source, entry price).
    # Off by default (config.AUTO_TRACK_ENABLED) - My Trades should only
    # show what you explicitly clicked "☆ Track" on.
    if config.AUTO_TRACK_ENABLED:
        for r in results:
            if r.get("status") == "INVALIDATED":
                continue
            try:
                trade_tracker.auto_track_if_new(
                    symbol=r["symbol"], source="darvax",
                    entry_price=r.get("breakout_close"), stop_loss=r.get("box_bottom"),
                    target=None, chart_url=r.get("chart_url"),
                )
            except Exception as e:
                print(f"  [warn] darvax auto-track failed for {r.get('symbol')}: {e}")

    charts = {
        str(r["symbol"]).upper(): get_chart_payload(r["symbol"], timeframe=timeframe)
        for r in results if get_chart_payload(r["symbol"], timeframe=timeframe) is not None
    }
    print(f"DarvaX ({timeframe}): {len(results)} hit(s) of {scanned} scanned ({label})")
    return {
        "generated_at": datetime.now().isoformat(),
        "universe_mode": mode, "universe_label": label, "universe_size": len(scan_df),
        "scanned": scanned, "num_results": len(results), "results": results, "charts": charts,
        "timeframe": timeframe,
    }
