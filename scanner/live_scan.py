"""
Runs a scanner against the most recent data for every symbol in the
universe and returns only the ones passing its conditions today —
your "Chartink-style" daily scan result. Supports both scanners:
  - run_scan(): the main 13-condition breakout scanner
  - run_ema10_scan(): the simpler "just above EMA10, not extended" scanner

Hits are annotated with classic chart patterns + local chart URLs.
"""
from datetime import date, timedelta

import pandas as pd
from tqdm import tqdm

import config
import ema10_scanner
import nday_scanner
import scanner
import sector_scanner
import support_bounce as sb
import trending_scanner


def run_scan(kite_client, universe_df: pd.DataFrame) -> list:
    """Returns a list of dicts, one per symbol currently passing the main
    13-condition scanner, with the fields the dashboard table needs."""
    today = date.today()
    from_date = today - timedelta(days=365 * config.WARMUP_YEARS)

    hits = []
    for _, row in tqdm(universe_df.iterrows(), total=len(universe_df), desc="Scanning"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, today)
            if daily.empty or len(daily) < config.EMA_SLOW:
                continue

            sig_df = scanner.compute_signals(daily)
            last = sig_df.iloc[-1]
            if not bool(last["signal"]):
                continue

            prev_close = sig_df.iloc[-2]["close"] if len(sig_df) >= 2 else None
            pct_change = ((last["close"] - prev_close) / prev_close * 100) if prev_close else None
            distance_from_breakout = (
                (last["close"] - last["breakout_level"]) / last["breakout_level"] * 100
                if pd.notna(last["breakout_level"]) else None
            )

            hits.append({
                "symbol": symbol,
                "signal_date": str(last["date"].date()),
                "close": round(float(last["close"]), 2),
                "pct_change": round(pct_change, 2) if pct_change is not None else None,
                "rsi14": round(float(last["rsi14"]), 1),
                "adx14": round(float(last["adx14"]), 1),
                "weekly_rsi14": round(float(last["w_rsi14"]), 1),
                "distance_from_breakout_pct": (
                    round(distance_from_breakout, 2) if distance_from_breakout is not None else None
                ),
                "volume": int(last["volume"]),
                "vol_vs_avg": round(float(last["volume"] / last["vol_sma20"]), 2) if last["vol_sma20"] else None,
            })
            sb.annotate_result_with_pattern(symbol, daily, hits[-1])
        except Exception as e:
            print(f"  [warn] scan skipped {symbol}: {e}")
            continue

    return hits


def run_ema10_scan(kite_client, universe_df: pd.DataFrame) -> list:
    """Returns a list of dicts for symbols currently trading above their
    10 EMA but not extended more than EMA10_MAX_DISTANCE_PCT beyond it."""
    today = date.today()
    from_date = today - timedelta(days=max(200, config.SUPPORT_BOUNCE_LOOKBACK_DAYS))

    hits = []
    for _, row in tqdm(universe_df.iterrows(), total=len(universe_df), desc="Scanning (EMA10)"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, today)
            if daily.empty or len(daily) < config.EMA10_PERIOD * 3:
                continue

            sig_df = ema10_scanner.compute_signals(daily)
            last = sig_df.iloc[-1]
            if not bool(last["signal"]):
                continue

            prev_close = sig_df.iloc[-2]["close"] if len(sig_df) >= 2 else None
            pct_change = ((last["close"] - prev_close) / prev_close * 100) if prev_close else None

            hits.append({
                "symbol": symbol,
                "signal_date": str(last["date"].date()),
                "close": round(float(last["close"]), 2),
                "pct_change": round(pct_change, 2) if pct_change is not None else None,
                "ema10": round(float(last["ema10"]), 2),
                "distance_from_ema10_pct": round(float(last["distance_from_ema10_pct"]), 2),
                "volume": int(last["volume"]),
            })
            sb.annotate_result_with_pattern(symbol, daily, hits[-1])
        except Exception as e:
            print(f"  [warn] ema10 scan skipped {symbol}: {e}")
            continue

    return hits


