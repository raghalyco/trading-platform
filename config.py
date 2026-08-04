"""
Central configuration. Tune everything here — nothing else in the codebase
should hardcode a threshold.
"""
import os
from datetime import date
from dateutil.relativedelta import relativedelta

# ---------------------------------------------------------------------------
# Kite Connect credentials (put real values in a local .env, never commit it)
# ---------------------------------------------------------------------------
KITE_API_KEY = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET", "")

# ---------------------------------------------------------------------------
# Backtest window — ALWAYS the latest N months ending today, recomputed
# fresh every time refresh_backtest_window() is called (backtest.py's
# run_backtest() does this automatically at the start of every run). Never
# hardcode dates here — a fixed range goes stale the moment "today" moves
# past it.
# ---------------------------------------------------------------------------
BACKTEST_MONTHS = 6


def refresh_backtest_window():
    global BACKTEST_START, BACKTEST_END
    BACKTEST_END = date.today()
    BACKTEST_START = BACKTEST_END - relativedelta(months=BACKTEST_MONTHS)
    return BACKTEST_START, BACKTEST_END


BACKTEST_START, BACKTEST_END = None, None
refresh_backtest_window()

# Extra daily history fetched BEFORE BACKTEST_START purely so EMA200 / weekly
# EMA20 / RSI / ADX have warmed up and aren't distorted at the start of the
# test window. 3 years is comfortably enough for daily EMA200 and weekly EMA20.
WARMUP_YEARS = 3

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
# "all" = every NSE cash-segment equity from the Kite instrument dump.
# For a first dry run you may want to cap this — scanning ~2000 symbols x
# ~4 years of daily candles through Kite's historical API (rate-limited to
# ~3 req/sec) takes hours. Set MAX_UNIVERSE_SIZE to e.g. 300 to test quickly.
UNIVERSE_MODE = "nifty100"     # "nifty50" | "nifty100" | "nifty500" | "all" | "file"
UNIVERSE_FILE = "data/universe.csv"   # used if UNIVERSE_MODE == "file", one symbol per line
MAX_UNIVERSE_SIZE = None       # e.g. 300 for a quick test run, None = no cap

# Market cap filter (Kite has no fundamentals endpoint — see README).
MARKET_CAP_CSV = "data/market_cap.csv"   # columns: symbol,market_cap_cr
MIN_MARKET_CAP_CR = 2000
USE_YFINANCE_FALLBACK = True   # slow, best-effort, only used if CSV missing/incomplete

# ---------------------------------------------------------------------------
# Scanner thresholds (mirrors the Chartink screen exactly)
# ---------------------------------------------------------------------------
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
WEEKLY_EMA = 20
BREAKOUT_LOOKBACK = 20          # Max(20, Daily High), 1 day ago
VOLUME_SMA_PERIOD = 20
VOLUME_MULTIPLIER = 2.5
CLOSE_NEAR_HIGH_PCT = 0.98      # Close >= 0.98 * High
RSI_PERIOD = 14
RSI_MIN = 60
RSI_MAX = 75
ADX_PERIOD = 14
ADX_MIN = 25
EXTENSION_CAP = 1.15            # Close < 1.15 * EMA20 (not too extended)
WEEKLY_RSI_MIN = 50

# ---------------------------------------------------------------------------
# Second scanner: "EMA10 Pullback" — Close > EMA(10) but not extended more
# than EMA10_MAX_DISTANCE_PCT above it (still close to the line, not already
# run away from it).
# ---------------------------------------------------------------------------
EMA10_PERIOD = 10
EMA10_MAX_DISTANCE_PCT = 4.0

# ---------------------------------------------------------------------------
# Third scanner: "N-Day BO" (breakout proximity) — mirrors TradeBrahma's
# Swing Spectrum: stocks trading within NDAY_PROXIMITY_PCT of their N-day
# high (bullish / "near high") or N-day low (bearish / "near low"), for
# each lookback in NDAY_LOOKBACKS.
# ---------------------------------------------------------------------------
NDAY_LOOKBACKS = [10, 50]
NDAY_PROXIMITY_PCT = 1.0

