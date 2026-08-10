"""
Pre-trade recommendation: exact strike, expected premium range, capital,
premium SL/targets, R:R, and TAKE vs SKIP.

Premiums are Black-Scholes estimates (optionally replaced by a live LTP when
KiteFeed.get_option_ltp is available). Always treat premium figures as
estimates unless `premium_source == "live"`.
"""

from __future__ import annotations

from app.config import CONFIG
from app.signal_engine.strike_selector import pick_strike, STRIKE_STEP
from app.signal_engine.option_pricing import estimate_premium
from app.signal_engine.modes import current_expiry_date, current_expiry_date_iso

# NSE/BSE index F&O lot sizes - sourced from Zerodha's official support page
# (support.zerodha.com, "What are the lot sizes for Index F&O contracts?"),
# fetched and verified against a live official source. Previous values in
# this dict were stale/wrong across the board - lot sizes get revised by
# the exchanges periodically, so re-verify this against the same page (or
# your broker's contract note) if trades start sizing oddly.
LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 25,
    "SENSEX": 20,
    "BANKEX": 30,
    "SENSEX50": 75,
}


def _premium_band(spot: float, strike: int, side: str, vix: float,
                  expiry_iso: str) -> dict:
    """Low / mid / high premium estimates via IV multipliers around VIX."""
    cfg = CONFIG.option_pricing
    base_mult = cfg.iv_multiplier
    # Near-expiry options often print richer than raw VIX — widen the band
    # without pretending any single point is exact.
    specs = {
        "low": max(0.7, base_mult * 0.85),
        "mid": max(1.0, base_mult),
        "high": max(1.3, base_mult * 1.4),
    }
    values = {}
    for key, mult in specs.items():
        values[key] = estimate_premium(
            spot=spot, strike=strike, side=side, vix_pct=vix,
            expiry_date_iso=expiry_iso,
            risk_free_rate=cfg.risk_free_rate,
            iv_multiplier=mult,
        )
    return values


def _try_live_premium(feed, symbol: str, expiry_iso: str, strike: int, side: str):
    getter = getattr(feed, "get_option_ltp", None)
    if getter is None or not expiry_iso:
        return None
    try:
        quote = getter(symbol, expiry_iso, strike, side)
        return float(quote["ltp"]), quote.get("tradingsymbol")
    except Exception:
        return None


def _premium_at_spot(spot: float, strike: int, side: str, vix: float,
                     expiry_iso: str, iv_mult: float) -> float | None:
    return estimate_premium(
        spot=spot, strike=strike, side=side, vix_pct=vix,
        expiry_date_iso=expiry_iso,
        risk_free_rate=CONFIG.option_pricing.risk_free_rate,
        iv_multiplier=iv_mult,
    )


