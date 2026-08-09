"""
Sends alerts to a Telegram chat via a bot. Setup (one-time):
  1. Message @BotFather on Telegram, /newbot, follow prompts -> get a token.
  2. Message your new bot anything (so it's allowed to message you back).
  3. Get your chat_id: visit https://api.telegram.org/bot<TOKEN>/getUpdates
     after step 2, and read the "chat":{"id": ...} value from the response.
  4. Put both in .env as TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
"""
import requests

import config


def send_telegram_message(text: str) -> bool:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env — skipping alert. "
              "See telegram_alerts.py docstring for setup steps.")
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
        if resp.status_code != 200:
            print(f"[telegram] send failed ({resp.status_code}): {resp.text}")
            return False
        return True
    except Exception as e:
        print(f"[telegram] send failed: {e}")
        return False


def format_smart_money_alert(sig: dict) -> str:
    """Telegram payload for the Smart Money pipeline (BUY/SELL with levels)."""
    side = sig.get("signal", "?")
    emoji = "🟢" if side == "BUY" else "🔴"
    sector = sig.get("sector") or "—"
    conf = sig.get("confidence")
    conf_line = f"Confidence: {conf:.0f}%\n" if conf is not None else ""
    chart_url = sig.get("chart_url") or f"https://www.tradingview.com/chart/?symbol=NSE:{sig['symbol']}"
    rr = sig.get("risk_reward")
    rr_line = f"R:R: {rr:.2f}\n" if rr is not None else ""

    return (
        f"{emoji} <b>{sig['symbol']}</b> — Smart Money <b>{side}</b>\n"
        f"Sector: {sector}\n"
        f"Entry: ₹{sig['entry_price']:.2f}\n"
        f"Stop-loss: ₹{sig['stop_loss']:.2f}\n"
        f"Target: ₹{sig['target']:.2f}\n"
        f"{rr_line}"
        f"{conf_line}"
        f"Time: {sig.get('timestamp', '')}\n"
        f"Chart: {chart_url}\n"
        f"<i>Alert only — no auto-order. Manage the trade yourself.</i>"
    )


def format_intraday_alert(trigger: dict) -> str:
    sources = trigger.get("sources") or []
    source_labels = {
        "momentum_breakout": "Momentum Breakout",
    }
    source_lines = [source_labels.get(s, s.replace("trending:", "Trending in ")) for s in sources]
    confluence_line = (
        f"Confluence: {' + '.join(source_lines)}\n" if len(source_lines) > 1
        else (f"Source: {source_lines[0]}\n" if source_lines else "")
    )
    chart_url = trigger.get("chart_url", f"https://www.tradingview.com/chart/?symbol=NSE:{trigger['symbol']}")

    return (
        f"🚨 <b>{trigger['symbol']}</b> — Opening Range Breakout\n"
        f"{confluence_line}"
        f"Price: ₹{trigger['price']:.2f} (broke OR high ₹{trigger['opening_range_high']:.2f})\n"
        f"VWAP: ₹{trigger['vwap']:.2f} (price above VWAP)\n"
        f"Volume: {trigger['minute_volume']:.0f} vs avg {trigger['avg_minute_volume']:.0f}/min "
        f"({trigger['minute_volume'] / trigger['avg_minute_volume']:.1f}x)\n"
        f"Time: {trigger['time']}\n"
        f"Chart: {chart_url}\n"
        f"Target zone: {config.INTRADAY_MOVE_TARGET_LOW_PCT:.0f}-{config.INTRADAY_MOVE_TARGET_HIGH_PCT:.0f}% "
        f"(this is an alert only — no auto-exit, manage the trade yourself)"
    )


if __name__ == "__main__":
    ok = send_telegram_message("✅ Test message from kite_scanner_bot — Telegram alerts are wired up correctly.")
    print("Sent OK" if ok else "Failed — check TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in .env")
