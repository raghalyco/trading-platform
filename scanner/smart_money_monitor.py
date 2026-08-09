"""
Smart Money signal monitor — runs the full requirement flow:

  top trending sectors → sector leaders → strategy gates → Telegram

One-shot:
  python smart_money_monitor.py

Loop during market hours (re-scan every SMART_MONEY_SCAN_INTERVAL_SEC):
  python smart_money_monitor.py --loop
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

import config
import smart_money_pipeline
import universe as universe_mod
from kite_auth import get_kite_session
from kite_client import KiteDataClient


def _market_open_now() -> bool:
    now = datetime.now().time()
    open_t = datetime.strptime(config.INTRADAY_MARKET_OPEN, "%H:%M").time()
    close_t = datetime.strptime(config.INTRADAY_MARKET_CLOSE, "%H:%M").time()
    return open_t <= now <= close_t


def run_once(client, universe_df) -> dict:
    result = smart_money_pipeline.run_pipeline(client, universe_df)
    print(f"\nDone — {result['num_signals']} signal(s) from "
          f"{result['num_leaders']} leaders across {len(result['sectors'])} sector(s).")
    for sig in result["signals"]:
        print(f"  {sig['signal']:4} {sig['symbol']:12} entry={sig['entry_price']} "
              f"sl={sig['stop_loss']} tp={sig['target']} RR={sig['risk_reward']} "
              f"conf={sig['confidence']}%")
    return result


def main():
    parser = argparse.ArgumentParser(description="Smart Money sector→stock→signal pipeline")
    parser.add_argument("--loop", action="store_true",
                        help="Re-run until market close")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Skip Telegram (still prints signals)")
    args = parser.parse_args()

    if not (config.KITE_API_KEY and config.KITE_API_SECRET):
        print("FATAL: KITE_API_KEY / KITE_API_SECRET not set in .env")
        sys.exit(1)

    if args.no_telegram:
        config.SMART_MONEY_SEND_TELEGRAM = False

    kite = get_kite_session()
    client = KiteDataClient(kite)
    print("Building universe...")
    universe_df = universe_mod.build_universe(client)

    if not args.loop:
        run_once(client, universe_df)
        return

    print(f"Looping every {config.SMART_MONEY_SCAN_INTERVAL_SEC}s until market close. Ctrl+C to stop.")
    try:
        while True:
            if not _market_open_now():
                print(f"[{datetime.now().strftime('%H:%M:%S')}] outside market hours — waiting...")
                time.sleep(60)
                # Stop after close
                now = datetime.now().time()
                close_t = datetime.strptime(config.INTRADAY_MARKET_CLOSE, "%H:%M").time()
                if now > close_t:
                    print("Market closed — exiting.")
                    break
                continue
            run_once(client, universe_df)
            time.sleep(config.SMART_MONEY_SCAN_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
