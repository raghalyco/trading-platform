"""
Cross-strategy + industry-level performance aggregation.

Deliberately built on top of data the app has ALREADY computed - DarvaX/
Swing Trade breakout scan results (which already carry both the breakout
price and today's current price) and trades tracked via trade_tracker.py.
No new API calls, no new data source - this only aggregates what's
already in memory/on disk, answering:

  - "Which strategy is actually working?" (trade_tracker, grouped by
    which screener a trade was tracked from)
  - "Which industries are producing stronger breakout candidates?"
    (DarvaX/Swing Trade results, grouped by industry via the Nifty
    constituents CSVs already cached for the universe scanners)
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from typing import Optional

import trade_tracker

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_INDUSTRY_MAP_CACHE: Optional[dict[str, str]] = None


def _load_symbol_industry_map() -> dict[str, str]:
    """Symbol -> Industry, built from the broadest Nifty constituents CSV
    already cached locally for the universe scanners. Broadest-first so a
    symbol only in Nifty 500 (not 200/100) still resolves."""
    global _INDUSTRY_MAP_CACHE
    if _INDUSTRY_MAP_CACHE is not None:
        return _INDUSTRY_MAP_CACHE

    mapping: dict[str, str] = {}
    for fname in ("nifty500_constituents.csv", "nifty200_constituents.csv", "nifty100_constituents.csv"):
        path = os.path.join(_CACHE_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = (row.get("Symbol") or "").strip().upper()
                industry = (row.get("Industry") or "").strip()
                if sym and industry and sym not in mapping:
                    mapping[sym] = industry
    _INDUSTRY_MAP_CACHE = mapping
    return mapping


def industry_breakout_performance(results: list[dict], entry_field: str, current_field: str) -> list[dict]:
    """Groups already-scanned breakout results (DarvaX or Swing Trade) by
    industry, computing each stock's forward return (breakout price ->
    today's price) and rolling that up to a per-industry win rate / avg
    return - a "which industries are producing stronger breakout
    candidates" view."""
    industry_map = _load_symbol_industry_map()
    by_industry: dict[str, list[dict]] = defaultdict(list)

    for r in results:
        symbol = r.get("symbol")
        entry = r.get(entry_field)
        current = r.get(current_field)
        if not symbol or entry is None or current is None or entry <= 0:
            continue
        industry = industry_map.get(str(symbol).upper(), "Unknown")
        forward_return_pct = round((float(current) - float(entry)) / float(entry) * 100.0, 2)
        by_industry[industry].append({
            "symbol": symbol,
            "forward_return_pct": forward_return_pct,
            "status": r.get("status"),
        })

    out = []
    for industry, rows in by_industry.items():
        returns = [x["forward_return_pct"] for x in rows]
        wins = sum(1 for x in returns if x > 0)
        out.append({
            "industry": industry,
            "count": len(rows),
            "win_rate_pct": round(wins / len(rows) * 100.0, 1),
            "avg_return_pct": round(sum(returns) / len(returns), 2),
            "best_return_pct": round(max(returns), 2),
            "worst_return_pct": round(min(returns), 2),
            "symbols": sorted({x["symbol"] for x in rows}),
        })
    out.sort(key=lambda x: x["avg_return_pct"], reverse=True)
    return out


def strategy_performance() -> list[dict]:
    """Aggregates trade_tracker's tracked trades by `source` (which
    screener they were tracked from) - "which strategy is actually
    working" across everything you've tracked, not just one scanner."""
    trades = trade_tracker.list_tracked()
    by_source: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_source[t.get("source") or "unknown"].append(t)

    out = []
    for source, rows in by_source.items():
        closed = [t for t in rows if t.get("status") in ("TARGET_HIT", "STOPPED_OUT", "CLOSED")]
        wins = [t for t in closed if (t.get("pnl_pct") or 0) > 0]
        pnls = [t.get("pnl_pct") for t in rows if t.get("pnl_pct") is not None]
        out.append({
            "source": source,
            "total_tracked": len(rows),
            "open": sum(1 for t in rows if t.get("status") == "OPEN"),
            "closed": len(closed),
            "win_rate_pct": round(len(wins) / len(closed) * 100.0, 1) if closed else None,
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 2) if pnls else None,
        })
    out.sort(key=lambda x: (x["avg_pnl_pct"] if x["avg_pnl_pct"] is not None else -999), reverse=True)
    return out
