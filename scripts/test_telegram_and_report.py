"""
Quick smoke test: Telegram message format + performance backtest.
Does not require TELEGRAM_BOT_TOKEN (uses dry_run).
"""

from app.data_feed.kite_feed import KiteFeed, SESSION_FILE
from app.data_feed.simulator import SimulatorFeed
from app.signal_engine.orchestrator import generate_signal
from app.signal_engine.risk import RiskManager
from app.signal_engine.backtest import run_backtest
from app.telegram_alerts import format_signal_message, send_signal_alert, telegram_configured
import os
import sys


def main():
    # Windows consoles often default to cp1252 — force UTF-8 for emoji output
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if os.path.exists(SESSION_FILE):
        try:
            feed = KiteFeed.from_session_file()
            source = "kite"
        except Exception as e:
            print(f"kite session failed ({e}), using simulator")
            feed = SimulatorFeed()
            source = "simulator"
    else:
        feed = SimulatorFeed()
        source = "simulator"

    print(f"data_source={source} telegram_configured={telegram_configured()}")
    risk = RiskManager()
    signal = generate_signal(feed, "NIFTY", "SCALP", risk)
    msg = format_signal_message(signal)
    print("\n=== TELEGRAM MESSAGE (dry-run) ===")
    print(msg)
    print("=================================\n")

    send_result = send_signal_alert(signal, dry_run=True)
    assert send_result["ok"] and send_result["dry_run"]
    assert "Entry:" in send_result["message"]
    assert "Educational only" in send_result["message"]
    print("format + dry_run send: OK")

    df = feed.get_ohlcv_1m("NIFTY", lookback_minutes=375)
    report = run_backtest(df, symbol="NIFTY", mode="SCALP", step_minutes=5)
    print("\n=== PERFORMANCE REPORT ===")
    print(
        f"candles={report['candles']} trades={report['trades']} "
        f"wins={report['wins']} losses={report['losses']} "
        f"timeouts={report['timeouts']} win_rate_pct={report['win_rate_pct']}"
    )
    if report["samples"]:
        s = report["samples"][0]
        print(f"sample[0]: {s['entry_ts']} {s['verdict']} -> {s['result']} ({s['reason']})")
    print("==========================\n")
    assert report["candles"] > 0
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