def build_trade_recommendation(
    feed,
    symbol: str,
    side: str,
    mode: str,
    spot: float,
    vix: float,
    levels: dict,
    verdict: str,
    confidence_pct_val: int,
    confidence_label_val: str,
    risk_can_enter: bool,
    risk_reason: str,
    regime: dict | None,
    risk_mgr,
    otm_steps: int | None = None,
) -> dict:
    """
    Returns a self-contained recommendation block for API + Telegram.
    """
    # SCALP prefers ATM; SMART TRADE defaults to 1 OTM (matches discretionary style)
    if otm_steps is None:
        otm_steps = 0 if mode == "SCALP" else 1

    # Align strike to the signal's index entry (last closed candle), not a
    # possibly-divergent quote print after hours.
    index_entry = float(levels.get("entry") or spot)
    strike = pick_strike(index_entry, symbol, otm_steps=otm_steps, side=side)
    atm = pick_strike(index_entry, symbol, otm_steps=0, side=side)
    step = STRIKE_STEP.get(symbol, 50)
    expiry_display = current_expiry_date(symbol)
    expiry_iso = current_expiry_date_iso(symbol)
    lot_size = LOT_SIZES.get(symbol, 25)

    band = {"low": None, "mid": None, "high": None}
    premium_source = "estimate"
    tradingsymbol = None
    # Price premiums off the entry spot so strike and model stay consistent
    pricing_spot = index_entry
    if expiry_iso:
        band = _premium_band(pricing_spot, strike, side, vix, expiry_iso)
        live = _try_live_premium(feed, symbol, expiry_iso, strike, side)
        if live:
            live_ltp, tradingsymbol = live
            band["mid"] = live_ltp
            band["low"] = round(live_ltp * 0.92, 2)
            band["high"] = round(live_ltp * 1.08, 2)
            premium_source = "live"

    mid = band.get("mid")
    iv_for_levels = max(1.0, CONFIG.option_pricing.iv_multiplier)

    index_t1 = float(levels.get("target1") or index_entry)
    index_t2 = float(levels.get("target2") or index_entry)
    index_sl = float(levels.get("stop_loss") or index_entry)

    prem_t1 = prem_t2 = prem_sl = None
    if expiry_iso and mid is not None:
        prem_t1 = _premium_at_spot(index_t1, strike, side, vix, expiry_iso, iv_for_levels)
        prem_t2 = _premium_at_spot(index_t2, strike, side, vix, expiry_iso, iv_for_levels)
        prem_sl = _premium_at_spot(index_sl, strike, side, vix, expiry_iso, iv_for_levels)
        if premium_source == "live":
            model_entry = _premium_at_spot(
                index_entry, strike, side, vix, expiry_iso, iv_for_levels
            )
            if model_entry and model_entry > 0:
                scale = mid / model_entry
                if prem_t1:
                    prem_t1 = round(prem_t1 * scale, 2)
                if prem_t2:
                    prem_t2 = round(prem_t2 * scale, 2)
                if prem_sl:
                    prem_sl = round(prem_sl * scale, 2)

    if mid is not None and prem_sl is not None:
        if prem_sl >= mid:
            prem_sl = round(mid * 0.7, 2)
        if prem_t1 is not None and prem_t1 <= mid:
            prem_t1 = round(mid * 1.4, 2)
        if prem_t2 is not None and prem_t2 <= (prem_t1 or mid):
            prem_t2 = round((prem_t1 or mid) * 1.35, 2)

    sl_prem_pts = round(mid - prem_sl, 2) if (mid and prem_sl) else None
    t1_prem_pts = round(prem_t1 - mid, 2) if (mid and prem_t1) else None
    premium_rr = (
        round(t1_prem_pts / sl_prem_pts, 2)
        if sl_prem_pts and t1_prem_pts and sl_prem_pts > 0
        else float(levels.get("rr") or 0)
    )

    risk_budget = risk_mgr.risk_amount_per_trade()
    oversized = False
    if sl_prem_pts and sl_prem_pts > 0:
        max_lots = int(risk_budget / (sl_prem_pts * lot_size))
        recommended_lots = max(1, max_lots) if max_lots >= 1 else 1
        oversized = max_lots < 1
    else:
        recommended_lots = 1

    capital_required = (
        round(mid * lot_size * recommended_lots, 2) if mid is not None else None
    )
    risk_rupees = (
        round(sl_prem_pts * lot_size * recommended_lots, 2)
        if sl_prem_pts is not None else None
    )
    reward_rupees = (
        round(t1_prem_pts * lot_size * recommended_lots, 2)
        if t1_prem_pts is not None else None
    )

    skip_reasons: list[str] = []
    take_reasons: list[str] = []

    if "WAIT" in (verdict or "").upper():
        skip_reasons.append("No clear setup (WAIT)")
    if not risk_can_enter:
        skip_reasons.append(risk_reason or "Risk gate blocked")
    if confidence_pct_val < 55 or "SKIP" in (confidence_label_val or "").upper():
        skip_reasons.append(f"Confidence too low ({confidence_label_val})")
    if premium_rr and premium_rr < CONFIG.risk.min_rr:
        skip_reasons.append(f"Premium R:R {premium_rr} < min {CONFIG.risk.min_rr}")
    if oversized:
        skip_reasons.append(
            f"1 lot risk ~Rs {risk_rupees} exceeds {CONFIG.risk.risk_per_trade_pct}% budget "
            f"(Rs {round(risk_budget)})"
        )
    regime_name = (regime or {}).get("regime")
    if regime_name == "RANGE" and mode == "SCALP":
        skip_reasons.append("RANGE regime — scalp breakouts often fail")

    if confidence_pct_val >= 75:
        take_reasons.append(f"High confidence ({confidence_pct_val}%)")
    if premium_rr and premium_rr >= CONFIG.risk.min_rr:
        take_reasons.append(f"R:R 1:{premium_rr} meets minimum")
    if risk_can_enter and "WAIT" not in (verdict or "").upper():
        take_reasons.append("Risk gate OK")

    action = "SKIP" if skip_reasons else "TAKE"
    if action == "TAKE" and not take_reasons:
        take_reasons.append("Passes filters")

    return {
        "action": action,
        "worth_taking": action == "TAKE",
        "reasons": skip_reasons if action == "SKIP" else take_reasons,
        "symbol": symbol,
        "side": side,
        "mode": mode,
        "expiry": expiry_display,
        "expiry_iso": expiry_iso,
        "strike": strike,
        "atm_strike": atm,
        "otm_steps": otm_steps,
        "strike_step": step,
        "contract": f"{strike} {side}",
        "tradingsymbol": tradingsymbol,
        "lot_size": lot_size,
        "recommended_lots": recommended_lots,
        "premium": {
            "source": premium_source,
            "low": band.get("low"),
            "mid": band.get("mid"),
            "high": band.get("high"),
            "note": (
                "Live LTP from Kite"
                if premium_source == "live"
                else "MODEL ESTIMATE range (not a live quote) — verify on broker before entry"
            ),
        },
        "levels_index": {
            "entry": round(index_entry, 2),
            "stop_loss": round(index_sl, 2),
            "target1": round(index_t1, 2),
            "target2": round(index_t2, 2),
            "rr": levels.get("rr"),
        },
        "levels_premium": {
            "entry": mid,
            "stop_loss": prem_sl,
            "target1": prem_t1,
            "target2": prem_t2,
            "sl_points": sl_prem_pts,
            "t1_points": t1_prem_pts,
            "rr": premium_rr,
        },
        "capital_required": capital_required,
        "risk_rupees": risk_rupees,
        "reward_rupees_t1": reward_rupees,
        "risk_budget": round(risk_budget, 2),
        "account_capital": CONFIG.risk.capital,
    }
