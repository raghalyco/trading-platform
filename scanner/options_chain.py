"""
Minimal NIFTY/BANKNIFTY option chain: per-strike Call/Put open interest for
the nearest expiry, plus a computed Put-Call Ratio (PCR) and a simple
Bulls-vs-Bears sentiment label — the scoped-down version of TradeBrahma's
Option Clock from the Kite Scanner improvement review ("even a minimal
version ... would remove a category TradeBrahma currently owns outright").

Single-snapshot only: this reads Kite's live quote (LTP + OI) once per call.
A multi-day PCR-vs-spot trend chart (TradeBrahma's other differentiator)
would need this snapshot persisted daily somewhere — not implemented here.

PCR interpretation used below (higher PCR = more Put OI relative to Call OI
= more downside hedging/put-selling = read as bullish, the common convention)
is a simplification real options desks argue about — treat the sentiment
label as a rough signal, not a rule.
"""
from datetime import datetime

import pandas as pd

INDEX_SPOT_KEYS = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
}
STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
}


def _nearest_expiry(opts: pd.DataFrame):
    today = pd.Timestamp.now().normalize()
    expiries = sorted(pd.to_datetime(opts["expiry"]).unique())
    upcoming = [e for e in expiries if e >= today]
    return upcoming[0] if upcoming else (expiries[-1] if expiries else None)


def compute_option_chain(kite_client, underlying: str = "NIFTY", strikes_around_atm: int = 10) -> dict:
    underlying = underlying.upper()
    nfo = kite_client.get_nfo_instruments()
    opts = nfo[(nfo["name"] == underlying) & (nfo["segment"] == "NFO-OPT")].copy()
    if opts.empty:
        return {
            "underlying": underlying, "generated_at": datetime.now().isoformat(),
            "error": f"No NFO option instruments found for {underlying}.",
        }

    opts["expiry"] = pd.to_datetime(opts["expiry"])
    expiry = _nearest_expiry(opts)
    opts = opts[opts["expiry"] == expiry]

    spot = None
    try:
        kite_client.limiter.wait()
        q = kite_client.kite.quote([INDEX_SPOT_KEYS.get(underlying, f"NSE:{underlying}")])
        spot = list(q.values())[0]["last_price"]
    except Exception as e:
        print(f"  [warn] option_chain: couldn't fetch {underlying} spot ({e})")

    step = STRIKE_STEP.get(underlying, 50)
    atm_strike = None
    if spot:
        atm_strike = round(spot / step) * step
        low, high = atm_strike - strikes_around_atm * step, atm_strike + strikes_around_atm * step
        opts = opts[(opts["strike"] >= low) & (opts["strike"] <= high)]

    tradingsymbols = opts["tradingsymbol"].tolist()
    keys = [f"NFO:{s}" for s in tradingsymbols]
    quotes = {}
    batch_size = 200
    for i in range(0, len(keys), batch_size):
        batch = keys[i:i + batch_size]
        kite_client.limiter.wait()
        try:
            quotes.update(kite_client.kite.quote(batch))
        except Exception as e:
            print(f"  [warn] option_chain quote batch failed: {e}")

    by_strike: dict = {}
    for _, row in opts.iterrows():
        key = f"NFO:{row['tradingsymbol']}"
        q = quotes.get(key)
        if not q:
            continue
        strike = float(row["strike"])
        side = row["instrument_type"]  # "CE" or "PE"
        entry = by_strike.setdefault(strike, {
            "strike": strike, "ce_oi": None, "pe_oi": None, "ce_ltp": None, "pe_ltp": None,
        })
        if side == "CE":
            entry["ce_oi"] = q.get("oi")
            entry["ce_ltp"] = q.get("last_price")
        elif side == "PE":
            entry["pe_oi"] = q.get("oi")
            entry["pe_ltp"] = q.get("last_price")

    rows = sorted(by_strike.values(), key=lambda r: r["strike"])
    total_ce_oi = sum(r["ce_oi"] or 0 for r in rows)
    total_pe_oi = sum(r["pe_oi"] or 0 for r in rows)
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else None

    if pcr is None:
        sentiment = "Unknown"
    elif pcr > 1.1:
        sentiment = "Bulls (elevated Put OI — support building)"
    elif pcr < 0.9:
        sentiment = "Bears (elevated Call OI — resistance building)"
    else:
        sentiment = "Neutral"

    max_oi = max([r["ce_oi"] or 0 for r in rows] + [r["pe_oi"] or 0 for r in rows] + [1])

    return {
        "underlying": underlying,
        "expiry": expiry.strftime("%Y-%m-%d") if expiry is not None else None,
        "spot": spot,
        "atm_strike": atm_strike,
        "generated_at": datetime.now().isoformat(),
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "pcr": pcr,
        "sentiment": sentiment,
        "max_oi": max_oi,
        "strikes": rows,
        "note": (
            "Single live snapshot (not a multi-day PCR-vs-spot trend). PCR "
            "sentiment reading is a common convention, not a guaranteed signal."
        ),
    }
