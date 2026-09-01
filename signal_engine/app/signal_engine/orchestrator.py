"""
Single entry point: generate_signal() runs the whole pipeline and returns
exactly the payload the dashboard needs to render a card like your
screenshots (verdict, score, PA, targets, confidence, cautions).
"""

from app.indicators.core import atr, candle_range_pct
from app.indicators.multi_tf import resample_ohlcv
from app.price_action.patterns import detect_pattern, pa_bonus_points
from app.signal_engine.scorer import score_components, compute_score, verdict_label
from app.signal_engine.smart_scorer import score_smart_trade, compute_smart_score, SMART_TRADE_COMPONENTS
from app.signal_engine.confidence import confidence_pct, confidence_label, build_cautions
from app.signal_engine.modes import scalp_levels, smart_trade_levels, gbb_levels, current_expiry_date_iso
from app.signal_engine.gbb_setup import compute_gbb_signal
from app.signal_engine.risk import RiskManager
from app.signal_engine.orb import check_orb_breakout
from app.signal_engine.retest import check_retest
from app.signal_engine.intraday_sr import find_intraday_zones
from app.signal_engine.gamma_blast import check_gamma_blast, is_monthly_expiry_today
from app.signal_engine.regime import classify_regime, gate_breakout_signal
from app.signal_engine.session import get_session_label
from app.signal_engine.option_pricing import estimate_premium
from app.signal_engine.trade_recommendation import build_trade_recommendation
from app.config import CONFIG
from app.data_feed.base import DataFeed

import time

# Multi-day 5-min history for S/R zone detection is fetched on a slow
# cache (not every generate_signal() call, which can run every ~15s from
# the background loop across 2 symbols) - a zone drawn from swing highs/
# lows over the last few days doesn't meaningfully change minute to
# minute, so there's no reason to re-fetch and re-cluster that often.
_SR_ZONE_CACHE: dict[str, dict] = {}
_SR_ZONE_CACHE_TTL_SECONDS = 20 * 60


def _get_intraday_zones(feed: DataFeed, symbol: str) -> dict:
    empty = {"key_levels": [], "day_levels": [], "all_levels": [],
             "nearest_resistance": None, "nearest_support": None, "price": None}
    cached = _SR_ZONE_CACHE.get(symbol)
    now = time.time()
    if cached and (now - cached["ts"]) < _SR_ZONE_CACHE_TTL_SECONDS:
        return cached["zones"]
    try:
        getter = getattr(feed, "get_ohlcv_history", None)
        zones = find_intraday_zones(getter(symbol, days=5, interval="5minute")) if getter else empty
    except Exception as e:
        print(f"[intraday_sr] zone fetch failed for {symbol}: {e}")
        zones = cached["zones"] if cached else empty
    _SR_ZONE_CACHE[symbol] = {"ts": now, "zones": zones}
    return zones


