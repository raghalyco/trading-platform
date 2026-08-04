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
from app.signal_engine.modes import scalp_levels, smart_trade_levels, current_expiry_date_iso
from app.signal_engine.risk import RiskManager
from app.signal_engine.orb import check_orb_breakout
from app.signal_engine.retest import check_retest
from app.signal_engine.gamma_blast import check_gamma_blast, is_monthly_expiry_today
from app.signal_engine.regime import classify_regime, gate_breakout_signal
from app.signal_engine.session import get_session_label
from app.signal_engine.option_pricing import estimate_premium
from app.config import CONFIG
from app.data_feed.base import DataFeed


def generate_signal(feed: DataFeed, symbol: str, mode: str, risk_mgr: RiskManager) -> dict:
    """
    mode: 'SCALP' or 'SMART_TRADE'
    """
    df = feed.get_ohlcv_1m(symbol, lookback_minutes=120)
    spot = feed.get_spot_price(symbol)
    vix = feed.get_vix()
    is_expiry = feed.is_expiry_day(symbol)
    df_5m = resample_ohlcv(df, "5min")

    # 1. base component scoring - SCALP and SMART TRADE use different scorers
    if mode == "SCALP":
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

    verdict = verdict_label(total_score, side, max_components)

    # 3. target/SL levels for the chosen mode
    # Uses 5-min ATR as the base volatility unit - 1-min ATR alone produces
    # targets/SL an order of magnitude too small for realistic index-option
    # scalp/swing levels (see config.py comments for calibration notes).
    atr_value = float(atr(df_5m).iloc[-1]) if len(df_5m) > 14 else float(atr(df).iloc[-1])
    entry_price = float(df.iloc[-1]["close"])
    if mode == "SCALP":
        levels = scalp_levels(entry_price, side, atr_value)
    else:
        levels = smart_trade_levels(df, side, atr_value, symbol)

    # 4. confidence + cautions
    pct = confidence_pct(total_score, max_score)
    label = confidence_label(pct)
    sl_points = abs(levels["entry"] - levels["stop_loss"])
    cautions = build_cautions(df, is_expiry, sl_points, spot)
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
    retest_signal = check_retest(df, orb_range if orb_range.get("high") else None)
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

    return {
        "symbol": symbol,
        "spot": round(spot, 2),
        "vix": vix,
        "atm_strike": round(spot / 50) * 50,
        "verdict": verdict,
        "side": side,
        "mode": mode,
        "score": total_score,
        "max_score": max_score,
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
        "gamma_blast": gamma_signal,
        "regime": regime_info,
        "session": session_label,
        "estimated_premium": estimated_premium,
        "estimated_premium_note": "MODEL ESTIMATE, not a live quote - see option_pricing.py",
        **smart_extra,
    }
