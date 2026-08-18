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
UNIVERSE_MODE = os.environ.get("UNIVERSE_MODE", "nifty500")     # "nifty50" | "nifty100" | "nifty200" | "nifty500" | "fno" | "all" | "file"
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
SMART_MONEY_PRE_MOMENTUM_FACTOR = 0.5     # Pine pre_momentum_factor_base for READY
SMART_MONEY_STRUCTURE_LOOKBACK = 3        # CHoCH/BOS lookback for structure labels
SMART_MONEY_REQUIRE_STRUCTURE = False     # Pine: BUY/SELL gates do not require CHoCH/BOS
SMART_MONEY_SHOW_GET_READY = True         # surface READY when near full signal (Pine get_ready)
SMART_MONEY_HTF_INTERVAL = "15minute"     # higher-TF trend filter
SMART_MONEY_LTF_INTERVAL = "5minute"      # lower-TF alignment filter
SMART_MONEY_SL_ATR_MULT = 1.5             # stop distance = ATR(14) * mult
SMART_MONEY_TP_ATR_MULT = 3.0             # target distance = ATR(14) * mult
SMART_MONEY_SCAN_INTERVAL_SEC = 300       # CLI loop delay between full scans
SMART_MONEY_SEND_TELEGRAM = True

# Stock for day — Smart Money BUY/SELL screen on a focused index list
STOCK_FOR_DAY_UNIVERSE = "nifty100"       # "nifty50" | "nifty100" | "nifty200" | "nifty500"
STOCK_FOR_DAY_LOOKBACK_DAYS = 400         # daily history (patterns need multi-month swings)
STOCK_FOR_DAY_SEND_TELEGRAM = False       # list in UI by default; opt-in alerts
STOCK_FOR_DAY_BACKTEST_MONTHS = 3         # success-rate window for Stock for day backtest
STOCK_FOR_DAY_INCLUDE_SELL = True         # show Pine-style SELL labels alongside BUY

# Previous Support Bounce — trendlines, bottoms, wedges, triangles, flags, cup&handle
SUPPORT_BOUNCE_UNIVERSE = "nifty100"      # "nifty50" | "nifty100" | "nifty200" | "nifty500"
SUPPORT_BOUNCE_LOOKBACK_DAYS = 400        # need multi-month swing history (chart-style)
SUPPORT_BOUNCE_PIVOT_LENGTH = 5           # short swing pivot
SUPPORT_BOUNCE_MAJOR_PIVOT_LENGTH = 12    # major swing lows for trendlines
SUPPORT_BOUNCE_TOUCH_PCT = 2.0            # % band around support / trendline
SUPPORT_BOUNCE_ATR_TOUCH_MULT = 0.6       # also allow ATR-scaled touch band
SUPPORT_BOUNCE_MAX_DIST_PCT = 4.0         # max distance above support if only touching
SUPPORT_BOUNCE_MAX_BOUNCE_DIST_PCT = 7.0  # after a bounce, allow only a little extension
                                            # (was 12.0 — too generous, let already-moved
                                            # stocks through; tightened to keep results
                                            # near the support/buy zone, not post-move)
SUPPORT_BOUNCE_BOUNCE_BARS = 35           # lookback for probe + bounce (~5-7 weeks)
SUPPORT_BOUNCE_MIN_TRENDLINE_SEP = 25     # min bars between trendline anchors
SUPPORT_BOUNCE_MAX_PIVOTS_FOR_LINE = 10   # recent swing lows considered for lines
SUPPORT_BOUNCE_MIN_HORIZONTAL_TOUCHES = 2
SUPPORT_BOUNCE_BREAK_CLOSES = 3           # closes below support => line invalidated
SUPPORT_BOUNCE_MIN_SCORE = 50             # soft buy-zone score floor (0-100)
SUPPORT_BOUNCE_REQUIRE_SM_BUY = False     # True = only full Smart Money BUY signals
SUPPORT_BOUNCE_WEDGE_MIN_SEP = 20         # min bars between wedge anchors
SUPPORT_BOUNCE_WEDGE_MAX_PIVOTS = 8
SUPPORT_BOUNCE_BOTTOM_TOL_PCT = 2.0       # bottoms must match within this %
SUPPORT_BOUNCE_BOTTOM_MIN_SEP = 15        # min bars between consecutive bottoms
SUPPORT_BOUNCE_BOTTOM_MAX_UP_PCT = 5.0    # only freshly bounced (≤5% above bottoms)
SUPPORT_BOUNCE_BOTTOM_MAX_PIVOTS = 10     # recent swing lows scanned for W/triple
SUPPORT_BOUNCE_PATTERN_MIN_SEP = 18       # min bars between triangle/flag anchors
SUPPORT_BOUNCE_PATTERN_MAX_PIVOTS = 6
SUPPORT_BOUNCE_TRIANGLE_FLAT_SLOPE = 0.00012  # "flat" side of ascending/descending triangle
SUPPORT_BOUNCE_FLAG_POLE_LOOKBACK = 60
SUPPORT_BOUNCE_FLAG_POLE_MIN_PCT = 8.0    # min prior rally to qualify as pole
SUPPORT_BOUNCE_FLAG_MIN_SEP = 8
SUPPORT_BOUNCE_FLAG_MAX_BARS = 40         # max consolidation length after pole
SUPPORT_BOUNCE_CUP_MIN_BARS = 40
SUPPORT_BOUNCE_CUP_MAX_BARS = 160
SUPPORT_BOUNCE_CUP_MAX_UP_PCT = 8.0       # handle bounce still near handle low