def generate_signal(feed: DataFeed, symbol: str, mode: str, risk_mgr: RiskManager,
                    otm_steps: int | None = None) -> dict:
    """
    mode: 'SCALP', 'SMART_TRADE', or 'GBB'
    """
    df = feed.get_ohlcv_1m(symbol, lookback_minutes=120)
    spot = feed.get_spot_price(symbol)
    vix = feed.get_vix()
    is_expiry = feed.is_expiry_day(symbol)
    df_5m = resample_ohlcv(df, "5min")

    # 1. base component scoring - SCALP, SMART TRADE, and GBB each use a
    # different scorer/setup-detection engine
    if mode == "GBB":
        # GBB needs a genuine SESSION vwap (anchored at market open), not
        # the short 120-minute rolling window the other two modes use for
        # everything else - fetch a longer lookback just for this branch.
        df_gbb_1m = feed.get_ohlcv_1m(symbol, lookback_minutes=400)
        df_gbb_5m = resample_ohlcv(df_gbb_1m, "5min")
        gbb = compute_gbb_signal(df_gbb_5m, df_gbb_1m, CONFIG.gbb.min_grade_score_pct)
        side = gbb["side"]
        if side is None:
            # No live setup - still need SOME side so downstream strike
            # selection/premium pricing doesn't crash; verdict below forces
            # WAIT regardless, so this never becomes a real recommendation.
            # Use gbb_setup.py's close-vs-VWAP bias so the placeholder
            # contract shown at least matches actual current price action
            # instead of always defaulting to CE regardless of trend.
            side = gbb.get("bias") or "CE"
        # Rescaled onto the same 0-7 range min_base_score/auto_trade.py's
        # gate already uses, so the shared anti-overtrading/score gate
        # keeps working for GBB without needing mode-aware changes there.
        base = {"score": round(gbb["score"] / gbb["max_score"] * 7, 1) if gbb["max_score"] else 0, "side": side}
        max_components = 7
        comp = {"votes": gbb["votes"]}
        smart_extra = {"gbb": gbb}
    elif mode == "SCALP":
        comp = score_components(df)
        base = compute_score(comp["votes"])
        side = base["side"]  # CE or PE
        max_components = base["max_score"]
        smart_extra = {}
    else:
        smart = score_smart_trade(df)
        base = compute_smart_score(smart["votes"])
        side = base["side"]
        comp = {"votes": smart["votes"]}  # keep payload shape consistent
        max_components = base["max_score"]
        smart_extra = {
            "adx_value": smart["adx_value"],
            "di_plus": smart["di_plus"],
            "di_minus": smart["di_minus"],
            "pcr": smart["pcr"],
            "oi_live": smart["oi_live"],
        }

    # 2. price action confirmation
    pa = detect_pattern(df)
    bonus = pa_bonus_points(pa, side)
    total_score = base["score"] + bonus
    max_score = max_components + 2  # base components + up to 2 PA bonus

    if mode == "GBB":
        gbb_grade = smart_extra["gbb"]["grade"]
        gbb_state = smart_extra["gbb"]["state"]
        if gbb_grade == "NO TRADE" or smart_extra["gbb"]["side"] is None:
            verdict = "WAIT - " + gbb_state
        elif gbb_grade in ("A+", "A"):
            verdict = ("STRONG BUY" if side == "CE" else "STRONG SELL")
        else:
            verdict = "BUY" if side == "CE" else "SELL"
    else:
        verdict = verdict_label(total_score, side, max_components)

    # 3. target/SL levels for the chosen mode
    # Uses 5-min ATR as the base volatility unit - 1-min ATR alone produces
    # targets/SL an order of magnitude too small for realistic index-option
    # scalp/swing levels (see config.py comments for calibration notes).
    atr_value = float(atr(df_5m).iloc[-1]) if len(df_5m) > 14 else float(atr(df).iloc[-1])
    entry_price = float(df.iloc[-1]["close"])
    if mode == "GBB":
        levels = gbb_levels(entry_price, side, smart_extra["gbb"].get("structure_stop"), atr_value)
    elif mode == "SCALP":
        levels = scalp_levels(entry_price, side, atr_value)
    else:
        levels = smart_trade_levels(df, side, atr_value, symbol)

    # 4. confidence + cautions
    # Computed on the raw base score alone (base["score"] / max_components),
    # NOT total_score/max_score (which folds in the 0-2 PA bonus and made
    # confidence_pct badly diverge from "N of 7 components agree" - e.g. a
    # clean 5/7 with zero PA bonus read as 56%, not ~71%, silently blocking
    # otherwise-good signals when compared against a 70-75% gate).
    pct = confidence_pct(base["score"], max_components)
    label = confidence_label(pct)
    sl_points = abs(levels["entry"] - levels["stop_loss"])
    cautions = build_cautions(df, is_expiry, sl_points, spot, mode=mode,
                               gbb_result=smart_extra.get("gbb") if mode == "GBB" else None)
    if is_monthly_expiry_today(symbol):
        cautions.append(
            f"{symbol} Monthly Expiry (rule uncertain - verify against your broker's contract notes)"
        )

    # 5. risk gate
    can_enter, risk_reason = risk_mgr.can_enter(levels["rr"])

    # 6. additional signal types (ORB, Retest, Gamma Blast)
    orb_signal = check_orb_breakout(df, df_5m)
    orb_range = orb_signal.get("orb_range") or {
        "high": orb_signal.get("orb_high"), "low": orb_signal.get("orb_low")
    }
    intraday_zones = _get_intraday_zones(feed, symbol)
    sr_zone_levels = [z["level"] for z in (intraday_zones.get("all_levels") or [])]
    retest_signal = check_retest(
        df, orb_range if orb_range.get("high") else None, sr_zones=sr_zone_levels,
    )
    gamma_signal = check_gamma_blast(symbol, df, vix)

    # 7. regime detection - gates ORB/Retest, which produce more false
    # signals in range-bound conditions than in trending ones
    regime_info = classify_regime(df_5m)
    orb_signal = gate_breakout_signal(orb_signal, regime_info)
    retest_signal = gate_breakout_signal(retest_signal, regime_info)

    # 8. session label + estimated premium (both honestly caveated - see
    # session.py and option_pricing.py docstrings for limitations, especially
    # option_pricing's known under-estimation for near-expiry contracts)
    session_label = get_session_label()
    atm_strike_for_premium = round(spot / 50) * 50
    expiry_iso = current_expiry_date_iso(symbol)
    estimated_premium = None
    if expiry_iso:
        estimated_premium = estimate_premium(
            spot=spot, strike=atm_strike_for_premium, side=side, vix_pct=vix,
            expiry_date_iso=expiry_iso,
            iv_multiplier=CONFIG.option_pricing.iv_multiplier,
        )

    # 9. full trade recommendation (strike, premium band, capital, TAKE/SKIP)
    recommendation = build_trade_recommendation(
        feed=feed,
        symbol=symbol,
        side=side,
        mode=mode,
        spot=spot,
        vix=vix,
        levels=levels,
        verdict=verdict,
        confidence_pct_val=pct,
        confidence_label_val=label,
        risk_can_enter=can_enter,
        risk_reason=risk_reason,
        regime=regime_info,
        risk_mgr=risk_mgr,
        otm_steps=otm_steps,
    )

    return {
        "symbol": symbol,
        "spot": round(spot, 2),
        "vix": vix,
        "atm_strike": recommendation["atm_strike"],
        "verdict": verdict,
        "side": side,
        "mode": mode,
        "score": total_score,
        "max_score": max_score,
        "base_score": base["score"],
        "max_components": max_components,
        "pa_bonus": bonus,
        "components": comp["votes"],
        "price_action": pa,
        "levels": levels,
        "confidence_pct": pct,
        "confidence_label": label,
        "cautions": cautions,
        "risk_gate": {"can_enter": can_enter, "reason": risk_reason},
        "daily_risk": risk_mgr.daily_summary(),
        "orb": orb_signal,
        "retest": retest_signal,
        "intraday_zones": {
            "nearest_resistance": intraday_zones.get("nearest_resistance"),
            "nearest_support": intraday_zones.get("nearest_support"),
            "key_levels": intraday_zones.get("key_levels"),
            "day_levels": intraday_zones.get("day_levels"),
        },
        "gamma_blast": gamma_signal,
        "regime": regime_info,
        "session": session_label,
        "estimated_premium": estimated_premium,
        "estimated_premium_note": "MODEL ESTIMATE, not a live quote - see option_pricing.py",
        "recommendation": recommendation,
        **smart_extra,
    }
