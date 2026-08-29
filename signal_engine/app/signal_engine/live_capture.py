"""
Live trade capture for NIFTY/SENSEX options (ATM or OTM).

Flow:
  1. generate_signal() produces side + index levels + recommendation
  2. capture_entry() locks strike (ATM/OTM), live option LTP, premium T1/SL
  3. poll_open_positions() on each refresh checks live LTP vs T1/SL and
     auto-exits + journals P&L when hit
"""

from __future__ import annotations

from app.config import CONFIG
from app.signal_engine import journal
from app.signal_engine.modes import current_expiry_date_iso
from app.signal_engine.trade_recommendation import (
    LOT_SIZES,
    build_trade_recommendation,
)
from app.signal_engine.risk import RiskManager
from app.telegram_alerts import (
    format_signal_message,
    format_t1_hit_message,
    send_telegram_message,
    telegram_configured,
)


def _live_option_quote(feed, symbol: str, strike: int, side: str) -> dict | None:
    getter = getattr(feed, "get_option_ltp", None)
    expiry_iso = current_expiry_date_iso(symbol)
    if getter is None or not expiry_iso or strike is None:
        return None
    try:
        q = getter(symbol, expiry_iso, int(strike), side)
        return {
            "ltp": float(q["ltp"]),
            "tradingsymbol": q.get("tradingsymbol"),
            "expiry_iso": expiry_iso,
        }
    except Exception as e:
        return {"error": str(e)}


def capture_entry(
    feed,
    signal: dict,
    risk_mgr: RiskManager,
    otm_steps: int = 0,
    lots: int = 1,
    send_telegram: bool = True,
) -> dict:
    """
    Arm a live trade from the current signal using ATM (0) or OTM (1+).
    Stores index + premium levels in the journal and optionally Telegram.
    """
    if signal.get("error"):
        return {"ok": False, "error": signal["error"]}
    if "WAIT" in (signal.get("verdict") or "").upper():
        return {"ok": False, "error": "No setup (WAIT) — nothing to capture"}

    symbol = signal["symbol"]
    side = signal["side"]
    mode = signal["mode"]
    levels = signal["levels"]

    # Rebuild recommendation for the chosen ATM/OTM offset
    rec = build_trade_recommendation(
        feed=feed,
        symbol=symbol,
        side=side,
        mode=mode,
        spot=float(signal.get("spot") or levels["entry"]),
        vix=float(signal.get("vix") or 12),
        levels=levels,
        verdict=signal.get("verdict", ""),
        confidence_pct_val=int(signal.get("confidence_pct") or 0),
        confidence_label_val=signal.get("confidence_label") or "",
        risk_can_enter=bool((signal.get("risk_gate") or {}).get("can_enter", True)),
        risk_reason=(signal.get("risk_gate") or {}).get("reason", ""),
        regime=signal.get("regime"),
        risk_mgr=risk_mgr,
        otm_steps=otm_steps,
    )

    pl = rec.get("levels_premium") or {}
    prem = rec.get("premium") or {}
    entry_premium = float(pl.get("entry") or prem.get("mid") or 0) or None
    if entry_premium is None:
        return {
            "ok": False,
            "error": "Could not resolve entry premium (need live Kite quote or estimate)",
            "recommendation": rec,
        }

    if mode == "GBB":
        # GBB uses a RATIO target (1:1.5 on premium, off the structure-
        # based stop) - rec["levels_premium"]["target1"] already computed
        # this exact number (trade_recommendation.py's GBB branch), so
        # reuse it directly rather than recomputing here and risking the
        # two drifting apart (the exact bug fixed earlier for SCALP's
        # display vs. actual capture mismatch).
        t1_premium = round(float(pl.get("target1") or entry_premium), 2)
    else:
        # Fixed OPTION PREMIUM points target (not index points) - this is
        # the actual exit rule for every captured SCALP/SMART_TRADE trade,
        # replacing the ATR-derived target1. Applies identically to CE and
        # PE: the premium always moves in the buyer's favor as the trade
        # works, regardless of side.
        t1_premium = round(entry_premium + CONFIG.auto_trade.target_premium_points, 2)

    lot_size = LOT_SIZES.get(symbol, 65)
    trade_id = journal.log_entry(
        symbol=symbol,
        side=side,
        signal_source="LIVE_CAPTURE",
        mode=mode,
        entry_price=float(levels["entry"]),
        rr=float(pl.get("rr") or levels.get("rr") or 0),
        target1=float(levels.get("target1")) if levels.get("target1") is not None else None,
        target2=float(levels.get("target2")) if levels.get("target2") is not None else None,
        stop_loss=float(levels.get("stop_loss")) if levels.get("stop_loss") is not None else None,
        contract=rec.get("contract"),
        expiry=rec.get("expiry"),
        strike=rec.get("strike"),
        entry_premium=float(entry_premium),
        t1_premium=t1_premium,
        t2_premium=float(pl["target2"]) if pl.get("target2") is not None else None,
        sl_premium=float(pl["stop_loss"]) if pl.get("stop_loss") is not None else None,
    )

    telegram_result = None
    if send_telegram:
        # Attach chosen recommendation onto signal for the premium-format card
        signal_out = {**signal, "recommendation": rec}
        text = format_signal_message(signal_out)
        telegram_result = send_telegram_message(
            text, dry_run=not telegram_configured()
        )

    return {
        "ok": True,
        "trade_id": trade_id,
        "otm_steps": otm_steps,
        "strike_type": "ATM" if otm_steps == 0 else f"{otm_steps} OTM",
        "contract": rec.get("contract"),
        "expiry": rec.get("expiry"),
        "strike": rec.get("strike"),
        "index_entry": levels.get("entry"),
        "entry_premium": entry_premium,
        "t1_premium": t1_premium,
        "t2_premium": pl.get("target2"),
        "sl_premium": pl.get("stop_loss"),
        "lot_size": lot_size,
        "lots": lots,
        "premium_source": prem.get("source"),
        "tradingsymbol": rec.get("tradingsymbol"),
        "telegram": telegram_result,
        "recommendation": rec,
    }


