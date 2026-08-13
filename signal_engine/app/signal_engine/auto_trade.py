"""
Auto-trade gate: enter ATM/OTM option captures when signal quality passes.

Does not place Zerodha orders by default — it arms journal live-capture
(entry premium + T1/SL) so exits can be tracked and reported.
"""

from __future__ import annotations

from app.config import CONFIG
from app.signal_engine import journal
from app.signal_engine.live_capture import capture_entry


def should_auto_enter(signal: dict, otm_steps: int) -> tuple[bool, str]:
    cfg = CONFIG.auto_trade
    if not cfg.enabled:
        return False, "AUTO_TRADE disabled"

    session = signal.get("session") or ""
    if session in cfg.skip_sessions or session == "MARKET CLOSED":
        return False, f"session={session}"

    if "WAIT" in (signal.get("verdict") or "").upper():
        return False, "WAIT - no setup"

    rec = signal.get("recommendation") or {}
    if cfg.require_take and rec.get("action") != "TAKE":
        reasons = "; ".join((rec.get("reasons") or [])[:2]) or "not TAKE"
        return False, f"SKIP — {reasons}"

    # Gate on the raw base score (out of 7), not the derived confidence_pct
    # (which is scaled out of 9 with PA bonus and under-represents a clean
    # base score - see min_base_score's comment in config.py).
    base_score = int(signal.get("base_score") or 0)
    if base_score < cfg.min_base_score:
        return False, f"score {base_score}/7 < {cfg.min_base_score}/7"

    gate = signal.get("risk_gate") or {}
    if not gate.get("can_enter", False):
        return False, gate.get("reason") or "risk gate blocked"

    open_n = len(journal.list_trades("OPEN"))
    if open_n >= cfg.max_open_positions:
        return False, f"max open positions ({cfg.max_open_positions}) reached"

    return True, "OK"


def maybe_auto_enter(feed, signal: dict, risk_mgr, otm_steps: int | None = None) -> dict | None:
    """
    If gates pass, capture one live ATM/OTM trade. Returns capture result or
    None if skipped. Idempotent per poll: won't open if already at max open.
    """
    cfg = CONFIG.auto_trade
    steps = cfg.default_otm_steps if otm_steps is None else otm_steps
    ok, reason = should_auto_enter(signal, steps)
    if not ok:
        return {"ok": False, "auto": True, "skipped": True, "reason": reason}

    result = capture_entry(
        feed, signal, risk_mgr,
        otm_steps=steps,
        lots=1,
        send_telegram=cfg.send_telegram,
    )
    result["auto"] = True
    result["skipped"] = False
    result["gate_reason"] = reason
    return result
