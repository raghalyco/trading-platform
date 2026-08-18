"""
Index Movers: for a tracked index, ranks constituents by their approximate
contribution to today's index move — the same "why is NIFTY down today"
job as TradeBrahma's Index Mover donut.

IMPORTANT CAVEAT: NSE's real index methodology weights each constituent by
FREE-FLOAT market cap divided by the index divisor — that exact number
isn't published anywhere Kite's API (or any other free feed) exposes. This
uses market-cap-weighted % contribution as a practical proxy (a stock's
weight here = its market cap / sum of the tracked constituents' market
caps), reusing the same market-cap sourcing universe.py already has for the
"all" universe mode (data/market_cap.csv, falling back to yfinance). This
correlates well with real weights for large, liquid names but will not
exactly match NSE's published index point contribution — treat it as
"roughly who's driving the move," not an authoritative number.
"""
from datetime import date, datetime, timedelta

import config
import universe as universe_mod

# Index label -> universe.py nifty-mode key for its constituent list.
INDEX_MOVER_UNIVERSES = {
    "NIFTY 50": "nifty50",
    "NIFTY 200": "nifty200",
}


def compute_index_movers(kite_client, index_label: str = "NIFTY 50", top_n: int = 12) -> dict:
    mode = INDEX_MOVER_UNIVERSES.get(index_label, "nifty50")
    constituents = universe_mod.build_nifty_index_universe(kite_client, mode)
    if constituents.empty:
        return {
            "index": index_label, "generated_at": datetime.now().isoformat(),
            "top_gainers": [], "top_losers": [], "net_contribution_pct": None,
            "note": "No constituents matched to tradeable Kite instruments.",
        }

    mcap_map = universe_mod.load_market_cap_csv()
    symbols = constituents["tradingsymbol"].tolist()
    missing = [s for s in symbols if s not in mcap_map]
    if missing and config.USE_YFINANCE_FALLBACK:
        mcap_map.update(universe_mod.fetch_market_cap_yfinance(missing))

    from_date = date.today() - timedelta(days=10)
    to_date = date.today()

    rows = []
    total_mcap = 0.0
    for _, r in constituents.iterrows():
        symbol = r["tradingsymbol"]
        mcap = mcap_map.get(symbol)
        if not mcap:
            continue
        daily = kite_client.get_daily_history(r["instrument_token"], symbol, from_date, to_date)
        if daily.empty or len(daily) < 2:
            continue
        last = daily.iloc[-1]
        prev = daily.iloc[-2]
        if prev["close"] == 0:
            continue
        pct_change = (last["close"] - prev["close"]) / prev["close"] * 100
        rows.append({
            "symbol": symbol,
            "close": round(float(last["close"]), 2),
            "pct_change_1d": round(float(pct_change), 2),
            "market_cap_cr": round(float(mcap), 1),
        })
        total_mcap += mcap

    if not rows or total_mcap <= 0:
        return {
            "index": index_label, "generated_at": datetime.now().isoformat(),
            "top_gainers": [], "top_losers": [], "net_contribution_pct": None,
            "note": "Insufficient market cap data to weight constituents — "
                    "add data/market_cap.csv or enable the yfinance fallback.",
        }

    for row in rows:
        row["weight_pct"] = round(row["market_cap_cr"] / total_mcap * 100, 3)
        row["contribution_pct"] = round(row["pct_change_1d"] * row["weight_pct"] / 100, 4)

    rows.sort(key=lambda r: r["contribution_pct"], reverse=True)
    top_gainers = [r for r in rows[:top_n] if r["contribution_pct"] > 0]
    top_losers = sorted(
        [r for r in rows if r["contribution_pct"] < 0],
        key=lambda r: r["contribution_pct"],
    )[:top_n]
    net_contribution = round(sum(r["contribution_pct"] for r in rows), 3)
    max_abs_contribution = max((abs(r["contribution_pct"]) for r in rows), default=0.0001) or 0.0001

    return {
        "index": index_label,
        "generated_at": datetime.now().isoformat(),
        "constituents_used": len(rows),
        "constituents_total": len(constituents),
        "net_contribution_pct": net_contribution,
        "max_abs_contribution_pct": round(max_abs_contribution, 4),
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "methodology_note": (
            "Market-cap-weighted approximation, not NSE's exact free-float index "
            "weights (not available via any free API) — directionally accurate "
            "for large, liquid constituents but won't exactly match NSE's "
            "published index point contribution."
        ),
    }
