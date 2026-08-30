"""
Custom Sector/Theme Baskets: synthetic "index" tracking for hand-curated
micro-theme stock groups that have no official tradeable NSE index (see
config.CUSTOM_SECTOR_BASKETS, e.g. "Digital / AI IT Services", "Plastic
Pipes & Packaging"). Modeled after a reference Telegram bot's "Sector
Breakout Found" alerts.

Since there's no real instrument to price a made-up theme, this computes
an equal-weighted average of the member stocks' daily closes as a
synthetic basket value, then applies the EXACT same EMA + N-day-high
breakout logic sector_scanner.py already uses for real NSE sector
indices - just applied to this synthetic series instead.

Caveat: equal-weighted average-of-closes is a simple, transparent
approximation, not a verified match to any particular reference source's
own basket-index methodology (never publicly documented) - treat the
basket VALUE as directionally useful for spotting a breakout, not an
authoritative index number.
"""
from datetime import date, timedelta

import pandas as pd

import config
import indicators as ind
import sector_constituents

_TOKEN_CACHE: dict = {}


def _resolve_tokens(kite_client, symbols: list) -> dict:
    missing = [s for s in symbols if s not in _TOKEN_CACHE]
    if missing:
        instruments = kite_client.get_nse_equity_instruments()
        lookup = dict(zip(instruments["tradingsymbol"], instruments["instrument_token"]))
        for s in missing:
            if s in lookup:
                _TOKEN_CACHE[s] = int(lookup[s])
            else:
                print(f"  [warn] custom_baskets: symbol '{s}' not found in Kite NSE instruments")
    return {s: _TOKEN_CACHE[s] for s in symbols if s in _TOKEN_CACHE}


def compute_basket_value_series(kite_client, symbols: list, lookback_days: int) -> pd.DataFrame:
    """Equal-weighted average of member stocks' daily closes, aligned by
    date. Returns DataFrame[date, value, norm_value] sorted ascending.

    'value' is the raw rupee average of closes - what's displayed as the
    basket's VALUE, kept exactly as before.

    'norm_value' rebases every member to its own first available close =
    100 before averaging, so each member contributes an equal-weighted
    PERCENTAGE move rather than an equal-weighted RUPEE amount. This is
    used only for breakout detection (see compute_basket_metrics): a
    basket like "Financial Market Infrastructure" (KFINTECH, IEX, CAMS,
    MCX, BSE, CDSL) mixes members trading at very different absolute price
    levels, so a straight average of raw closes is really price-level-
    weighted - a single high-priced constituent moving on its own can push
    the raw 'value' to a fresh N-day high (tagging a false BREAKOUT) even
    when no member individually made a fresh high. norm_value fixes that."""
    tokens = _resolve_tokens(kite_client, symbols)
    if not tokens:
        return pd.DataFrame(columns=["date", "value", "norm_value"])

    today = date.today()
    from_date = today - timedelta(days=lookback_days)
    frames = []
    for symbol, token in tokens.items():
        daily = kite_client.get_daily_history(token, symbol, from_date, today)
        if daily is None or daily.empty:
            continue
        s = daily[["date", "close"]].copy()
        s["date"] = pd.to_datetime(s["date"]).dt.date
        frames.append(s.set_index("date")["close"])

    if not frames:
        return pd.DataFrame(columns=["date", "value", "norm_value"])

    combined = pd.concat(frames, axis=1)
    value = combined.mean(axis=1, skipna=True)

    def _rebase(col: pd.Series) -> pd.Series:
        base = col.dropna()
        if base.empty:
            return col
        return col / base.iloc[0] * 100.0

    norm_value = combined.apply(_rebase, axis=0).mean(axis=1, skipna=True)

    out = pd.DataFrame({"value": value, "norm_value": norm_value}).reset_index()
    out.columns = ["date", "value", "norm_value"]
    out = out.dropna(subset=["value"]).sort_values("date").reset_index(drop=True)
    return out


def compute_basket_metrics(value_series: pd.DataFrame) -> dict:
    """Same N-day-high breakout logic as sector_scanner.compute_sector_metrics
    (close > rolling max of the prior CUSTOM_BASKET_BREAKOUT_LOOKBACK days),
    applied to the synthetic basket series instead of a real tradeable index -
    except the breakout check runs on 'norm_value' (each member rebased to
    100 at its own first available close, then averaged), not the raw rupee
    'value'. See compute_basket_value_series' docstring: comparing the raw
    average against its own rolling high let one high-priced constituent's
    move alone tag the whole basket as a BREAKOUT with no member actually
    at a fresh N-day high - this is the fix for that false positive."""
    df = value_series.copy()
    if len(df) < config.CUSTOM_BASKET_BREAKOUT_LOOKBACK + 2:
        return None

    df["ema"] = ind.ema(df["value"], config.CUSTOM_BASKET_EMA_PERIOD)
    df["norm_nday_high"] = df["norm_value"].rolling(config.CUSTOM_BASKET_BREAKOUT_LOOKBACK).max().shift(1)

    last = df.iloc[-1]
    prev_1d = df.iloc[-2] if len(df) >= 2 else None

    def pct_change(prev_row):
        if prev_row is None or prev_row["value"] == 0:
            return None
        return round((last["value"] - prev_row["value"]) / prev_row["value"] * 100, 2)

    breakout = bool(last["norm_value"] > last["norm_nday_high"]) if pd.notna(last["norm_nday_high"]) else False

    return {
        "value": round(float(last["value"]), 2),
        "pct_change_1d": pct_change(prev_1d),
        "breakout_20d": breakout,
        "date": str(last["date"]),
    }


def _resolve_symbols(spec: dict) -> list:
    """A basket can either list its own static "symbols", or point at an
    official NSE sectoral index via "source_sector" (e.g. "NIFTY MEDIA") to
    pull the real, current constituent list from sector_constituents.py -
    more accurate and self-updating than hand-copying a symbol list for a
    theme that already has an official index."""
    if spec.get("source_sector"):
        try:
            return sector_constituents.fetch_constituents(spec["source_sector"])
        except Exception as e:
            print(f"  [warn] custom_baskets: source_sector '{spec['source_sector']}' failed: {e}")
            return []
    return spec.get("symbols") or []


def scan_custom_baskets(kite_client) -> list:
    """Returns one row per config.CUSTOM_SECTOR_BASKETS entry: synthetic
    basket value, % change, breakout flag, type label, and member symbols."""
    results = []
    for name, spec in config.CUSTOM_SECTOR_BASKETS.items():
        symbols = _resolve_symbols(spec)
        try:
            series = compute_basket_value_series(
                kite_client, symbols, config.CUSTOM_BASKET_BREAKOUT_LOOKBACK + 40
            )
            metrics = compute_basket_metrics(series)
        except Exception as e:
            print(f"  [warn] custom basket '{name}' failed: {e}")
            continue
        if metrics is None:
            continue
        results.append({
            "name": name,
            "type": spec.get("type", "Custom"),
            "symbols": symbols,
            **metrics,
        })
    results.sort(key=lambda r: r.get("pct_change_1d") if r.get("pct_change_1d") is not None else -999, reverse=True)
    return results
