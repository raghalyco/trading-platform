"""
Intraday SELL-signal backtest for the Smart Money (GainzAlgo) strategy,
restricted to the F&O-eligible stock universe.

Exact scope requested:
  - SELL signals only (Pine's CHoCH/BOS + momentum + trend "sell_condition").
  - Only counted while the underlying stock is ALREADY in a downtrend —
    on top of the live strategy's own HTF(15m)/LTF(5m) bearish-trend
    gates, this adds a hard filter: the stock's DAILY close must have
    been below its own 50-day EMA as of the prior day's close. A SELL
    signal firing on a stock that is not structurally down is dropped,
    it never reaches the trade log.
  - Intraday only: entry and exit both happen inside the same trading
    session (09:15-15:30 IST). No overnight carry. A signal with no
    same-day bars left to exit on is skipped. If neither the ATR stop
    nor the ATR target is hit by the close of that session, the trade
    is squared off at the last bar's close ("eod_square_off") — matching
    how this setup is actually meant to be traded (fixed point TP/SL in
    the original Pine script, ATR-scaled here same as the live BUY side).
  - Universe: universe.build_fno_universe() — every NSE stock with listed
    futures/options, not the broader Nifty 500 cash list.

This module only computes signals against whatever history the Kite
client returns; it never places an order. It needs a LIVE, logged-in
Kite session (today's access token) to pull intraday history, so it must
be run from the machine that holds that token — not from a sandbox with
no Kite credentials.

Run it directly:

    cd scanner
    .venv\\Scripts\\activate      (Windows)   or   source .venv/bin/activate
    python smart_money_intraday_backtest.py --months 3

Or hit the Flask route added alongside the other backtests:

    GET/POST /api/smart_money/intraday_sell_backtest?months=3
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta
from tqdm import tqdm

import config
import indicators as ind
import smart_money_strategy as sms
import universe as universe_mod

OPTION_RISK_FREE_RATE = 0.07  # annualized, flat approximation (T-bill-ish)
OPTION_MIN_IV = 0.20          # floor/ceiling so a thin-history vol estimate
OPTION_MAX_IV = 0.90          # can't send the model wildly off
OPTION_FALLBACK_IV = 0.35     # used when realized vol can't be computed at all


def _fetch_intraday_chunked(
    kite_client, token, symbol: str, interval: str,
    from_dt: datetime, to_dt: datetime, chunk_days: int = 90,
) -> pd.DataFrame:
    """Kite's minute-level historical API caps how far back a single call
    can reach, so a multi-month intraday backtest has to be fetched in
    windows and stitched back together."""
    frames = []
    cur = from_dt
    while cur < to_dt:
        chunk_end = min(cur + timedelta(days=chunk_days), to_dt)
        df = kite_client.get_history(token, symbol, interval, cur, chunk_end)
        if df is not None and not df.empty:
            frames.append(df)
        cur = chunk_end
    if not frames:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    out = pd.concat(frames).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return out


def _daily_downtrend_flags(daily: pd.DataFrame) -> dict:
    """{date: bool} — was the stock already below its own daily EMA as of
    the PRIOR day's close (no lookahead into the still-open session)."""
    if daily is None or daily.empty or len(daily) < 55:
        return {}
    d = daily.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    ema_n = getattr(config, "SMART_MONEY_DAILY_DOWNTREND_EMA", 50)
    ema_val = ind.ema(d["close"].astype(float), ema_n)
    is_down_asof_prior_close = (d["close"].astype(float) < ema_val).shift(1).fillna(False)
    dates = pd.to_datetime(d["date"]).dt.date
    return dict(zip(dates, is_down_asof_prior_close))


