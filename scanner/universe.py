"""
Builds the list of (instrument_token, tradingsymbol) to scan, and applies
the market-cap filter.

Kite has no fundamentals/market-cap endpoint. Priority order:
  1. data/market_cap.csv if present (columns: symbol,market_cap_cr) — you can
     export this from NSE's equity list or your broker/screener of choice.
  2. yfinance fallback (best-effort, slow, some symbols won't resolve to a
     valid NSE Yahoo ticker automatically).
Symbols with no market cap data available are EXCLUDED rather than silently
let through, so the filter is never accidentally skipped.
"""
import os
from typing import Optional

import pandas as pd
import requests

import config

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY_INDEX_URLS = {
    "nifty50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty500": NIFTY500_URL,
}
NIFTY_INDEX_LABELS = {
    "nifty50": "Nifty 50",
    "nifty100": "Nifty 100",
    "nifty200": "Nifty 200",
    "nifty500": "Nifty 500",
}


def normalize_nifty_mode(mode: Optional[str], default: str = "nifty100") -> str:
    mode = (mode or default).strip().lower()
    if mode not in NIFTY_INDEX_URLS:
        raise ValueError(
            f"Unknown nifty universe mode: {mode!r}. "
            f"Use one of: {', '.join(NIFTY_INDEX_URLS)}"
        )
    return mode


def nifty_mode_label(mode: str) -> str:
    return NIFTY_INDEX_LABELS.get(mode, mode)


def load_market_cap_csv() -> dict:
    path = config.MARKET_CAP_CSV
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    return dict(zip(df["symbol"].str.upper(), df["market_cap_cr"]))


def fetch_market_cap_yfinance(symbols: list) -> dict:
    """Fetches market cap for each symbol in parallel (yfinance has no bulk
    market-cap endpoint, but per-symbol calls are I/O-bound so threading
    gives a large speedup over sequential calls — ~9500 symbols sequentially
    can take hours; threaded, it's typically minutes)."""
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from tqdm import tqdm

    errors = []

    def _fetch_one(sym):
        try:
            t = yf.Ticker(f"{sym}.NS")
            fi = t.fast_info
            # fast_info supports both dict-style and attribute-style access
            # depending on yfinance version — try both, and try the camelCase
            # key too since that's what yfinance actually uses internally.
            mc = None
            for accessor in (
                lambda: fi.get("market_cap"),
                lambda: fi.get("marketCap"),
                lambda: fi["marketCap"],
                lambda: fi.market_cap,
            ):
                try:
                    mc = accessor()
                    if mc:
                        break
                except Exception:
                    continue
            if mc:
                return sym, mc / 1e7, None  # rupees -> crores
            return sym, None, None
        except Exception as e:
            return sym, None, f"{type(e).__name__}: {e}"

    out = {}
    max_workers = 20
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, sym): sym for sym in symbols}
        for future in tqdm(as_completed(futures), total=len(futures),
                            desc="Fetching market cap (yfinance, parallel)"):
            try:
                sym, mc, err = future.result(timeout=15)
                if mc:
                    out[sym] = mc
                elif err and len(errors) < 5:
                    errors.append((sym, err))
            except Exception as e:
                if len(errors) < 5:
                    errors.append((futures[future], f"{type(e).__name__}: {e}"))

    if errors:
        print(f"\n[debug] {len(out)}/{len(symbols)} succeeded. Sample errors "
              f"from the {len(symbols) - len(out)} that failed:")
        for sym, err in errors:
            print(f"  {sym}: {err}")

    return out


def build_nifty_index_universe(kite_client, mode: str = "nifty100") -> pd.DataFrame:
    """NSE index constituent list matched to Kite equity instruments.
    mode: nifty50 | nifty100 | nifty200 | nifty500"""
    mode = normalize_nifty_mode(mode)
    url = NIFTY_INDEX_URLS[mode]

    cache_file = os.path.join(config.CACHE_DIR, f"{mode}_constituents.csv")
    import time
    if os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file)) / 3600 < 24:
        symbols_df = pd.read_csv(cache_file)
    else:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        with open(cache_file, "wb") as f:
            f.write(resp.content)
        symbols_df = pd.read_csv(cache_file)

    symbols_df.columns = [c.strip() for c in symbols_df.columns]
    col = "Symbol" if "Symbol" in symbols_df.columns else symbols_df.columns[0]
    symbols = symbols_df[col].astype(str).str.upper().tolist()

    instruments = kite_client.get_nse_equity_instruments()
    instruments = instruments[instruments["tradingsymbol"].isin(symbols)].reset_index(drop=True)
    label = nifty_mode_label(mode)
    print(f"Universe: {label} list -> {len(instruments)} matched to tradeable "
          f"Kite instruments (of {len(symbols)} listed)")
    return instruments


def build_nifty500_universe(kite_client) -> pd.DataFrame:
    """Back-compat wrapper around build_nifty_index_universe('nifty500')."""
    return build_nifty_index_universe(kite_client, "nifty500")


def build_universe(kite_client) -> pd.DataFrame:
    """Returns DataFrame[instrument_token, tradingsymbol, (market_cap_cr)]
    depending on UNIVERSE_MODE:
      - "nifty50" / "nifty100" / "nifty200" / "nifty500": NSE index lists (fast, no mcap fetch)
      - "all": every NSE equity, filtered by MIN_MARKET_CAP_CR (slow)
      - "file": your own symbol list in UNIVERSE_FILE
    """
    if config.UNIVERSE_MODE in NIFTY_INDEX_URLS:
        return build_nifty_index_universe(kite_client, config.UNIVERSE_MODE)

    if config.UNIVERSE_MODE == "file":
        symbols = pd.read_csv(config.UNIVERSE_FILE, header=None)[0].str.upper().tolist()
        instruments = kite_client.get_nse_equity_instruments()
        instruments = instruments[instruments["tradingsymbol"].isin(symbols)]
    else:
        instruments = kite_client.get_nse_equity_instruments()

    mcap_map = load_market_cap_csv()
    missing = [s for s in instruments["tradingsymbol"] if s not in mcap_map]

    if missing and config.USE_YFINANCE_FALLBACK:
        print(f"Market cap missing for {len(missing)} symbols, trying yfinance fallback "
              f"(this is slow)...")
        mcap_map.update(fetch_market_cap_yfinance(missing))

    instruments["market_cap_cr"] = instruments["tradingsymbol"].map(mcap_map)
    before = len(instruments)
    instruments = instruments.dropna(subset=["market_cap_cr"])
    instruments = instruments[instruments["market_cap_cr"] > config.MIN_MARKET_CAP_CR]
    print(f"Universe: {before} NSE equities -> {len(instruments)} pass market cap "
          f"> {config.MIN_MARKET_CAP_CR} Cr (excluded {before - len(instruments)} "
          f"with no/insufficient market cap data)")

    if config.MAX_UNIVERSE_SIZE:
        instruments = instruments.head(config.MAX_UNIVERSE_SIZE)

    return instruments.reset_index(drop=True)
