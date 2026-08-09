"""
Builds the shortlist of symbols to actually stream live via WebSocket.
Streaming the full universe isn't practical — so this takes symbols from:

  1. Momentum breakout EOD scanner (flagship)
  2. Top-performing stocks from today's *top trending sectors*
     (Smart Money pipeline steps 1–2)

Capped at config.INTRADAY_SHORTLIST_MAX. `sources` lists which scanner(s)
flagged each symbol (surfaced in the intraday UI/alerts).
"""
import config
import live_scan
import smart_money_pipeline


def build_shortlist(kite_client, universe_df) -> list:
    """Returns a list of {symbol, instrument_token, sources} dicts."""
    print("Building intraday shortlist (momentum breakout + top-sector leaders)...")

    seen = {}  # symbol -> {"token": ..., "sources": set()}
    token_by_symbol = dict(zip(universe_df["tradingsymbol"], universe_df["instrument_token"]))

    def add(symbol, source, token=None):
        tok = token if token is not None else token_by_symbol.get(symbol)
        if tok is None:
            return
        entry = seen.setdefault(symbol, {"token": tok, "sources": set()})
        entry["sources"].add(source)

    main_hits = live_scan.run_scan(kite_client, universe_df)
    for h in main_hits:
        add(h["symbol"], "momentum_breakout")

    leaders = smart_money_pipeline.build_smart_money_shortlist(kite_client, universe_df)
    for item in leaders:
        add(item["symbol"], f"trending:{item['sector']}", token=item["instrument_token"])

    shortlist = [
        {"symbol": sym, "instrument_token": info["token"], "sources": sorted(info["sources"])}
        for sym, info in seen.items()
    ]
    shortlist = shortlist[:config.INTRADAY_SHORTLIST_MAX]

    confluence_count = sum(1 for s in shortlist if len(s["sources"]) > 1)
    print(f"Shortlist: {len(shortlist)} symbols (momentum breakout: {len(main_hits)}, "
          f"top-sector leaders: {len(leaders)}, {confluence_count} symbol(s) "
          f"flagged by BOTH sources, deduped & capped at {config.INTRADAY_SHORTLIST_MAX})")
    return shortlist