def _align_htf_trend(df5: pd.DataFrame, df15: pd.DataFrame) -> pd.Series:
    """As-of map the 15-min EMA20+VWAP trend onto every 5-min bar."""
    if df15 is None or df15.empty or len(df15) < 25:
        return pd.Series(0, index=df5.index, dtype=int)
    t15 = sms._trend_series(df15.reset_index(drop=True))
    d15 = pd.to_datetime(df15["date"]).reset_index(drop=True)
    d5 = pd.to_datetime(df5["date"])
    left = pd.DataFrame({"date": d5, "_i": np.arange(len(df5))}).sort_values("date")
    right = pd.DataFrame({"date": d15, "trend": t15.values}).sort_values("date")
    merged = pd.merge_asof(left, right, on="date", direction="backward")
    out = pd.Series(0, index=df5.index, dtype=int)
    valid = merged["trend"].notna()
    out.iloc[merged.loc[valid, "_i"].astype(int)] = merged.loc[valid, "trend"].astype(int).values
    return out


def build_intraday_sell_signal_frame(
    sig5: pd.DataFrame, htf15: pd.DataFrame, daily: pd.DataFrame,
) -> pd.DataFrame:
    """Vectorized Pine-style SELL condition on 5-min bars, plus the hard
    daily-downtrend gate. Adds an 'atr' column used for trade sizing."""
    df = sig5.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    out = df.copy()
    out["signal"] = False
    out["atr"] = np.nan

    min_bars = max(
        config.SMART_MONEY_PIVOT_LENGTH * 2 + 5,
        config.SMART_MONEY_VOLUME_LONG + 5,
        60,
    )
    if len(df) < min_bars:
        return out

    close = df["close"].astype(float)
    atr_s = ind.atr(df["high"], df["low"], df["close"], 14)
    atr_s = atr_s.fillna((df["high"] - df["low"]).astype(float))
    vol_factor = (atr_s / close.replace(0, np.nan)).fillna(0)
    mom_thr = config.SMART_MONEY_MOMENTUM_THRESHOLD_PCT * (1 + vol_factor * 2)
    price_change = (close - close.shift(1)) / close.shift(1).replace(0, np.nan) * 100

    last_high, last_low = sms._pivot_levels(df["high"], df["low"], config.SMART_MONEY_PIVOT_LENGTH)
    _choch_buy, choch_sell, _bos_buy, bos_sell = sms._structure_flags(df, last_high, last_low)
    lookback = config.SMART_MONEY_STRUCTURE_LOOKBACK
    structure_sell = (choch_sell | bos_sell).rolling(lookback, min_periods=1).max().astype(bool)

    trend_5m = sms._trend_series(df)
    trend_htf = _align_htf_trend(df, htf15)

    vol_avg = ind.sma(df["volume"].astype(float), config.SMART_MONEY_VOLUME_LONG)
    vol_short = ind.sma(df["volume"].astype(float), config.SMART_MONEY_VOLUME_SHORT)
    vol_ok = (df["volume"].astype(float) > vol_avg) & (vol_short.diff() > 0)

    lowest = df["low"].rolling(config.SMART_MONEY_BREAKOUT_PERIOD).min().shift(1)
    breakout_sell = close < lowest

    momentum_sell = price_change < -mom_thr
    htf_sell = trend_htf == -1
    ltf_sell = trend_5m == -1

    daily_downtrend_by_date = _daily_downtrend_flags(daily)
    bar_dates = pd.to_datetime(df["date"]).dt.date
    downtrend_ok = bar_dates.map(daily_downtrend_by_date).fillna(False).to_numpy()

    sell = (
        momentum_sell.fillna(False) & htf_sell & ltf_sell
        & vol_ok.fillna(False) & breakout_sell.fillna(False) & downtrend_ok
    )
    if bool(getattr(config, "SMART_MONEY_REQUIRE_STRUCTURE", False)):
        sell = sell & structure_sell

    # a signal on the last bar of its own session has nowhere to exit intraday
    bar_date = df["date"].dt.date
    is_last_bar_of_day = bar_date.ne(bar_date.shift(-1))
    sell = sell & (~is_last_bar_of_day)

    out["atr"] = atr_s
    out["signal"] = sell.fillna(False)
    return out


