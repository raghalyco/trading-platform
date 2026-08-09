"""
Independently recomputes the summary stats (win rate, avg P&L, etc.) from a
trades CSV/export, done a completely different way than backtest.py's
summarize() function — so if there's a bug in one, this catches it.

Usage:
    python verify_backtest.py results/trades_20260730_....csv
"""
import sys
import pandas as pd

if len(sys.argv) < 2:
    print("Usage: python verify_backtest.py path/to/trades.csv")
    sys.exit(1)

df = pd.read_csv(sys.argv[1])
print(f"Loaded {len(df)} trades from {sys.argv[1]}\n")

# --- 1. Manual, independent win-rate calculation ---
wins = 0
losses = 0
breakeven = 0
for _, row in df.iterrows():
    if row["pnl_pct"] > 0:
        wins += 1
    elif row["pnl_pct"] < 0:
        losses += 1
    else:
        breakeven += 1

total = wins + losses + breakeven
print(f"Manual count: {wins} wins, {losses} losses, {breakeven} breakeven, {total} total")
print(f"Manual win rate: {wins / total * 100:.1f}%  <-- compare this to the dashboard's Win Rate KPI\n")

# --- 2. Sanity checks that would reveal a logic bug ---
print("Sanity checks:")

# a) every trade should have exit_date after entry_date after signal_date
df["signal_date"] = pd.to_datetime(df["signal_date"])
df["entry_date"] = pd.to_datetime(df["entry_date"])
df["exit_date"] = pd.to_datetime(df["exit_date"])
bad_order = df[(df["entry_date"] < df["signal_date"]) | (df["exit_date"] < df["entry_date"])]
print(f"  Trades with impossible date ordering (should be 0): {len(bad_order)}")

# b) target_hit trades should all show ~15% (or your configured TARGET_MAX_PCT)
target_hits = df[df["exit_reason"] == "target_hit"]
if len(target_hits):
    print(f"  target_hit trades: min pnl={target_hits['pnl_pct'].min():.1f}%, "
          f"max pnl={target_hits['pnl_pct'].max():.1f}% (should both be ~15%, "
          f"or your configured TARGET_MAX_PCT)")

# c) no symbol should have overlapping trades (entry before previous exit)
print("  Checking for overlapping trades per symbol...")
overlap_found = False
for symbol, g in df.groupby("symbol"):
    g = g.sort_values("entry_date")
    prev_exit = None
    for _, row in g.iterrows():
        if prev_exit is not None and row["entry_date"] < prev_exit:
            print(f"    OVERLAP found in {symbol}: entry {row['entry_date'].date()} "
                  f"before prior exit {prev_exit.date()}")
            overlap_found = True
        prev_exit = row["exit_date"]
if not overlap_found:
    print("  No overlapping trades found (correct — one position per symbol at a time)")

# d) hit_min_target flag should match pnl_pct >= 10 exactly
if "hit_min_target" in df.columns:
    mismatch = df[(df["pnl_pct"] >= 10) != df["hit_min_target"]]
    print(f"  hit_min_target flag mismatches (should be 0): {len(mismatch)}")

print(f"\nAvg P&L (manual): {df['pnl_pct'].mean():.2f}%  <-- compare to dashboard's Avg P&L")
print(f"Avg duration (manual): {df['duration_days'].mean():.1f} days  <-- compare to dashboard")