def run_nday_scan(kite_client, universe_df: pd.DataFrame, lookback: int) -> list:
    """Returns a list of dicts for symbols currently near their N-day high
    (direction='near_high') or N-day low (direction='near_low'), matching
    TradeBrahma's '10 Day BO' / '50 Day BO' panels."""
    today = date.today()
    from_date = today - timedelta(days=max(200, lookback * 5, config.SUPPORT_BOUNCE_LOOKBACK_DAYS))

    hits = []
    for _, row in tqdm(universe_df.iterrows(), total=len(universe_df),
                        desc=f"Scanning (N-Day BO, {lookback}d)"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, today)
            if daily.empty or len(daily) < lookback:
                continue

            sig_df = nday_scanner.compute_signals(daily, lookback)
            last = sig_df.iloc[-1]

            prev_close = sig_df.iloc[-2]["close"] if len(sig_df) >= 2 else None
            pct_change = ((last["close"] - prev_close) / prev_close * 100) if prev_close else None

            base = {
                "symbol": symbol,
                "signal_date": str(last["date"].date()),
                "close": round(float(last["close"]), 2),
                "pct_change": round(pct_change, 2) if pct_change is not None else None,
                "volume": int(last["volume"]),
            }

            if bool(last["near_high"]):
                hits.append({
                    **base,
                    "direction": "near_high",
                    "distance_pct": round(float(last["distance_from_high_pct"]), 2),
                    "reference_price": round(float(last["nday_high"]), 2),
                })
                sb.annotate_result_with_pattern(symbol, daily, hits[-1])
            elif bool(last["near_low"]):
                hits.append({
                    **base,
                    "direction": "near_low",
                    "distance_pct": round(float(last["distance_from_low_pct"]), 2),
                    "reference_price": round(float(last["nday_low"]), 2),
                })
                sb.annotate_result_with_pattern(symbol, daily, hits[-1])
        except Exception as e:
            print(f"  [warn] nday scan skipped {symbol}: {e}")
            continue

    return hits


def run_sector_scan(kite_client) -> list:
    """Returns a list of dicts, one per matched sector index, ranked by
    today's (1-day) momentum, with breakout flags."""
    index_instruments = kite_client.get_nse_index_instruments()
    matched = sector_scanner.match_sector_indices(index_instruments)

    from_date = date.today() - timedelta(days=400)  # plenty for EMA20 + 1m/5d lookback
    to_date = date.today()

    results = []
    for sector_name, info in matched.items():
        daily = kite_client.get_daily_history(info["instrument_token"], info["tradingsymbol"],
                                               from_date, to_date)
        if daily.empty:
            continue
        metrics = sector_scanner.compute_sector_metrics(daily)
        if metrics is None:
            continue
        results.append({"sector": sector_name, "tradingsymbol": info["tradingsymbol"], **metrics})

    results.sort(key=lambda r: (r["pct_change_1d"] if r["pct_change_1d"] is not None else -999),
                 reverse=True)
    return results


def run_trending_scan(kite_client, universe_df) -> dict:
    """Top stocks per sector by today's (1D) move, with sector cards ordered
    the same way as Sector Analysis (1D sector ranking)."""
    by_sector = trending_scanner.build_trending_by_sector(kite_client, universe_df)
    sector_rank = run_sector_scan(kite_client)

    ordered = {}
    for s in sector_rank:
        name = s["sector"]
        if name in by_sector:
            ordered[name] = by_sector.pop(name)
    # Any sectors that had constituents but no index match still appear last
    for name, stocks in by_sector.items():
        ordered[name] = stocks
    return ordered


def enrich_with_live_quotes(kite_client, hits: list) -> list:
    """Overlays live LTP/%change from Kite's quote endpoint onto scan
    results (only called for the small list of symbols that already
    passed — not the whole universe). Shared by both scanners."""
    if not hits:
        return hits
    symbols = [h["symbol"] for h in hits]
    quotes = kite_client.get_quotes(symbols)
    for h in hits:
        q = quotes.get(h["symbol"])
        if q and q["last_price"] is not None:
            h["ltp"] = round(q["last_price"], 2)
            h["pct_change"] = round(q["pct_change"], 2) if q["pct_change"] is not None else h["pct_change"]
        else:
            h["ltp"] = h["close"]  # market closed / quote unavailable, fall back to last daily close
    return hits