def simulate_intraday_short_trades(sig_df: pd.DataFrame, symbol: str) -> list:
    """Next-bar-open entry, ATR stop/target, forced square-off at session
    close if neither is hit — never carried past the trading day."""
    sig_df = sig_df.reset_index(drop=True)
    n = len(sig_df)
    dates = pd.to_datetime(sig_df["date"]).dt.date
    sl_mult = config.SMART_MONEY_SL_ATR_MULT
    tp_mult = config.SMART_MONEY_TP_ATR_MULT

    trades = []
    in_position_until = -1
    for i in sig_df.index[sig_df["signal"]].tolist():
        if i <= in_position_until:
            continue  # already holding a short on this symbol
        entry_i = i + 1
        if entry_i >= n or dates.iloc[entry_i] != dates.iloc[i]:
            continue  # no next bar, or it rolls into the next session

        atr_val = sig_df.loc[i, "atr"]
        if pd.isna(atr_val) or atr_val <= 0:
            continue
        atr_val = float(atr_val)

        entry_price = float(sig_df.loc[entry_i, "open"])
        stop_price = entry_price + sl_mult * atr_val
        target_price = entry_price - tp_mult * atr_val
        day = dates.iloc[entry_i]

        exit_price = exit_time = exit_reason = None
        j = entry_i
        while j < n and dates.iloc[j] == day:
            row = sig_df.loc[j]
            if row["high"] >= stop_price:
                exit_price, exit_time, exit_reason = stop_price, row["date"], "stop_loss"
                break
            if row["low"] <= target_price:
                exit_price, exit_time, exit_reason = target_price, row["date"], "target_hit"
                break
            j += 1

        if exit_price is None:
            last_j = j - 1 if j > entry_i else entry_i
            row = sig_df.loc[last_j]
            exit_price, exit_time, exit_reason = float(row["close"]), row["date"], "eod_square_off"
            j = last_j

        pnl_pct = (entry_price - exit_price) / entry_price * 100  # short: price falling = profit
        stop_pct = (stop_price - entry_price) / entry_price * 100  # risk %, always positive (short)
        risk_reward = round(tp_mult / sl_mult, 2)
        trades.append({
            "symbol": symbol,
            "signal_time": str(sig_df.loc[i, "date"]),
            "entry_time": str(sig_df.loc[entry_i, "date"]),
            "entry_price": round(entry_price, 2),
            "stop_loss": round(stop_price, 2),
            "target": round(target_price, 2),
            "stop_pct": round(stop_pct, 2),
            "risk_reward": risk_reward,
            "exit_time": str(exit_time),
            "exit_price": round(exit_price, 2),
            "pnl_pct": round(pnl_pct, 2),
            "exit_reason": exit_reason,
            "atr": round(atr_val, 2),
        })
        in_position_until = j

    return trades


def summarize_short(trades_df: pd.DataFrame) -> dict:
    if trades_df.empty:
        return {"total_trades": 0}
    return {
        "total_trades": len(trades_df),
        "win_rate_pct": round((trades_df["pnl_pct"] > 0).mean() * 100, 1),
        "avg_pnl_pct": round(trades_df["pnl_pct"].mean(), 2),
        "median_pnl_pct": round(trades_df["pnl_pct"].median(), 2),
        "target_hit_count": int((trades_df["exit_reason"] == "target_hit").sum()),
        "stop_loss_count": int((trades_df["exit_reason"] == "stop_loss").sum()),
        "eod_square_off_count": int((trades_df["exit_reason"] == "eod_square_off").sum()),
        "target_hit_rate_pct": round((trades_df["exit_reason"] == "target_hit").mean() * 100, 1),
        "stop_loss_rate_pct": round((trades_df["exit_reason"] == "stop_loss").mean() * 100, 1),
        "eod_square_off_rate_pct": round((trades_df["exit_reason"] == "eod_square_off").mean() * 100, 1),
    }


