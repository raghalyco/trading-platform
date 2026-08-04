"""
Requirement flow orchestrator:

  1. Rank sector indices → top trending sectors
  2. Take top-performing constituents from those sectors
  3. Run Smart Money strategy on each shortlisted stock
  4. Emit signals when all entry gates pass
  5. (Optional) Telegram notify

Used by smart_money_monitor.py and /api/smart_money/scan.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from tqdm import tqdm

import config
import live_scan
import smart_money_strategy as sms
import telegram_alerts

# Suppress repeat Telegram/API noise for the same symbol+side+bar timestamp
# within a process lifetime (important for --loop).
_seen_signal_keys: set[str] = set()


def _signal_key(sig: dict) -> str:
    return f"{sig.get('symbol')}|{sig.get('signal')}|{sig.get('timestamp')}"


def select_trending_sectors(kite_client) -> list:
    """Step 1 — top sectors by today's (1D) momentum (optionally above EMA20)."""
    sectors = live_scan.run_sector_scan(kite_client)
    if config.SMART_MONEY_REQUIRE_SECTOR_ABOVE_EMA:
        sectors = [s for s in sectors if s.get("above_ema20")]
    return sectors[:config.SMART_MONEY_TOP_SECTORS]


def select_sector_leaders(kite_client, universe_df, trending_sectors: list) -> list:
    """Step 2 — top stocks from each trending sector (today / 1D).
    Returns [{symbol, instrument_token, sector, pct_change_1d, sources}, ...]."""
    if not trending_sectors:
        return []

    sector_names = {s["sector"] for s in trending_sectors}
    token_by_symbol = dict(zip(universe_df["tradingsymbol"], universe_df["instrument_token"]))
    by_sector = live_scan.run_trending_scan(kite_client, universe_df)

    leaders = []
    seen = set()
    for sector_name in sector_names:
        stocks = by_sector.get(sector_name, [])[:config.SMART_MONEY_STOCKS_PER_SECTOR]
        for stock in stocks:
            symbol = stock["symbol"]
            if symbol in seen:
                continue
            token = token_by_symbol.get(symbol)
            if token is None:
                continue
            seen.add(symbol)
            leaders.append({
                "symbol": symbol,
                "instrument_token": token,
                "sector": sector_name,
                "pct_change_1d": stock.get("pct_change_1d"),
                "sources": [f"trending:{sector_name}"],
            })
    return leaders


def evaluate_leaders(kite_client, leaders: list) -> list:
    """Steps 3–4 — apply Smart Money strategy; return signal dicts."""
    if not leaders:
        return []

    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=config.SMART_MONEY_HISTORY_DAYS)
    daily_from = (to_dt - timedelta(days=120)).date()
    daily_to = to_dt.date()

    signals = []
    for item in leaders:
        symbol = item["symbol"]
        token = item["instrument_token"]
        sector = item.get("sector", "")

        signal_df = kite_client.get_history(
            token, symbol, config.SMART_MONEY_SIGNAL_INTERVAL, from_dt, to_dt
        )
        htf_df = None
        if config.SMART_MONEY_HTF_INTERVAL != config.SMART_MONEY_SIGNAL_INTERVAL:
            htf_df = kite_client.get_history(
                token, symbol, config.SMART_MONEY_HTF_INTERVAL, from_dt, to_dt
            )
        daily_df = kite_client.get_daily_history(token, symbol, daily_from, daily_to)

        sig = sms.evaluate_symbol(
            symbol=symbol,
            signal_df=signal_df,
            sector=sector,
            htf_df=htf_df,
            daily_df=daily_df,
        )
        if sig is None:
            continue
        signals.append(sig.to_dict())

    return signals


