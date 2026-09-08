"""
Breakout Radar — 8th dashboard tab.

Built after comparing this dashboard against a commercial F&O screener
("Stock Hunter Breakouts"). That tool showed four things this app didn't
have yet, all against the F&O (derivatives-eligible) universe:

  1. A relative-rotation-style quadrant tag per stock (their labels looked
     like ST/BD/FD/WK) — two independent reads on relative strength vs
     Nifty 50: its LEVEL (is the stock's RS ratio above or below its own
     trend) and its MOMENTUM (is that ratio rising or falling right now).
     Reverse-engineered from the vendor's own CSS class names
     (`early_spark` / `high_gear` / `cold_floor` / `peak_fade`), not from
     any published formula — see compute_rs_quadrant()'s docstring for the
     exact (documented, tunable) definition used here.
  2. A same-day PDH/PDL breach flag with the breach time.
  3. Opening-range-breakout status across FIVE windows at once
     (5/15/30/45/60 min) instead of just one.
  4. A "Money Flow" pair (RVOL + a directional volume-flow ratio, named
     OVS here as a proxy — the vendor never published its formula either).

Unlike the Intraday Monitor tab (which is a genuinely live, streaming
WebSocket engine — see intraday_engine.py), this is a poll/request-response
scanner, same shape as sector_scanner.py / trending_scanner.py / nday_scanner.py:
click Run Scan (or let it auto-refresh while the tab is open) and it
snapshots current data. PDH/PDL breach time, multi-timeframe ORB, and Money
Flow all need TODAY's intraday candles, so those fields are only meaningful
during/after market hours — outside market hours they come back as None/
"no_intraday_data" and only the RS quadrant (which only needs daily data)
still populates.

Universe defaults to "fno" (config.BREAKOUT_RADAR_UNIVERSE) to match what
was actually being compared against (208 F&O names), not the full NSE
universe — same reasoning as the Intraday Monitor's shortlist cap: a
5-minute-candle fetch per symbol per scan does not scale to 2000+ symbols
on Kite's rate limit.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from tqdm import tqdm

import config
import indicators as ind
import universe as universe_mod

# quadrant_code -> (quadrant_name, vendor-style short badge)
# Mapping rationale (see compute_rs_quadrant docstring): RS level x RS
# momentum, matching the four-quadrant rotation a classic RRG chart uses
# (Leading / Improving / Weakening / Lagging), badge letters chosen to
# mirror the ST/BD/FD/WK style seen on the reference screener.
QUADRANT_INFO = {
    "high_gear":  {"badge": "ST", "name": "Leading (RS rising, above trend)"},
    "early_spark": {"badge": "BD", "name": "Improving (RS rising, below trend)"},
    "peak_fade":  {"badge": "FD", "name": "Weakening (RS falling, above trend)"},
    "cold_floor": {"badge": "WK", "name": "Lagging (RS falling, below trend)"},
}


def find_index_instrument(index_instruments: pd.DataFrame, target: str) -> Optional[dict]:
    """Same substring-match approach as sector_scanner.match_sector_indices,
    but for a single index (Nifty 50, used as the RS benchmark). Prefers an
    exact tradingsymbol match, then the shortest name containing `target`
    (avoids e.g. 'NIFTY 50' accidentally matching 'NIFTY 500' first)."""
    if index_instruments is None or index_instruments.empty:
        return None
    target_u = target.upper()
    exact = index_instruments[index_instruments["tradingsymbol"].str.upper() == target_u]
    if not exact.empty:
        row = exact.iloc[0]
        return {"instrument_token": row["instrument_token"], "tradingsymbol": row["tradingsymbol"]}

    names_upper = index_instruments["name"].str.upper().fillna("")
    tsyms_upper = index_instruments["tradingsymbol"].str.upper().fillna("")
    mask = names_upper.str.contains(target_u, regex=False) | tsyms_upper.str.contains(target_u, regex=False)
    candidates = index_instruments[mask]
    if candidates.empty:
        return None
    row = candidates.iloc[(candidates["tradingsymbol"].str.len()).argsort()].iloc[0]
    return {"instrument_token": row["instrument_token"], "tradingsymbol": row["tradingsymbol"]}


def compute_rs_quadrant(stock_daily: pd.DataFrame, nifty_daily: pd.DataFrame) -> Optional[dict]:
    """Relative-strength quadrant, RRG-style.

    RS ratio = stock close / Nifty 50 close, aligned by date (inner join —
    only dates both series actually traded).

    LEVEL: is today's RS ratio above or below its own
    config.BREAKOUT_RADAR_RS_TREND_EMA-period EMA? (above = relatively
    strong right now, below = relatively weak).

    MOMENTUM: % change of the RS ratio itself over the last
    config.BREAKOUT_RADAR_RS_MOMENTUM_LOOKBACK trading days (rising = the
    stock is gaining ground on the index, falling = losing ground),
    independent of whether the stock's own price is up or down that day.

    The two combine into the same four-quadrant rotation a Relative
    Rotation Graph uses:
        level UP   + momentum UP   -> "high_gear"   (Leading)
        level DOWN + momentum UP   -> "early_spark" (Improving)
        level UP   + momentum DOWN -> "peak_fade"   (Weakening)
        level DOWN + momentum DOWN -> "cold_floor"  (Lagging)

    Also returns today's simple relative-strength number
    (stock's 1-day % change minus Nifty's 1-day % change) — this is the
    per-scan sortable "vs Nifty" figure; quadrant/rank use it for context,
    not as the quadrant classification itself (that's level+momentum
    above, a multi-day read, not a single day's excess return)."""
    if stock_daily is None or stock_daily.empty or nifty_daily is None or nifty_daily.empty:
        return None

    merged = pd.merge(
        stock_daily[["date", "close"]].rename(columns={"close": "stock_close"}),
        nifty_daily[["date", "close"]].rename(columns={"close": "nifty_close"}),
        on="date", how="inner",
    ).sort_values("date").reset_index(drop=True)

    min_len = config.BREAKOUT_RADAR_RS_TREND_EMA + config.BREAKOUT_RADAR_RS_MOMENTUM_LOOKBACK + 2
    if len(merged) < min_len:
        return None

    merged["rs"] = merged["stock_close"] / merged["nifty_close"]
    merged["rs_ema"] = ind.ema(merged["rs"], config.BREAKOUT_RADAR_RS_TREND_EMA)

    last = merged.iloc[-1]
    lookback_row = merged.iloc[-1 - config.BREAKOUT_RADAR_RS_MOMENTUM_LOOKBACK]

    if pd.isna(last["rs_ema"]) or lookback_row["rs"] == 0:
        return None

    level_up = bool(last["rs"] > last["rs_ema"])
    rs_momentum_pct = float((last["rs"] - lookback_row["rs"]) / lookback_row["rs"] * 100)
    momentum_up = rs_momentum_pct > 0

    if level_up and momentum_up:
        quadrant = "high_gear"
    elif not level_up and momentum_up:
        quadrant = "early_spark"
    elif level_up and not momentum_up:
        quadrant = "peak_fade"
    else:
        quadrant = "cold_floor"

    prev = merged.iloc[-2] if len(merged) >= 2 else None
    rs_vs_nifty_pct = None
    if prev is not None and prev["stock_close"] and prev["nifty_close"]:
        stock_pct = (last["stock_close"] - prev["stock_close"]) / prev["stock_close"] * 100
        nifty_pct = (last["nifty_close"] - prev["nifty_close"]) / prev["nifty_close"] * 100
        rs_vs_nifty_pct = round(float(stock_pct - nifty_pct), 2)

    info = QUADRANT_INFO[quadrant]
    return {
        "quadrant": quadrant,
        "quadrant_badge": info["badge"],
        "quadrant_name": info["name"],
        "rs_level_above_trend": level_up,
        "rs_momentum_pct": round(rs_momentum_pct, 2),
        "rs_vs_nifty_pct": rs_vs_nifty_pct,
    }


def _today_intraday(kite_client, token, symbol, today: date) -> pd.DataFrame:
    """Today's 5-min candles so far. Empty (not an error) outside market
    hours / before the first candle forms — callers must handle that."""
    from_dt = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.now()
    if to_dt <= from_dt:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    return kite_client.get_history(
        token, symbol, config.BREAKOUT_RADAR_INTRADAY_INTERVAL, from_dt, to_dt
    )


def compute_pdh_pdl(stock_daily: pd.DataFrame, intraday: pd.DataFrame, today: date) -> dict:
    """Previous trading day's high/low, plus the first 5-min bar (if any)
    where today's price crossed each — same "PDH 09:20 / PDL 11:20" idea
    as the reference screener."""
    out = {"pdh": None, "pdl": None, "pdh_breach_time": None, "pdl_breach_time": None}
    if stock_daily is None or stock_daily.empty:
        return out

    is_today_row = bool(len(stock_daily) and stock_daily.iloc[-1]["date"].date() == today)
    prev_row = stock_daily.iloc[-2] if is_today_row and len(stock_daily) >= 2 else stock_daily.iloc[-1]
    out["pdh"] = round(float(prev_row["high"]), 2)
    out["pdl"] = round(float(prev_row["low"]), 2)

    if intraday is None or intraday.empty:
        return out

    up_breach = intraday[intraday["high"] >= out["pdh"]]
    down_breach = intraday[intraday["low"] <= out["pdl"]]
    if not up_breach.empty:
        out["pdh_breach_time"] = pd.to_datetime(up_breach.iloc[0]["date"]).strftime("%H:%M")
    if not down_breach.empty:
        out["pdl_breach_time"] = pd.to_datetime(down_breach.iloc[0]["date"]).strftime("%H:%M")
    return out


def compute_multi_orb(intraday: pd.DataFrame, windows=None) -> dict:
    """Opening-range-breakout status for every window in `windows` at once,
    from the same 5-min candle series (each window's opening range is
    simply its first window/5 bars). Direction is whichever side (up/down)
    was crossed first after the opening range, matching a single ORB
    engine's semantics — a symbol can't be both."""
    windows = windows or config.BREAKOUT_RADAR_ORB_WINDOWS
    out = {}
    if intraday is None or intraday.empty:
        for w in windows:
            out[str(w)] = {"status": "no_intraday_data"}
        return out

    bar_minutes = 5  # config.BREAKOUT_RADAR_INTRADAY_INTERVAL is "5minute"
    for w in windows:
        n_bars = max(1, w // bar_minutes)
        if len(intraday) <= n_bars:
            out[str(w)] = {"status": "not_yet"}
            continue
        opening = intraday.iloc[:n_bars]
        after = intraday.iloc[n_bars:]
        orb_high = float(opening["high"].max())
        orb_low = float(opening["low"].min())

        up_hits = after[after["close"] > orb_high]
        down_hits = after[after["close"] < orb_low]
        up_time = up_hits.iloc[0]["date"] if not up_hits.empty else None
        down_time = down_hits.iloc[0]["date"] if not down_hits.empty else None

        if up_time is not None and (down_time is None or up_time <= down_time):
            direction, when = "up", up_time
        elif down_time is not None:
            direction, when = "down", down_time
        else:
            direction, when = None, None

        out[str(w)] = {
            "status": "breakout" if direction else "inside_range",
            "direction": direction,
            "breakout_time": pd.to_datetime(when).strftime("%H:%M") if when is not None else None,
            "orb_high": round(orb_high, 2),
            "orb_low": round(orb_low, 2),
        }
    return out


def compute_money_flow(intraday: pd.DataFrame, avg_daily_volume: Optional[float]) -> dict:
    """RVOL: today's volume-so-far vs the average full-day volume, scaled
    by how much of the session has elapsed (so 10:00am isn't unfairly
    compared to a full day's average). OVS ("volume swing" — no published
    reference formula from the vendor being compared against, this is our
    own proxy): ratio of volume traded on up-close 5-min bars to volume on
    down-close bars today, i.e. how lopsided today's volume has been
    towards buying vs selling pressure."""
    out = {"volume_today": None, "rvol": None, "ovs": None}
    if intraday is None or intraday.empty:
        return out

    volume_today = float(intraday["volume"].sum())
    out["volume_today"] = int(volume_today)

    up_vol = float(intraday.loc[intraday["close"] >= intraday["open"], "volume"].sum())
    down_vol = float(intraday.loc[intraday["close"] < intraday["open"], "volume"].sum())
    if down_vol > 0:
        out["ovs"] = round(up_vol / down_vol, 2)
    elif up_vol > 0:
        out["ovs"] = None  # all one-sided with zero opposing volume — not a meaningful ratio

    now = datetime.now()
    session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    elapsed_min = max(0.0, min((now - session_start).total_seconds() / 60.0, 375.0))
    fraction = elapsed_min / 375.0
    if avg_daily_volume and fraction > 0.02:
        out["rvol"] = round(volume_today / (avg_daily_volume * fraction), 2)
    return out


def scan_breakout_radar(kite_client, universe_df=None, universe_mode: Optional[str] = None) -> dict:
    try:
        mode = universe_mod.normalize_nifty_mode(
            universe_mode or config.BREAKOUT_RADAR_UNIVERSE or "fno"
        )
    except ValueError:
        mode = "fno"

    try:
        scan_df = universe_mod.build_nifty_index_universe(kite_client, mode)
    except Exception as e:
        print(f"  [warn] breakout-radar universe {mode} failed ({e}) — falling back")
        scan_df = universe_df

    label = universe_mod.nifty_mode_label(mode)
    empty_payload = {
        "generated_at": datetime.now().isoformat(),
        "universe_mode": mode,
        "universe_label": label,
        "universe_size": 0,
        "scanned": 0,
        "num_results": 0,
        "results": [],
        "quadrant_counts": {},
        "sentiment_counts": {"bullish": 0, "bearish": 0, "flat": 0},
    }
    if scan_df is None or scan_df.empty:
        return empty_payload

    today = date.today()
    from_date = today - timedelta(days=config.BREAKOUT_RADAR_LOOKBACK_DAYS)

    index_instruments = kite_client.get_nse_index_instruments()
    nifty_info = find_index_instrument(index_instruments, "NIFTY 50")
    if nifty_info is None:
        print("  [warn] breakout-radar: couldn't find NIFTY 50 index instrument — "
              "RS quadrant will be unavailable this scan.")
        nifty_daily = pd.DataFrame(columns=["date", "close"])
    else:
        nifty_daily = kite_client.get_daily_history(
            nifty_info["instrument_token"], nifty_info["tradingsymbol"], from_date, today
        )

    results = []
    scanned = 0
    print(f"Breakout Radar: scanning {label} ({len(scan_df)} symbols)...")

    for _, row in tqdm(scan_df.iterrows(), total=len(scan_df), desc=f"Breakout Radar ({label})"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        scanned += 1
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, today)
            if daily.empty or len(daily) < 30:
                continue

            last = daily.iloc[-1]
            prev = daily.iloc[-2] if len(daily) >= 2 else None
            pct_change_1d = (
                round(float((last["close"] - prev["close"]) / prev["close"] * 100), 2)
                if prev is not None and prev["close"] else None
            )

            rs = compute_rs_quadrant(daily, nifty_daily) or {}

            intraday = _today_intraday(kite_client, token, symbol, today)
            pdh_pdl = compute_pdh_pdl(daily, intraday, today)
            orb = compute_multi_orb(intraday)

            avg_daily_volume = None
            hist_vol = daily.iloc[:-1] if daily.iloc[-1]["date"].date() == today else daily
            if len(hist_vol) >= 5:
                avg_daily_volume = float(
                    hist_vol["volume"].tail(config.BREAKOUT_RADAR_RVOL_LOOKBACK).mean()
                )
            money_flow = compute_money_flow(intraday, avg_daily_volume)

            sentiment = "flat"
            if pct_change_1d is not None:
                sentiment = "bullish" if pct_change_1d > 0 else ("bearish" if pct_change_1d < 0 else "flat")

            results.append({
                "symbol": symbol,
                "close": round(float(last["close"]), 2),
                "pct_change_1d": pct_change_1d,
                "sentiment": sentiment,
                "volume": int(last["volume"]) if pd.notna(last["volume"]) else None,
                **rs,
                **pdh_pdl,
                "orb": orb,
                **money_flow,
            })
        except Exception as e:
            print(f"  [warn] breakout-radar skipped {symbol}: {e}")
            continue

    # Rank by today's simple relative-strength number (best excess return
    # over Nifty = rank 1) — matches the reference screener's per-stock
    # "VS NIFTY / RANK" column. Rows with no RS number (not enough history
    # yet) sort last and get no rank.
    ranked = [r for r in results if r.get("rs_vs_nifty_pct") is not None]
    ranked.sort(key=lambda r: r["rs_vs_nifty_pct"], reverse=True)
    for i, r in enumerate(ranked, start=1):
        r["rs_rank"] = i
    for r in results:
        r.setdefault("rs_rank", None)

    results.sort(key=lambda r: (r["rs_rank"] is None, r["rs_rank"]))

    quadrant_counts: dict = {}
    sentiment_counts = {"bullish": 0, "bearish": 0, "flat": 0}
    for r in results:
        q = r.get("quadrant")
        if q:
            quadrant_counts[q] = quadrant_counts.get(q, 0) + 1
        sentiment_counts[r.get("sentiment", "flat")] = sentiment_counts.get(r.get("sentiment", "flat"), 0) + 1

    print(f"Breakout Radar: {len(results)} of {scanned} scanned ({label}) — "
          f"{sentiment_counts['bullish']} bullish / {sentiment_counts['bearish']} bearish")

    return {
        "generated_at": datetime.now().isoformat(),
        "universe_mode": mode,
        "universe_label": label,
        "universe_size": len(scan_df),
        "scanned": scanned,
        "num_results": len(results),
        "results": results,
        "quadrant_counts": quadrant_counts,
        "sentiment_counts": sentiment_counts,
        "orb_windows": config.BREAKOUT_RADAR_ORB_WINDOWS,
    }