def _infer_pe_contract_specs(kite_client, symbol: str) -> dict:
    """Nearest-expiry PE strike spacing + lot size for this F&O stock, read
    from the CURRENTLY-listed NFO instrument dump. Strike steps and lot
    sizes are set by the exchange and rarely change, so using today's is a
    reasonable stand-in for a historical trade's real contract specs even
    though Kite's instrument list can't be queried for already-expired
    contracts directly."""
    out = {"strike_step": None, "lot_size": None}
    try:
        nfo = kite_client.get_nfo_instruments()
        opts = nfo[(nfo["name"] == symbol) & (nfo["segment"] == "NFO-OPT") & (nfo["instrument_type"] == "PE")]
        if opts.empty:
            return out
        opts = opts.copy()
        opts["expiry"] = pd.to_datetime(opts["expiry"])
        nearest_expiry = opts["expiry"].min()
        nearest = opts[opts["expiry"] == nearest_expiry]
        strikes = sorted(nearest["strike"].unique())
        if len(strikes) >= 2:
            diffs = [round(strikes[i + 1] - strikes[i], 2) for i in range(len(strikes) - 1)]
            # modal step (most common gap) — a few indices list a handful of
            # irregular far-from-spot strikes, so mode is more robust than min/mean.
            out["strike_step"] = float(pd.Series(diffs).mode().iloc[0])
        if "lot_size" in nearest.columns and not nearest.empty:
            out["lot_size"] = int(nearest["lot_size"].mode().iloc[0])
        return out
    except Exception:
        return out


def _realized_annualized_vol(daily: pd.DataFrame, asof_date, lookback: int = 20) -> Optional[float]:
    """Trailing N-day realized volatility of the underlying's daily closes,
    annualized — used as the implied-vol stand-in for the option premium
    model below (no historical IV/option-chain data is available, so this
    is the best proxy from data we already have)."""
    try:
        d = daily.copy()
        d["date"] = pd.to_datetime(d["date"]).dt.date
        d = d[d["date"] <= pd.Timestamp(asof_date).date()].sort_values("date")
        closes = d["close"].astype(float).tail(lookback + 1)
        if len(closes) < 5:
            return None
        log_ret = np.log(closes / closes.shift(1)).dropna()
        if log_ret.empty:
            return None
        vol = float(log_ret.std() * math.sqrt(252))
        if not np.isfinite(vol) or vol <= 0:
            return None
        return min(max(vol, OPTION_MIN_IV), OPTION_MAX_IV)
    except Exception:
        return None


def _bs_put_price(spot: float, strike: float, years_to_expiry: float, vol: float,
                   r: float = OPTION_RISK_FREE_RATE) -> float:
    """Black-Scholes European put premium. Falls back to intrinsic value
    once there's effectively no time left (or inputs are degenerate) —
    this is an APPROXIMATION standing in for a real historical option
    quote; see run_intraday_sell_backtest()'s docstring for why one isn't
    available."""
    intrinsic = max(strike - spot, 0.0)
    if years_to_expiry <= 1e-6 or vol <= 0 or spot <= 0:
        return intrinsic
    sqrt_t = math.sqrt(years_to_expiry)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * years_to_expiry) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    price = strike * math.exp(-r * years_to_expiry) * n(-d2) - spot * n(-d1)
    return max(price, intrinsic, 0.0)


def _month_last_thursday(year: int, month: int) -> pd.Timestamp:
    """Approximates NSE's monthly F&O expiry (last Thursday of the month) —
    ignores exchange-holiday shifts, which is fine for an illustrative
    time-to-expiry input."""
    next_month = pd.Timestamp(year=year, month=month, day=28) + pd.Timedelta(days=4)
    last_day = next_month - pd.Timedelta(days=next_month.day)
    offset = (last_day.weekday() - 3) % 7  # Thursday == 3
    return last_day - pd.Timedelta(days=offset)


