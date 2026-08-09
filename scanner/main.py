import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# Load .env from the same directory as this script, regardless of the
# current working directory the script is launched from.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

import config
import backtest
import universe as universe_mod
import report
from kite_auth import get_kite_session
from kite_client import KiteDataClient


def _check_credentials():
    placeholders = {
        "KITE_API_KEY": "your_api_key_here",
        "KITE_API_SECRET": "your_api_secret_here",
    }
    missing_or_placeholder = []
    for var, placeholder in placeholders.items():
        val = getattr(config, var, "")
        if not val or val == placeholder:
            missing_or_placeholder.append(var)

    if missing_or_placeholder:
        print("ERROR: the following credentials are missing or still contain "
              "placeholder values from .env.example:")
        for var in missing_or_placeholder:
            print(f"  - {var}")
        env_path = Path(__file__).resolve().parent / ".env"
        print(f"\nExpected a real .env file at: {env_path}")
        print(f"  exists: {env_path.exists()}")
        print("\nFix: copy .env.example to .env in this folder and fill in your "
              "real Kite Connect API key/secret from developers.kite.trade, "
              "then run again.")
        sys.exit(1)


def main():
    print(f"=== Kite scanner backtest ===")
    config.refresh_backtest_window()
    print(f"Window: {config.BACKTEST_START} -> {config.BACKTEST_END} "
          f"(+{config.WARMUP_YEARS}y warmup for indicators)")

    _check_credentials()

    print("\nLogging into Kite...")
    kite = get_kite_session()
    client = KiteDataClient(kite)

    print("\nBuilding universe...")
    uni = universe_mod.build_universe(client)
    if uni.empty:
        print("Universe is empty after filtering — check data/market_cap.csv "
              "or MIN_MARKET_CAP_CR in config.py")
        sys.exit(1)

    print(f"\nRunning backtest over {len(uni)} symbols "
          f"(this can take a while on first run — subsequent runs use the cache)...")
    trades_df = backtest.run_backtest(client, uni)

    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(config.RESULTS_DIR, f"trades_{stamp}.csv")
    trades_df.to_csv(out_path, index=False)

    summary = backtest.summarize(trades_df)
    print("\n=== Summary ===")
    for k, v in summary.items():
        print(f"{k}: {v}")

    print(f"\nFull trade log written to {out_path}")

    report.generate_report(out_path)


if __name__ == "__main__":
    main()
