"""
Telegram signal alerts — premium-focused trade cards.

Env:
  TELEGRAM_BOT_TOKEN  - from BotFather
  TELEGRAM_CHAT_ID    - @Testalgotrading or numeric -100... id
"""

from __future__ import annotations

import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_CHANNEL = "@Testalgotrading"


def _load_dotenv() -> None:
    """Load repo-root .env into os.environ if present (no python-dotenv dependency).
    __file__ is signal_engine/app/telegram_alerts.py, so the repo root is
    TWO levels up (../..), not one - the previous ../.env pointed at
    signal_engine/.env, which never existed, silently no-op'ing this the
    whole time even though the credentials were sitting one level up."""
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


_load_dotenv()


def _expiry_short(expiry: str | None) -> str:
    """'11AUG26' / '04AUG26' -> '11 AUG'."""
    if not expiry or expiry == "--":
        return "--"
    e = expiry.strip().upper().replace("-", "").replace(" ", "")
    if len(e) >= 5 and e[:2].isdigit():
        return f"{e[:2]} {e[2:5]}"
    return expiry


def _rupee(v: float | None, plus: bool = False) -> str:
    if v is None:
        return "n/a"
    n = int(round(float(v)))
    suffix = "+" if plus else ""
    return f"₹{n}{suffix}"


def _entry_band(low: float | None, mid: float | None, high: float | None) -> str:
    """'Buy around ₹12-13' style band."""
    if low is not None and high is not None:
        a, b = int(math.floor(min(low, high))), int(math.ceil(max(low, high)))
        if a == b:
            b = a + 1
        return f"₹{a}-{b}"
    if mid is not None:
        m = int(round(mid))
        return f"₹{max(1, m - 1)}-{m + 1}"
    return "n/a"


def format_signal_message(signal: dict) -> str:
    """
    Entry card in the requested format:

        NIFTY 24500 PE (04 AUG)
        Entry: Buy around ₹12-13
        Target 1: ₹20
        Target 2: ₹40+
        Stop-Loss: ₹7
    """
    rec = signal.get("recommendation") or {}
    prem = rec.get("premium") or {}
    pl = rec.get("levels_premium") or {}

    symbol = signal.get("symbol", "NIFTY")
    side = signal.get("side") or rec.get("side") or ""
    strike = rec.get("strike") or signal.get("atm_strike")
    expiry = _expiry_short(rec.get("expiry"))
    action = rec.get("action", "SKIP")

    entry_txt = _entry_band(prem.get("low"), prem.get("mid") or pl.get("entry"), prem.get("high"))
    t1 = pl.get("target1")
    t2 = pl.get("target2")
    sl = pl.get("stop_loss")

    # Fallback if premium levels missing: derive rough band from mid only
    if t1 is None and prem.get("mid"):
        mid = float(prem["mid"])
        t1, t2, sl = mid * 1.5, mid * 2.5, mid * 0.55

    lines = [
        f"{symbol} {strike} {side} ({expiry})",
        f"Entry: Buy around {entry_txt}",
        f"Target 1: {_rupee(t1)}",
        f"Target 2: {_rupee(t2, plus=True)}",
        f"Stop-Loss: {_rupee(sl)}",
    ]
    if action == "SKIP":
        reasons = rec.get("reasons") or []
        why = reasons[0] if reasons else "filters not met"
        lines.append(f"Recommendation: SKIP — {why}")
    else:
        lines.append("Recommendation: TAKE")
    return "\n".join(lines)


def format_t1_hit_message(trade: dict) -> str:
    """
    Target achieved card:

        🎯 TARGET 1 ACHIEVED
        NIFTY 24500 PE (04 AUG)
        Entry: ₹12
        Target 1: ₹20
        Profit: ₹8 / premium (+67%)
    """
    symbol = trade.get("symbol", "NIFTY")
    side = trade.get("side", "")
    contract = trade.get("contract") or ""
    # contract may be "24500 PE" — prefer strike+side
    strike = trade.get("strike")
    if strike is None and contract:
        parts = str(contract).split()
        if parts and parts[0].isdigit():
            strike = parts[0]
            if len(parts) > 1:
                side = parts[1]
    expiry = _expiry_short(trade.get("expiry"))

    entry = trade.get("entry_premium")
    if entry is None:
        entry = trade.get("entry_price")
    t1 = trade.get("t1_premium")
    if t1 is None:
        t1 = trade.get("target1")

    entry_f = float(entry) if entry is not None else 0.0
    t1_f = float(t1) if t1 is not None else 0.0
    # Long premium: profit = T1 - entry
    profit = round(t1_f - entry_f, 2)
    pct = round((profit / entry_f) * 100, 0) if entry_f > 0 else 0

    header = f"{symbol} {strike} {side} ({expiry})".replace("  ", " ").strip()
    lines = [
        "🎯 TARGET 1 ACHIEVED",
        header,
        f"Entry: {_rupee(entry_f)}",
        f"Target 1: {_rupee(t1_f)}",
        f"Profit: {_rupee(profit)} / premium (+{int(pct)}%)",
        "Suggestion: book partial, trail SL to cost",
    ]
    return "\n".join(lines)


def telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def _normalize_chat_id(chat_id: str) -> str:
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return DEFAULT_CHANNEL
    # Allow users to paste "Testalgotrading" without @
    if chat_id.startswith("-") or chat_id.lstrip("-").isdigit():
        return chat_id
    if not chat_id.startswith("@"):
        return f"@{chat_id}"
    return chat_id


def send_telegram_message(text: str, dry_run: bool = False) -> dict:
    """
    POST to Telegram Bot API. If dry_run or credentials missing, returns the
    message without sending.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = _normalize_chat_id(os.getenv("TELEGRAM_CHAT_ID", DEFAULT_CHANNEL))

    if dry_run or not token:
        return {
            "ok": True,
            "dry_run": True,
            "chat_id": chat_id,
            "reason": None if dry_run else "TELEGRAM_BOT_TOKEN not set",
            "message": text,
        }

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        data = resp.json()
        return {
            "ok": bool(data.get("ok")),
            "dry_run": False,
            "chat_id": chat_id,
            "telegram_response": data,
            "message": text,
            "status_code": resp.status_code,
        }
    except Exception as e:
        return {
            "ok": False,
            "dry_run": False,
            "chat_id": chat_id,
            "error": str(e),
            "message": text,
        }


def send_t1_hit_alert(trade: dict, dry_run: bool = False) -> dict:
    text = format_t1_hit_message(trade)
    result = send_telegram_message(text, dry_run=dry_run)
    result["event"] = "T1_HIT"
    result["trade_id"] = trade.get("id")
    return result


def send_signal_alert(signal: dict, dry_run: bool = False) -> dict:
    text = format_signal_message(signal)
    result = send_telegram_message(text, dry_run=dry_run)
    result["verdict"] = signal.get("verdict")
    result["symbol"] = signal.get("symbol")
    return result