def _build_option_trade_fields(
    symbol: str, trade: dict, specs: dict, vol: Optional[float],
) -> dict:
    """Turns one equity-level SELL trade into its PE-contract equivalent:
    an illustrative strike/expiry, a lot-size quantity, and Black-Scholes
    premium estimates (using trailing realized vol as the IV stand-in) for
    entry/stop/target/exit in place of the underlying's own price levels.

    This is a MODEL, not a real historical option quote — Kite's
    instrument list only carries currently-active F&O contracts, so an
    already-expired contract's true past premium can't be looked up. The
    strike and lot size come from today's listed contract specs (these
    rarely change); the premium at each price point is priced off that
    spot level with the same assumed vol/expiry, so it moves the right
    direction and by a plausible amount, but it is not what the option
    actually traded at."""
    entry_price = trade["entry_price"]
    strike_step = specs.get("strike_step")
    if strike_step and strike_step > 0:
        strike = round(entry_price / strike_step) * strike_step
    else:
        strike = round(entry_price / 10) * 10  # coarse fallback when no step could be inferred

    entry_time = pd.Timestamp(trade["entry_time"])
    exit_time = pd.Timestamp(trade["exit_time"])
    expiry = _month_last_thursday(entry_time.year, entry_time.month)
    if expiry < entry_time:
        nxt = entry_time + pd.DateOffset(months=1)
        expiry = _month_last_thursday(nxt.year, nxt.month)
    expiry_label = expiry.strftime("%y%b").upper()

    iv = vol if vol is not None else OPTION_FALLBACK_IV
    years_entry = max((expiry - entry_time).total_seconds(), 0.0) / (365.0 * 86400)
    years_exit = max((expiry - exit_time).total_seconds(), 0.0) / (365.0 * 86400)

    entry_premium = _bs_put_price(entry_price, strike, years_entry, iv)
    stop_premium = _bs_put_price(trade["stop_loss"], strike, years_entry, iv)
    target_premium = _bs_put_price(trade["target"], strike, years_entry, iv)
    exit_premium = _bs_put_price(trade["exit_price"], strike, years_exit, iv)

    premium_pnl_pct = ((exit_premium - entry_premium) / entry_premium * 100) if entry_premium else 0.0
    premium_stop_pct = ((entry_premium - stop_premium) / entry_premium * 100) if entry_premium else 0.0
    qty = specs.get("lot_size") or 1

    strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
    return {
        "option_symbol": f"{symbol}{expiry_label}{strike_str}PE",
        "option_strike": float(strike),
        "option_expiry": str(expiry.date()),
        "option_qty": int(qty),
        "option_iv_pct": round(iv * 100, 1),
        "entry_premium": round(entry_premium, 2),
        "stop_premium": round(stop_premium, 2),
        "target_premium": round(target_premium, 2),
        "exit_premium": round(exit_premium, 2),
        "premium_pnl_pct": round(premium_pnl_pct, 2),
        "premium_stop_pct": round(premium_stop_pct, 2),
    }


