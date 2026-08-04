"""
Standalone CLI entry point for the intraday monitor — same engine as the
"Intraday Monitor" tab in the web app (intraday_engine.py), for anyone who
prefers running this outside the Flask app (e.g. on a headless server).

Run with: python intraday_monitor.py
Leave it running from before market open (9:15) until close (15:30) — this
is a long-running process, not a one-shot script. For daily reliability,
run this on the same EC2 instance as your existing Telegram bot rather than
a machine that might sleep or lose network mid-day.

IMPORTANT — one thing to verify on your end that this sandbox has no way to
test: the exact field names Kite's live tick payload uses for cumulative
volume and running VWAP. The first raw tick received is printed in full —
check it against what intraday_state.py's process_tick() expects.
"""
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

import config
import universe as universe_mod
from intraday_engine import IntradayEngine
from kite_auth import get_kite_session
from kite_client import KiteDataClient


def main():
    print("=== Intraday monitor: opening-range breakout + volume surge + VWAP ===")

    if not (config.KITE_API_KEY and config.KITE_API_SECRET):
        print("FATAL: KITE_API_KEY / KITE_API_SECRET not set in .env")
        sys.exit(1)

    kite = get_kite_session()
    client = KiteDataClient(kite)

    print("Building universe...")
    universe_df = universe_mod.build_universe(client)

    engine = IntradayEngine()
    result = engine.start(kite, client, universe_df)
    if result.get("error"):
        print(f"FATAL: {result['error']}")
        sys.exit(1)
    print(f"Started. Shortlist size: {result['shortlist_size']}. "
          f"Monitoring until {engine.market_close_dt.time()}. Ctrl+C to stop early.")

    try:
        while datetime.now() < engine.market_close_dt:
            time.sleep(30)
            status = engine.get_status()
            print(f"[heartbeat] {datetime.now().strftime('%H:%M:%S')} — "
                  f"range_locked={status['range_locked']}, alerts_fired={status['alert_count']}")
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        engine.stop()
        print(f"Session ended. Total alerts fired today: {engine.alert_count}")


if __name__ == "__main__":
    main()
