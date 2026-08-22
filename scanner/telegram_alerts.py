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
from charts import tradingview_chart_url


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
    chart_url = (
        sig.get("tv_chart_url")
        or tradingview_chart_url(sig["symbol"])
    )
    rr = sig.get("risk_reward")
    rr_line = f"R:R: {rr:.2f}\n" if rr is not None else ""
    pattern = sig.get("pattern") or sig.get("support_type")
    pattern_line = f"Pattern: {pattern}\n" if pattern else ""

    return (
        f"{emoji} <b>{sig['symbol']}</b> — Smart Money <b>{side}</b>\n"
        f"Sector: {sector}\n"
        f"{pattern_line}"
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
    chart_url = (
        trigger.get("tv_chart_url")
        or tradingview_chart_url(trigger["symbol"], interval="5")
    )

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


def format_swing_trade_alert(hit: dict) -> str:
    """Telegram payload for a Swing Trade (Weekly) resistance breakout -
    trendline or horizontal box, confirmed by a volume spike."""
    symbol = hit["symbol"]
    resistance_type = hit.get("resistance_type") or "Resistance"
    resistance_price = hit.get("resistance_price")
    breakout_high = hit.get("breakout_high")
    current_price = hit.get("current_price")
    volume_ratio = hit.get("volume_ratio")
    retest = hit.get("retest")
    ema_cross = hit.get("ema_bullish_cross")
    quality = hit.get("quality")
    stop_loss = hit.get("stop_loss")
    target = hit.get("target")
    rr = hit.get("risk_reward")
    chart_url = hit.get("tv_chart_url") or tradingview_chart_url(symbol, interval="W")

    lines = [
        f"📈 <b>{symbol}</b> — Weekly Resistance Breakout",
        f"Pattern: {resistance_type}",
    ]
    if resistance_price is not None:
        lines.append(f"Resistance/trendline: ₹{resistance_price:.2f}")
    if breakout_high is not None:
        lines.append(f"Breakout high: ₹{breakout_high:.2f}")
    if current_price is not None:
        lines.append(f"Current price: ₹{current_price:.2f}")
    if volume_ratio is not None:
        lines.append(f"Volume: {volume_ratio:.2f}x avg")
    if retest:
        lines.append("Re-tested the broken level and held ✅")
    if ema_cross:
        lines.append("EMA bullish cross confirmed")
    if quality is not None:
        lines.append(f"Score: {quality:.0f}")
    if stop_loss is not None:
        lines.append(f"Stop-loss: ₹{stop_loss:.2f}")
    if target is not None:
        lines.append(f"Target: ₹{target:.2f}")
    if rr is not None:
        lines.append(f"R:R: {rr:.2f}")
    lines.append(f"Chart: {chart_url}")
    lines.append("<i>Alert only — no auto-order. Manage the trade yourself.</i>")
    return "\n".join(lines)


def format_episodic_pivot_alert(hit: dict) -> str:
    """Telegram payload for a fresh Episodic Pivot (delayed EP) trigger —
    a neglected stock's day-0 reaction candle followed by a tight-range
    pullback candle whose high just broke out."""
    symbol = hit["symbol"]
    day0_date = hit.get("day0_date")
    day0_move = hit.get("day0_move_pct")
    rvol = hit.get("day0_rvol_pct")
    pivot_date = hit.get("pivot_date")
    entry = hit.get("entry_price")
    stop = hit.get("stop_loss")
    stop_pct = hit.get("stop_pct")
    target = hit.get("target")
    target_1_3 = hit.get("target_1_3")
    trail_ema = hit.get("trail_ema_period")
    rr = hit.get("risk_reward")
    score = hit.get("score")
    chart_url = hit.get("tv_chart_url") or f"https://in.tradingview.com/symbols/NSE-{symbol}/"
    kell_stacked = hit.get("kell_trend_stacked")
    is_blowoff = hit.get("is_blowoff")
    is_add_on = hit.get("is_add_on")

    badges = []
    if kell_stacked:
        badges.append("📈 Trend-stacked (10>20 EMA>50 SMA)")
    if is_add_on:
        badges.append("➕ ADD-on (later breakout, same day-0)")
    if is_blowoff:
        badges.append("⚠️ Extended/blow-off risk")

    lines = [f"🚀 <b>{symbol}</b> — Episodic Pivot TRIGGERED"]
    if day0_date is not None:
        lines.append(f"Day-0 reaction: {day0_date}" + (f" (+{day0_move:.1f}%)" if day0_move is not None else ""))
    if rvol is not None:
        lines.append(f"Volume shock: {rvol:.0f}% of 50D avg")
    if pivot_date is not None:
        lines.append(f"Pullback candle: {pivot_date}")
    if entry is not None:
        lines.append(f"Entry (GTT above): ₹{entry:.2f}")
    if stop is not None:
        stop_txt = f"Stop-loss: ₹{stop:.2f}"
        if stop_pct is not None:
            stop_txt += f" ({stop_pct:.1f}%)"
        lines.append(stop_txt)
    if target is not None:
        lines.append(f"Target: ₹{target:.2f}")
    if rr is not None:
        lines.append(f"R:R: {rr:.2f}")
    if target_1_3 is not None:
        lines.append(f"Ankur's 1:3 (book ~50%): ₹{target_1_3:.2f} — trail rest below {trail_ema or 20} EMA")
    if score is not None:
        lines.append(f"Score: {score:.0f}")
    for b in badges:
        lines.append(b)
    lines.append(f"Chart: {chart_url}")
    lines.append("<i>Delayed EP — do not chase if it's already gapped up big. Alert only, manage the trade yourself.</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    ok = send_telegram_message("✅ Test message from kite_scanner_bot — Telegram alerts are wired up correctly.")
    print("Sent OK" if ok else "Failed — check TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in .env")