def poll_open_positions(feed, auto_exit: bool = True) -> list[dict]:
    """
    For each OPEN journal trade with strike + premium levels, fetch live LTP.
    Auto-exit when LTP >= T1 (WIN) or LTP <= SL (LOSS). Also fires Telegram.
    """
    open_trades = journal.list_trades("OPEN")
    results = []

    for trade in open_trades:
        symbol = trade["symbol"]
        side = trade["side"]
        strike = trade.get("strike")
        entry_prem = trade.get("entry_premium")
        t1_prem = trade.get("t1_premium")
        sl_prem = trade.get("sl_premium")

        quote = _live_option_quote(feed, symbol, strike, side) if strike else None
        ltp = None
        if quote and "ltp" in quote:
            ltp = quote["ltp"]

        # Index fallback snapshot
        try:
            index_spot = float(feed.get_spot_price(symbol))
        except Exception:
            index_spot = None

        status = {
            "trade_id": trade["id"],
            "contract": trade.get("contract"),
            "strike": strike,
            "side": side,
            "entry_premium": entry_prem,
            "t1_premium": t1_prem,
            "sl_premium": sl_prem,
            "live_premium": ltp,
            "index_spot": index_spot,
            "tradingsymbol": (quote or {}).get("tradingsymbol"),
            "quote_error": (quote or {}).get("error"),
            "hit": None,
            "exited": False,
        }

        if ltp is None or entry_prem is None:
            # Still try index T1 for alert-only path
            results.append(status)
            continue

        hit = None
        if t1_prem is not None and ltp >= float(t1_prem):
            hit = "T1"
        elif sl_prem is not None and ltp <= float(sl_prem):
            hit = "SL"
        # No time-based force-close: open positions ride until T1 or SL
        # actually hits, however long that takes. Only NEW entries are cut
        # off at CONFIG.auto_trade.no_entry_after (see auto_trade.py).

        status["hit"] = hit
        if hit and auto_exit:
            lot_size = LOT_SIZES.get(symbol, 65)
            exit_info = journal.log_exit(
                trade["id"],
                exit_price=float(ltp),
                lot_size=1,
                points_per_lot_value=float(lot_size),
            )

            payload = {
                **trade,
                "entry_premium": entry_prem,
                "t1_premium": t1_prem if hit == "T1" else ltp,
                "spot_now": ltp,
            }
            text = format_t1_hit_message(payload) if hit == "T1" else _format_sl_hit(payload, ltp)
            tg = send_telegram_message(text, dry_run=not telegram_configured())
            if hit == "T1" and not trade.get("t1_alerted"):
                journal.mark_t1_hit(trade["id"], ltp)

            status["exited"] = True
            status["exit"] = {**exit_info, "reason": hit}
            status["telegram"] = tg

        results.append(status)

    return results


def _format_sl_hit(trade: dict, ltp: float) -> str:
    symbol = trade.get("symbol", "NIFTY")
    strike = trade.get("strike")
    side = trade.get("side", "")
    expiry = trade.get("expiry") or "--"
    entry = float(trade.get("entry_premium") or 0)
    loss = round(entry - ltp, 2)
    pct = round((loss / entry) * 100, 0) if entry else 0
    return "\n".join([
        "🛑 STOP-LOSS HIT",
        f"{symbol} {strike} {side} ({expiry})",
        f"Entry: ₹{int(round(entry))}",
        f"Exit: ₹{int(round(ltp))}",
        f"Loss: ₹{int(round(loss))} / premium (-{int(pct)}%)",
        "Suggestion: stop out — wait for next setup",
    ])
