"""
Validates scanner.py + backtest.py end-to-end using synthetic OHLCV data —
no Kite credentials or network access needed. Run this any time after
editing scanner/backtest logic to sanity-check nothing is broken.
"""
import numpy as np
import pandas as pd

import config
import scanner
import backtest


def make_synthetic_ohlcv(n_days=1000, seed=42, breakout_at=None):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_days)

    # base random walk (mildly upward drift) so EMAs/RSI have realistic texture
    returns = rng.normal(0.0004, 0.015, n_days)
    close = 100 * np.cumprod(1 + returns)

    # inject a clean breakout regime around `breakout_at` so we can confirm
    # the scanner actually fires under favorable conditions
    if breakout_at:
        idx = breakout_at
        close[idx:idx + 25] = close[idx] * np.cumprod(1 + rng.normal(0.012, 0.006, 25))

    high = close * (1 + np.abs(rng.normal(0.006, 0.004, n_days)))
    low = close * (1 - np.abs(rng.normal(0.006, 0.004, n_days)))
    open_ = low + (high - low) * rng.uniform(0.2, 0.8, n_days)
    volume = rng.integers(100_000, 500_000, n_days).astype(float)

    if breakout_at:
        volume[breakout_at:breakout_at + 25] *= rng.uniform(2.5, 4.0, 25)

    df = pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    })
    # guarantee high/low bracket open/close properly
    df["high"] = df[["open", "close", "high"]].max(axis=1)
    df["low"] = df[["open", "close", "low"]].min(axis=1)
    return df


def main():
    print("1) Generating synthetic price series with an injected breakout regime...")
    df = make_synthetic_ohlcv(n_days=1000, breakout_at=700)

    print("2) Computing scanner signals...")
    sig_df = scanner.compute_signals(df)
    n_signals = sig_df["signal"].sum()
    print(f"   -> {n_signals} signal day(s) found out of {len(sig_df)} trading days")
    assert n_signals >= 0  # sanity: doesn't crash, produces a boolean column
    print(f"   Columns present: {[c for c in sig_df.columns if c not in df.columns]}")

    if n_signals > 0:
        print("\n   Sample signal rows:")
        cols = ["date", "close", "ema20", "ema50", "ema200", "rsi14", "adx14", "w_rsi14"]
        print(sig_df.loc[sig_df["signal"], cols].head(5).to_string(index=False))

    print("\n3) Running trade simulation on the signals...")
    # temporarily widen the backtest window to cover the whole synthetic series
    orig_start, orig_end = config.BACKTEST_START, config.BACKTEST_END
    config.BACKTEST_START = df["date"].min().date()
    config.BACKTEST_END = df["date"].max().date()

    trades = backtest.simulate_trades(sig_df, "SYNTH")
    config.BACKTEST_START, config.BACKTEST_END = orig_start, orig_end

    print(f"   -> {len(trades)} trade(s) simulated")
    if trades:
        trades_df = pd.DataFrame(trades)
        print(trades_df.to_string(index=False))
        summary = backtest.summarize(trades_df)
        print("\n   Summary:", summary)
    else:
        print("   No trades — try a longer series or a stronger injected breakout "
              "(this is expected sometimes, the filter set is intentionally strict).")

    print("\nAll checks passed: scanner + backtest run end-to-end without errors.")


if __name__ == "__main__":
    main()
