"""
Quick diagnostic: run this directly to see the FULL traceback for a single
yfinance market-cap lookup, instead of digging through thousands of
suppressed errors in the main run.

Usage: python debug_yfinance.py [SYMBOL]   (default: RELIANCE)
"""
import sys
import traceback

import yfinance as yf

print("yfinance version:", yf.__version__)
print("python version:", sys.version)

symbol = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
print(f"\nTesting {symbol}.NS ...\n")

try:
    t = yf.Ticker(f"{symbol}.NS")
    print("Ticker created OK. Trying fast_info...")
    fi = t.fast_info
    print("fast_info type:", type(fi))
    print("fast_info repr:", fi)
    try:
        print("fast_info['marketCap']:", fi["marketCap"])
    except Exception as e:
        print("  fi['marketCap'] failed:", e)
    try:
        print("fast_info.market_cap:", fi.market_cap)
    except Exception as e:
        print("  fi.market_cap failed:", e)
    try:
        print("fast_info.get('marketCap'):", fi.get("marketCap"))
    except Exception as e:
        print("  fi.get('marketCap') failed:", e)
except Exception:
    print("FULL TRACEBACK:")
    traceback.print_exc()