# Breakout-pattern quality (triangles/wedges/flags/pennants breaking above
# resistance): require either a re-test of the broken level, or that price
# hasn't run too far without one — avoids surfacing stocks that already
# moved away with no re-test to anchor a stop against. Also rewards a
# high-volume, strong-close breakout bar.
SUPPORT_BOUNCE_VOLUME_SMA_PERIOD = 20
SUPPORT_BOUNCE_MAX_BREAKOUT_DIST_PCT = 6.0    # exclude un-retested breakouts extended past this
                                                # (was 15.0 — let stocks that already ran 15%
                                                # past resistance through; this is the main
                                                # source of "already moved" results)
SUPPORT_BOUNCE_RETEST_MAX_BARS = 25           # bars allowed for a re-test after breakout (~5 weeks)
SUPPORT_BOUNCE_RETEST_BAND_PCT = 3.0          # % band around the broken level for a re-test
SUPPORT_BOUNCE_MIN_CLOSE_STRENGTH = 0.6       # close in the top 40% of the bar's range = "strong"
SUPPORT_BOUNCE_VOLUME_SPIKE_MULT = 1.5        # breakout volume >= this x SMA counts as "high volume"

# Final buy-zone gate — applied uniformly to EVERY result (pattern-based buy
# zone AND raw Pine Smart Money BUY/SELL alike). Pine's BUY signal alone has
# no notion of "distance from previous support", so without this a stock
# already 20%+ past its support could still surface just because Pine fired
# BUY on unrelated momentum grounds — this closes that loophole.
SUPPORT_BOUNCE_BUY_ZONE_MAX_PCT = 7.0          # current price must be within this % of support
SUPPORT_BOUNCE_PROXIMITY_PENALTY_MULT = 2.5    # score points lost per % away from support
SUPPORT_BOUNCE_REQUIRE_NON_BEARISH_TREND = True  # hard-gate out htf_trend == bearish (-1)

# ---------------------------------------------------------------------------
# Swing Trade (Weekly) — resistance breakout (descending trendline or
# horizontal box) confirmed by a volume spike, mirrors Previous Support
# Bounce's trendline/horizontal detection but flipped to breakout-above and
# run on weekly candles instead of daily.
# ---------------------------------------------------------------------------
SWING_TRADE_UNIVERSE = "nifty200"         # "nifty50" | "nifty100" | "nifty200" | "nifty500"
SWING_TRADE_LOOKBACK_DAYS = 750           # ~3y daily -> enough weekly bars for long bases
SWING_TRADE_PIVOT_LENGTH = 3              # weekly swing pivot (bars each side)
SWING_TRADE_MAJOR_PIVOT_LENGTH = 6
SWING_TRADE_MIN_TREND_SEP = 8             # min weeks between trendline anchors
SWING_TRADE_MAX_PIVOTS_FOR_LINE = 8       # recent swing highs considered for trendlines
SWING_TRADE_MIN_HORIZONTAL_TOUCHES = 2
SWING_TRADE_TOUCH_PCT = 2.5               # % band around resistance / trendline
SWING_TRADE_ATR_TOUCH_MULT = 0.6          # also allow ATR-scaled touch band
SWING_TRADE_BREAKOUT_LOOKBACK_WEEKS = 6   # recent weekly bars scanned for a breakout candle
SWING_TRADE_VOLUME_SMA_WEEKS = 20
SWING_TRADE_VOLUME_MULT = 2.0             # breakout week volume >= this x the SMA (hard gate)
SWING_TRADE_BREAKOUT_BUFFER_PCT = 0.5     # close must clear resistance by this % to count
SWING_TRADE_RETEST_MAX_WEEKS = 6          # re-test must land within N weeks of the breakout
SWING_TRADE_RETEST_BAND_PCT = 3.0         # % band around old resistance for a re-test
SWING_TRADE_TRIGGER_MAX_WEEKS = 8         # recency cap — stale breakouts drop off the list
SWING_TRADE_EMA_FAST = 10                 # informational only, never gates a result
SWING_TRADE_EMA_SLOW = 20
SWING_TRADE_SL_ATR_MULT = 1.5             # stop distance = weekly ATR(14) * mult
SWING_TRADE_TP_ATR_MULT = 3.0             # target distance = weekly ATR(14) * mult
SWING_TRADE_MIN_SCORE = 45                # soft quality-score floor (0-100) — CANDIDATE/TRIGGERED
SWING_TRADE_WATCH_BAND_PCT = 6.0          # % below (or just at) resistance still counted as "pressing"
SWING_TRADE_MIN_SCORE_WATCHING = 25       # lower floor — WATCHING rows have no breakout/volume bonus yet
SWING_TRADE_MAX_EXTENSION_PCT = 15.0      # drop TRIGGERED rows already this far past the breakout high
SWING_TRADE_SEND_TELEGRAM = True

