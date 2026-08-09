"""
Answers "on what basis was this stock flagged on date X" precisely, instead
of guessing from a chart: fetches real historical data for one symbol and
shows every one of the 13 rules (plus the EMA10 scanner), pass/fail, with
the actual numbers, for a specific date.

Usage:
    python explain_signal.py FERMENTA 2026-06-05
    python explain_signal.py FERMENTA 2026-06-05 --ema10   (also show the EMA10 scanner)
"""
import sys
from datetime import datetime, timedelta

import config
import ema10_scanner
import scanner
from kite_auth import get_kite_session
from kite_client import KiteDataClient


def _fmt(label, value, threshold, passed):
    mark = "PASS" if passed else "FAIL"
    return f"  [{mark}] {label}: {value}  (threshold: {threshold})"


def explain(symbol: str, target_date_str: str, show_ema10: bool):
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()

    kite = get_kite_session()
    client = KiteDataClient(kite)

    instruments = client.get_nse_equity_instruments()
    match = instruments[instruments["tradingsymbol"] == symbol.upper()]
    if match.empty:
        print(f"Symbol '{symbol}' not found in the NSE equity instrument list "
              f"(check spelling — use the exact Kite tradingsymbol).")
        sys.exit(1)
    token = match.iloc[0]["instrument_token"]

    from_date = target_date - timedelta(days=365 * config.WARMUP_YEARS)
    to_date = target_date + timedelta(days=5)  # small buffer past the target date
    daily = client.get_daily_history(token, symbol.upper(), from_date, to_date)

    if daily.empty:
        print(f"No historical data returned for {symbol}.")
        sys.exit(1)

    sig_df = scanner.compute_signals(daily)
    sig_df["d"] = sig_df["date"].dt.date
    row_matches = sig_df[sig_df["d"] == target_date]
    if row_matches.empty:
        available = sig_df["d"].tolist()
        nearest = min(available, key=lambda d: abs((d - target_date).days))
        print(f"No trading data for {symbol} on exactly {target_date} "
              f"(market holiday/weekend?). Nearest trading day: {nearest}. "
              f"Rerun with that date if that's what you meant.")
        sys.exit(1)
    row = row_matches.iloc[0]

    print(f"\n=== {symbol} on {target_date} — 13-rule breakout scanner ===")
    print(f"Close: {row['close']:.2f}  High: {row['high']:.2f}  Volume: {int(row['volume']):,}\n")

    print(_fmt("Close > EMA20", f"{row['close']:.2f} vs {row['ema20']:.2f}",
               "close > ema20", row["c1_close_gt_ema20"]))
    print(_fmt("EMA20 > EMA50", f"{row['ema20']:.2f} vs {row['ema50']:.2f}",
               "ema20 > ema50", row["c2_ema20_gt_ema50"]))
    print(_fmt("EMA50 > EMA200", f"{row['ema50']:.2f} vs {row['ema200']:.2f}",
               "ema50 > ema200", row["c3_ema50_gt_ema200"]))
    print(_fmt("Weekly close > Weekly EMA20", f"{row['w_close']:.2f} vs {row['w_ema20']:.2f}",
               "w_close > w_ema20", row["c4_weekly_close_gt_wema20"]))
    print(_fmt("20-day breakout", f"close {row['close']:.2f} vs prior 20d high {row['breakout_level']:.2f}",
               "close > 20d high (1 day ago)", row["c5_breakout_20d"]))
    print(_fmt("Volume surge", f"{int(row['volume']):,} vs {config.VOLUME_MULTIPLIER}x avg ({row['vol_sma20']:.0f})",
               f">{config.VOLUME_MULTIPLIER}x 20d avg volume", row["c6_volume_surge"]))
    print(_fmt("Close near high", f"{row['close']:.2f} vs {config.CLOSE_NEAR_HIGH_PCT}x high ({row['high']:.2f})",
               f">={config.CLOSE_NEAR_HIGH_PCT}x day's high", row["c7_close_near_high"]))
    print(_fmt("RSI(14) > 60", f"{row['rsi14']:.1f}", f"> {config.RSI_MIN}", row["c8_rsi_gt_60"]))
    print(_fmt("RSI(14) < 75", f"{row['rsi14']:.1f}", f"< {config.RSI_MAX}", row["c9_rsi_lt_75"]))
    print(_fmt("ADX(14) > 25", f"{row['adx14']:.1f}", f"> {config.ADX_MIN}", row["c10_adx_gt_25"]))
    print(_fmt("Not overextended", f"{row['close']:.2f} vs {config.EXTENSION_CAP}x EMA20 ({row['ema20']*config.EXTENSION_CAP:.2f})",
               f"< {config.EXTENSION_CAP}x EMA20", row["c12_not_overextended"]))
    print(_fmt("Weekly RSI(14) > 50", f"{row['w_rsi14']:.1f}", f"> {config.WEEKLY_RSI_MIN}", row["c13_weekly_rsi_gt_50"]))

    print(f"\n  => Overall signal: {'FIRED' if row['signal'] else 'DID NOT FIRE'}")
    print("  Note: market cap filter is applied separately, upstream, when building the universe —")
    print("  not shown here since it doesn't depend on the date.")

    if show_ema10:
        e_df = ema10_scanner.compute_signals(daily)
        e_df["d"] = e_df["date"].dt.date
        e_row = e_df[e_df["d"] == target_date].iloc[0]
        print(f"\n=== {symbol} on {target_date} — EMA10 pullback scanner ===")
        print(_fmt("Close > EMA10", f"{e_row['close']:.2f} vs {e_row['ema10']:.2f}",
                   "close > ema10", e_row["close"] > e_row["ema10"]))
        print(_fmt("Distance from EMA10", f"{e_row['distance_from_ema10_pct']:.2f}%",
                   f"<= {config.EMA10_MAX_DISTANCE_PCT}%",
                   e_row["distance_from_ema10_pct"] <= config.EMA10_MAX_DISTANCE_PCT))
        print(f"\n  => Overall signal: {'FIRED' if e_row['signal'] else 'DID NOT FIRE'}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python explain_signal.py SYMBOL YYYY-MM-DD [--ema10]")
        sys.exit(1)
    explain(sys.argv[1], sys.argv[2], "--ema10" in sys.argv)
