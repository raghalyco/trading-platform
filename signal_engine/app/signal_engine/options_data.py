"""
Put-Call Ratio (PCR) and Open Interest (OI) scoring component.

IMPORTANT - honesty note: PCR/OI require live OPTIONS CHAIN data (OI per
strike, per expiry), which is a fundamentally different Kite API call than
anything else in this engine. The index feed (get_ohlcv_1m, get_spot_price)
only touches the underlying index instrument token - it has no visibility
into option contracts at all.

To get real PCR, you need to:
1. Find ALL option instrument tokens for the current expiry from
   kite.instruments("NFO") (hundreds of strikes x CE/PE).
2. Call kite.quote() with those tokens to get OI per contract
   (kite.quote() accepts up to ~500 instruments per call).
3. Sum PE OI and CE OI (usually across all strikes, or an ATM+/-N band),
   then PCR = total_PE_OI / total_CE_OI.

That's a non-trivial amount of new integration work - fetching, caching,
and refreshing hundreds of option quotes every cycle - not something to
fake with synthetic numbers, since a wrong PCR could mislead a real
trading decision. Below is the calculation function (correct once you
feed it real data) and a clearly-stubbed fetch function you'll need to
implement against your live Kite session.
"""

import pandas as pd


def calculate_pcr(total_put_oi: float, total_call_oi: float) -> float | None:
    """PCR = total Put OI / total Call OI. >1 conventionally read as
    bullish-leaning (more puts written = more downside protection sold,
    often near support); <1 as bearish-leaning. Treat as one input among
    several, not a standalone signal."""
    if total_call_oi <= 0:
        return None
    return round(total_put_oi / total_call_oi, 2)


def pcr_vote(pcr: float | None, bullish_threshold: float = 1.1,
             bearish_threshold: float = 0.9) -> str:
    if pcr is None:
        return "NEUTRAL"
    if pcr >= bullish_threshold:
        return "CE"
    if pcr <= bearish_threshold:
        return "PE"
    return "NEUTRAL"


def fetch_option_chain_oi(kite, symbol: str, expiry: str) -> dict:
    """
    STUB - not implemented. This is the piece that needs real work before
    PCR/OI can be live rather than NEUTRAL/placeholder.

    Expected implementation:
        instruments = kite.instruments("NFO")
        chain = [i for i in instruments
                 if i["name"] == symbol and i["expiry"] == expiry]
        tokens = [i["instrument_token"] for i in chain]
        quotes = kite.quote(tokens)   # batch, respect ~500/call limit
        put_oi = sum(q["oi"] for tok, q in quotes.items()
                     if <token is a PE contract>)
        call_oi = sum(q["oi"] for tok, q in quotes.items()
                      if <token is a CE contract>)
        return {"put_oi": put_oi, "call_oi": call_oi}

    Returns None until implemented, so callers must treat OI/PCR as
    optional and fall back to NEUTRAL - which is exactly what
    smart_trade_scorer.py currently does.
    """
    return None