def run_intraday_sell_backtest(
    kite_client,
    months: Optional[int] = None,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> dict:
    """Runs the backtest over an explicit [start_dt, end_dt] window when
    given one (used by the report layer's day-by-day monitoring: current
    month on first run, then just the days since the last cached trade on
    every later run). Falls back to the old trailing `months` window
    (default config.STOCK_FOR_DAY_BACKTEST_MONTHS) when neither date is
    given — scanning the whole F&O universe at 5-min resolution over 3
    months in one go is slow, so this mode is really just for the CLI's
    --months flag now, not the live dashboard."""
    end_dt = end_dt or datetime.now()
    if start_dt is None:
        months = months if months is not None else config.STOCK_FOR_DAY_BACKTEST_MONTHS
        start_dt = end_dt - relativedelta(months=months)

    scan_df = universe_mod.build_nifty_index_universe(kite_client, "fno")

    # 400 calendar days of daily warmup so the 50-EMA downtrend gate and the
    # HTF trend have something to stand on at the start of the window.
    daily_from = (start_dt - timedelta(days=400)).date()
    daily_to = end_dt.date()

    all_trades = []
    window_label = f"{months}m" if months is not None else "explicit window"
    print(f"Intraday SELL backtest: F&O universe ({len(scan_df)} stocks), "
          f"{start_dt.date()} -> {end_dt.date()} ({window_label})...")

    for _, row in tqdm(scan_df.iterrows(), total=len(scan_df), desc="SM intraday SELL (F&O)"):
        symbol = row["tradingsymbol"]
        token = row["instrument_token"]
        try:
            daily = kite_client.get_daily_history(token, symbol, daily_from, daily_to)
            if daily.empty or len(daily) < 60:
                continue
            sig5 = _fetch_intraday_chunked(
                kite_client, token, symbol, config.SMART_MONEY_SIGNAL_INTERVAL, start_dt, end_dt
            )
            if sig5.empty or len(sig5) < 60:
                continue
            htf15 = _fetch_intraday_chunked(
                kite_client, token, symbol, config.SMART_MONEY_HTF_INTERVAL, start_dt, end_dt
            )
            sig_df = build_intraday_sell_signal_frame(sig5, htf15, daily)
            trades = simulate_intraday_short_trades(sig_df, symbol)
            if trades:
                specs = _infer_pe_contract_specs(kite_client, symbol)
                for t in trades:
                    vol = _realized_annualized_vol(daily, t["entry_time"])
                    t.update(_build_option_trade_fields(symbol, t, specs, vol))
            all_trades.extend(trades)
        except Exception as e:
            print(f"  [warn] intraday SELL backtest skipped {symbol}: {e}")
            continue

    trades_df = pd.DataFrame(all_trades)
    summary = summarize_short(trades_df)
    trades = [] if trades_df.empty else trades_df.to_dict(orient="records")
    if trades:
        trades.sort(key=lambda t: t["signal_time"], reverse=True)

    return {
        "window_start": str(start_dt.date()),
        "window_end": str(end_dt.date()),
        "months": months,
        "universe_mode": "fno",
        "universe_size": int(len(scan_df)),
        "signal_interval": config.SMART_MONEY_SIGNAL_INTERVAL,
        "htf_interval": config.SMART_MONEY_HTF_INTERVAL,
        "downtrend_filter": (
            f"daily close < daily EMA{getattr(config, 'SMART_MONEY_DAILY_DOWNTREND_EMA', 50)} "
            "(as of prior close) AND live HTF(15m)/LTF(5m) bearish trend"
        ),
        "option_symbol_note": (
            "Each row's option_symbol, strike, qty (lot size) and premium (entry/stop/target/exit) "
            "are MODELED, not real historical option quotes: Kite's instrument list only carries "
            "currently-active F&O contracts, so an already-expired contract's true past premium "
            "can't be looked up. The strike and lot size come from today's listed contract specs "
            "(these rarely change); each premium is priced with Black-Scholes off the underlying's "
            "real price at that point, using that stock's own trailing 20-day realized volatility "
            "as the implied-vol stand-in and the month's approximate last-Thursday expiry. Use "
            "these as a plausible illustration of the trade's option P&L, not an exact fill price."
        ),
        "summary": summary,
        "trades": trades,
    }


if __name__ == "__main__":
    import argparse

    from kite_auth import get_kite_session
    from kite_client import KiteDataClient

    parser = argparse.ArgumentParser(
        description="Intraday SELL-signal backtest (F&O universe, hard downtrend gate)"
    )
    parser.add_argument("--months", type=int, default=config.STOCK_FOR_DAY_BACKTEST_MONTHS)
    args = parser.parse_args()

    kite = get_kite_session()
    client = KiteDataClient(kite)
    result = run_intraday_sell_backtest(client, months=args.months)

    print("\n" + "=" * 60)
    print(json.dumps(result["summary"], indent=2))
    print(f"Window: {result['window_start']} -> {result['window_end']}  "
          f"|  F&O universe: {result['universe_size']} stocks  "
          f"|  Downtrend filter: {result['downtrend_filter']}")
    print("=" * 60)