def run_pipeline(kite_client, universe_df, send_telegram: Optional[bool] = None) -> dict:
    """Full flow. Returns {sectors, leaders, signals, ...}."""
    if send_telegram is None:
        send_telegram = config.SMART_MONEY_SEND_TELEGRAM

    print("Smart Money pipeline: selecting top trending sectors...")
    sectors = select_trending_sectors(kite_client)
    print(f"  Top sectors ({len(sectors)}): "
          + ", ".join(f"{s['sector']} ({s.get('pct_change_1d')}%)" for s in sectors))

    print("Smart Money pipeline: selecting sector leaders...")
    leaders = select_sector_leaders(kite_client, universe_df, sectors)
    print(f"  Leaders: {len(leaders)} symbols")

    print("Smart Money pipeline: evaluating strategy gates...")
    signals = evaluate_leaders(kite_client, leaders)
    fresh = []
    for sig in signals:
        key = _signal_key(sig)
        if key in _seen_signal_keys:
            continue
        _seen_signal_keys.add(key)
        fresh.append(sig)
    print(f"  Signals: {len(signals)} raw, {len(fresh)} new")

    if send_telegram:
        for sig in fresh:
            telegram_alerts.send_telegram_message(
                telegram_alerts.format_smart_money_alert(sig)
            )

    return {
        "generated_at": datetime.now().isoformat(),
        "sectors": sectors,
        "num_leaders": len(leaders),
        "leaders": [
            {"symbol": x["symbol"], "sector": x["sector"], "pct_change_1d": x.get("pct_change_1d")}
            for x in leaders
        ],
        "num_signals": len(fresh),
        "signals": fresh,
    }


def build_smart_money_shortlist(kite_client, universe_df) -> list:
    """Shortlist helper for the intraday WebSocket watchlist — same sector
    → leader selection as the signal pipeline, without running the strategy."""
    sectors = select_trending_sectors(kite_client)
    return select_sector_leaders(kite_client, universe_df, sectors)


def _weekly_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """Higher-TF frame for daily Stock-for-day scans (weekly OHLCV)."""
    if daily is None or daily.empty:
        return daily
    return sms._resample_ohlcv(daily, "W-FRI")


def scan_stock_for_day(kite_client, universe_df=None, send_telegram: Optional[bool] = None) -> dict:
    """Scan Nifty 50/100 (config.STOCK_FOR_DAY_UNIVERSE) on daily candles with
    Smart Money structure. Returns only BUY-eligible stocks."""
    import universe as universe_mod

    if send_telegram is None:
        send_telegram = config.STOCK_FOR_DAY_SEND_TELEGRAM

    mode = (config.STOCK_FOR_DAY_UNIVERSE or "nifty100").strip().lower()
    try:
        scan_df = universe_mod.build_nifty_index_universe(kite_client, mode)
    except Exception as e:
        print(f"  [warn] could not build {mode} list ({e}) — falling back to passed universe")
        scan_df = universe_df

    if scan_df is None or scan_df.empty:
        return {
            "generated_at": datetime.now().isoformat(),
            "universe_mode": mode,
            "universe_size": 0,
            "scanned": 0,
            "num_buys": 0,
            "buys": [],
        }

    today = datetime.now().date()
    from_date = today - timedelta(days=config.STOCK_FOR_DAY_LOOKBACK_DAYS)

    buys = []
    scanned = 0
    label = {"nifty50": "Nifty 50", "nifty100": "Nifty 100", "nifty500": "Nifty 500"}.get(mode, mode)
    print(f"Stock for day: scanning {label} ({len(scan_df)} symbols) with Smart Money structure...")

    for _, row in tqdm(scan_df.iterrows(), total=len(scan_df), desc=f"Stock for day ({label})"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        scanned += 1
        try:
            daily = kite_client.get_daily_history(token, symbol, from_date, today)
            if daily.empty or len(daily) < 60:
                continue
            weekly = _weekly_from_daily(daily)
            sig = sms.evaluate_symbol(
                symbol=symbol,
                signal_df=daily,
                sector="",
                htf_df=weekly,
                ltf_df=daily,
                daily_df=daily,
            )
            if sig is None or sig.signal != "BUY":
                continue
            buys.append(sig.to_dict())
        except Exception as e:
            print(f"  [warn] stock-for-day skipped {symbol}: {e}")
            continue

    buys.sort(key=lambda s: (s.get("confidence") or 0, s.get("risk_reward") or 0), reverse=True)
    print(f"Stock for day: {len(buys)} BUY-eligible of {scanned} scanned ({label})")

    if send_telegram:
        for sig in buys:
            telegram_alerts.send_telegram_message(
                telegram_alerts.format_smart_money_alert(sig)
            )

    return {
        "generated_at": datetime.now().isoformat(),
        "universe_mode": mode,
        "universe_label": label,
        "universe_size": len(scan_df),
        "scanned": scanned,
        "num_buys": len(buys),
        "buys": buys,
    }