# ---------------------------------------------------------------------------
# My Trades tracking — whether scanners auto-add every signal they produce
# (darvax breakouts, support_bounce BUYs, swing_trade TRIGGERED rows, stock
# for day BUYs) to My Trades, versus only tracking what you explicitly click
# "☆ Track" on. Off by default: My Trades should reflect trades YOU chose to
# track, not every signal the scanners happened to produce.
AUTO_TRACK_ENABLED = False

# ---------------------------------------------------------------------------
# DarvaX — proper Darvas Box construction (Nicolas Darvas's 3-session-stall
# top/bottom rule) with Amitabh Jha's "DarvaX" Indian-market overlay: bias
# toward stocks near their all-time high, tiered EMA stop-loss reference,
# never average down. Runs on DAILY candles (the box mechanic + "enter above
# previous day high" trigger are both daily-native, unlike Swing Trade's
# weekly-only resistance breakout).
# ---------------------------------------------------------------------------
DARVAX_UNIVERSE = "nifty200"          # "nifty50" | "nifty100" | "nifty200" | "nifty500"
DARVAX_LOOKBACK_DAYS = 400            # enough daily history for box formation + ATH context
DARVAX_CONFIRM_BARS = 3               # Darvas's "3 sessions without a new high/low"
DARVAX_BREAKOUT_LOOKBACK_DAYS = 20    # surface boxes that broke out in the last N sessions
                                        # (was 5 — too tight: real examples like MUFIN/EMMVEE/
                                        # CENTUM had valid, still-un-invalidated breakouts 17-56
                                        # bars old that got excluded purely on recency, not quality)
DARVAX_VOLUME_SMA_DAYS = 20
DARVAX_VOLUME_MULT = 1.5              # "surge in volume" gate on the breakout bar
DARVAX_MAX_BOX_AGE_DAYS = 90          # ignore boxes that took absurdly long to form
DARVAX_EMA_TIERS = {                  # p.7 of the deck: SL reference by holding style
    "very_short_term": 5,
    "swing": 10,
    "positional": 20,
    "investor": 200,
}
DARVAX_MIN_SCORE = 30                 # soft quality-score floor (0-100)

# Weekly variant — same box state machine, run on weekly-resampled candles.
# The deck explicitly prefers this over daily ("the higher the timeframe,
# the higher the respect") since a weekly 3-bar stall represents 3 WEEKS of
# real consolidation, not 3 days — far fewer, much higher-conviction boxes.
DARVAX_WEEKLY_LOOKBACK_DAYS = 900          # ~3.5y daily -> enough weekly bars for long bases
DARVAX_WEEKLY_CONFIRM_BARS = 3             # same 3-session-stall rule, on weekly bars
DARVAX_WEEKLY_BREAKOUT_LOOKBACK_WEEKS = 10  # surface boxes that broke out in the last N weeks
                                              # (was 4 — same recency issue as the daily lookback)
DARVAX_WEEKLY_VOLUME_SMA_WEEKS = 20
DARVAX_WEEKLY_VOLUME_MULT = 1.5
DARVAX_WEEKLY_MAX_BOX_AGE_WEEKS = 26       # ~6 months
DARVAX_WEEKLY_MIN_SCORE = 30

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

# TradingView chart links (opened from the dashboard / Telegram)
# interval: "D" = daily (matches most scanners). Use "5" / "15" for intraday.
# Optional TRADINGVIEW_CHART_ID = your saved layout id from the TV URL
#   https://in.tradingview.com/chart/<THIS_ID>/
# so zoom, indicators, and theme from that layout are reused.
TRADINGVIEW_BASE_URL = "https://in.tradingview.com/chart/"
TRADINGVIEW_INTERVAL = "D"
# Leave empty for a public chart link (works without owning a layout).
# Set only if the layout is yours and you'll stay logged into TradingView,
# or if you've enabled sharing / published it.
TRADINGVIEW_CHART_ID = ""