# ---------------------------------------------------------------------------
# Fourth scanner: Sector Analysis — ranks NSE sector indices by momentum and
# flags sector-level breakouts. Names are matched case-insensitively/
# substring against Kite's INDICES instrument dump (see kite_client.py),
# since exact Kite tradingsymbol formatting for indices can vary slightly
# and isn't worth hardcoding blind.
# ---------------------------------------------------------------------------
SECTOR_INDEX_NAMES = [
    "NIFTY AUTO", "NIFTY BANK", "NIFTY FMCG", "NIFTY IT", "NIFTY METAL",
    "NIFTY PHARMA", "NIFTY PSU BANK", "NIFTY PVT BANK", "NIFTY REALTY",
    "NIFTY FIN SERVICE", "NIFTY MEDIA", "NIFTY ENERGY", "NIFTY HEALTHCARE",
    "NIFTY CONSUMER DURABLES", "NIFTY OIL AND GAS",
]
SECTOR_EMA_PERIOD = 20
SECTOR_BREAKOUT_LOOKBACK = 20  # days, for "sector breakout" flag

# ---------------------------------------------------------------------------
# Trending Stocks by Sector — per-sector constituent lists (NSE's free
# archives, same pattern as the Nifty 500 list). URLs follow NSE's standard
# naming convention but a few of the less common sectors' exact filenames
# aren't 100% certain without live verification — sector_constituents.py
# fails gracefully (skips + warns) per sector rather than crashing if one
# doesn't resolve.
# ---------------------------------------------------------------------------
SECTOR_CONSTITUENT_URLS = {
    "NIFTY AUTO": "https://archives.nseindia.com/content/indices/ind_niftyautolist.csv",
    "NIFTY BANK": "https://archives.nseindia.com/content/indices/ind_niftybanklist.csv",
    "NIFTY FMCG": "https://archives.nseindia.com/content/indices/ind_niftyfmcglist.csv",
    "NIFTY IT": "https://archives.nseindia.com/content/indices/ind_niftyitlist.csv",
    "NIFTY METAL": "https://archives.nseindia.com/content/indices/ind_niftymetallist.csv",
    "NIFTY PHARMA": "https://archives.nseindia.com/content/indices/ind_niftypharmalist.csv",
    "NIFTY PSU BANK": "https://archives.nseindia.com/content/indices/ind_niftypsubanklist.csv",
    "NIFTY PVT BANK": "https://archives.nseindia.com/content/indices/ind_nifty_privatebanklist.csv",
    "NIFTY REALTY": "https://archives.nseindia.com/content/indices/ind_niftyrealtylist.csv",
    "NIFTY FIN SERVICE": "https://archives.nseindia.com/content/indices/ind_niftyfinancelist.csv",
    "NIFTY MEDIA": "https://archives.nseindia.com/content/indices/ind_niftymedialist.csv",
    "NIFTY ENERGY": "https://archives.nseindia.com/content/indices/ind_niftyenergylist.csv",
    "NIFTY HEALTHCARE": "https://archives.nseindia.com/content/indices/ind_niftyhealthcarelist.csv",
    "NIFTY CONSUMER DURABLES": "https://archives.nseindia.com/content/indices/ind_niftyconsumerdurableslist.csv",
    "NIFTY OIL AND GAS": "https://archives.nseindia.com/content/indices/ind_niftyoilgaslist.csv",
}
TRENDING_STOCKS_PER_SECTOR = 6
TRENDING_LOOKBACK_DAYS = 60  # enough history for 1D % change (+ optional 5D field)

# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Intraday live monitor — opening-range breakout + volume surge + VWAP
# ---------------------------------------------------------------------------
INTRADAY_MARKET_OPEN = "09:15"
INTRADAY_MARKET_CLOSE = "15:30"
INTRADAY_OPENING_RANGE_MINUTES = 15      # first N minutes define the opening range
INTRADAY_VOLUME_SURGE_MULTIPLIER = 2.0   # current minute's volume vs opening-range avg minute volume
INTRADAY_SHORTLIST_MAX = 100             # cap on how many symbols to stream live (bandwidth/sanity)
# Move-size target is informational only (shown in the alert) — this module
# alerts on the setup, it does not manage an exit; 3-5% / 8% are just the
# language used in the alert message.
INTRADAY_MOVE_TARGET_LOW_PCT = 3.0
INTRADAY_MOVE_TARGET_HIGH_PCT = 5.0

# ---------------------------------------------------------------------------
# Smart Money pipeline — top trending sectors → sector leaders → Pine-style
# multi-TF / structure / momentum / volume / breakout signal → Telegram
# ---------------------------------------------------------------------------
SMART_MONEY_TOP_SECTORS = 5              # how many sector indices by today's (1D) momentum
SMART_MONEY_REQUIRE_SECTOR_ABOVE_EMA = True  # only sectors above their EMA20
SMART_MONEY_STOCKS_PER_SECTOR = 6        # top stocks by today's (1D) momentum per sector
SMART_MONEY_SIGNAL_INTERVAL = "5minute"  # chart TF for entries / structure
SMART_MONEY_HISTORY_DAYS = 10            # days of intraday history to fetch
SMART_MONEY_PIVOT_LENGTH = 5
SMART_MONEY_MOMENTUM_THRESHOLD_PCT = 0.01  # base % bar change (ATR-scaled)
SMART_MONEY_MIN_SIGNAL_DISTANCE = 5       # bars between signals (same symbol)
SMART_MONEY_VOLUME_LONG = 50
SMART_MONEY_VOLUME_SHORT = 5
SMART_MONEY_BREAKOUT_PERIOD = 5
SMART_MONEY_STRUCTURE_LOOKBACK = 3        # CHoCH/BOS must fire within last N bars
SMART_MONEY_HTF_INTERVAL = "15minute"     # higher-TF trend filter
SMART_MONEY_LTF_INTERVAL = "5minute"      # lower-TF alignment filter
SMART_MONEY_SL_ATR_MULT = 1.5             # stop distance = ATR(14) * mult
SMART_MONEY_TP_ATR_MULT = 3.0             # target distance = ATR(14) * mult
SMART_MONEY_SCAN_INTERVAL_SEC = 300       # CLI loop delay between full scans
SMART_MONEY_SEND_TELEGRAM = True

# Stock for day — Smart Money BUY screen on a focused index list
STOCK_FOR_DAY_UNIVERSE = "nifty100"       # "nifty50" | "nifty100" | "nifty500"
STOCK_FOR_DAY_LOOKBACK_DAYS = 200         # daily history warmup
STOCK_FOR_DAY_SEND_TELEGRAM = False       # list in UI by default; opt-in alerts
STOCK_FOR_DAY_BACKTEST_MONTHS = 3         # success-rate window for Stock for day backtest

# ---------------------------------------------------------------------------
# Trade simulation rules
# ---------------------------------------------------------------------------
ENTRY_ON = "next_open"          # enter at next trading day's open after signal
TARGET_MIN_PCT = 10.0            # informational floor — used to tag trades in results
TARGET_MAX_PCT = 15.0            # hard take-profit target
MAX_HOLDING_DAYS = 60            # force-exit (time stop) if target never hit
STOP_LOSS_PCT = None             # None = no stop loss (not specified in the screen);
                                  # set e.g. -8.0 to add one

# ---------------------------------------------------------------------------
# Kite API rate limiting / caching
# ---------------------------------------------------------------------------
REQUESTS_PER_SECOND = 2.5        # stay under Kite's ~3 req/sec historical-data limit
CACHE_DIR = "cache"
ACCESS_TOKEN_FILE = "cache/access_token.json"

RESULTS_DIR = "results"
