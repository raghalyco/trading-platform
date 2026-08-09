"""
"Trending Stocks by Sector": for each sector, ranks its constituent stocks
by *today's* (1-day) momentum — the same present-day lens as Sector Analysis —
and returns the top N. Only considers constituents that are also in your
currently-built universe (so instrument tokens are already known — no extra
instrument lookups needed); if UNIVERSE_MODE is narrower than "all"
(e.g. nifty500), a handful of smaller-cap sector constituents outside that
universe just won't appear here. Given the universe is typically Nifty 500
already, coverage should be high in practice.
"""
from datetime import date, timedelta

import config
import sector_constituents


def build_trending_by_sector(kite_client, universe_df) -> dict:
    """Returns {sector_name: [ {symbol, close, pct_change_1d, pct_change_5d}, ... ]}
    (top config.TRENDING_STOCKS_PER_SECTOR per sector, sorted by 1D / today)."""
    token_by_symbol = dict(zip(universe_df["tradingsymbol"], universe_df["instrument_token"]))

    from_date = date.today() - timedelta(days=config.TRENDING_LOOKBACK_DAYS)
    to_date = date.today()

    results = {}
    for sector_name in config.SECTOR_INDEX_NAMES:
        constituents = sector_constituents.fetch_constituents(sector_name)
        if not constituents:
            continue

        in_universe = [s for s in constituents if s in token_by_symbol]
        if not in_universe:
            continue

        stock_metrics = []
        for symbol in in_universe:
            token = token_by_symbol[symbol]
            daily = kite_client.get_daily_history(token, symbol, from_date, to_date)
            if daily.empty or len(daily) < 2:
                continue

            last = daily.iloc[-1]
            prev_1d = daily.iloc[-2]
            prev_5d = daily.iloc[-6] if len(daily) >= 6 else None

            def pct(prev_row):
                if prev_row is None or prev_row["close"] == 0:
                    return None
                return round((last["close"] - prev_row["close"]) / prev_row["close"] * 100, 2)

            stock_metrics.append({
                "symbol": symbol,
                "close": round(float(last["close"]), 2),
                "pct_change_1d": pct(prev_1d),
                "pct_change_5d": pct(prev_5d),
            })

        stock_metrics.sort(
            key=lambda m: (m["pct_change_1d"] if m["pct_change_1d"] is not None else -999),
            reverse=True,
        )
        top = stock_metrics[:config.TRENDING_STOCKS_PER_SECTOR]
        if top:
            results[sector_name] = top

    return results
