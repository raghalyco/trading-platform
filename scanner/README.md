# Kite Scanner Bot

A local trading-research web app built on Kite Connect: multiple daily
stock scanners, sector analysis, a 6-month backtester, and a live intraday
alert engine (WebSocket-based opening-range breakout monitor with Telegram
notifications). Everything runs locally on your machine; nothing is hosted
or auto-traded.

---

## 1. Setup — single command

```bash
cd kite_scanner_bot
python app.py
```

That's it for a fresh clone. `app.py` bootstraps itself on startup
(`bootstrap.py`): installs any missing pip packages, creates `.env` from
`.env.example`, and creates the `cache/`/`data/`/`results/` folders
automatically. The **only** thing it can't automate is your actual Kite
Connect API key/secret (a private credential only you have) — the very
first run will stop and tell you to fill those into `.env`, then you run
`python app.py` again. After that, it's genuinely just the one command
every time.

You'll need a **Kite Connect API subscription** — a *separate paid
product* from your regular Zerodha account (~₹2000+GST/month), from
https://developers.kite.trade. Regular Zerodha login does not give API
access.

Then open **http://127.0.0.1:5000** in your browser.

### Logging in

Login is manual (no stored password/TOTP, no browser automation — that
approach was tried and abandoned early on due to bot-detection issues).
On startup, the app prints a login URL:

```
Kite login required. Steps:
  1. Open this URL in your browser:
     https://kite.zerodha.com/connect/login?api_key=...&v=3
  2. Log in with your Zerodha user ID, password, and TOTP as usual.
  3. After login you'll be redirected to your app's redirect URL —
     copy either the request_token value or the whole URL.
```

Paste back the `request_token` (or the full redirect URL — either works).
It's single-use and expires within a couple minutes, so paste it promptly.
The resulting access token is cached in `cache/access_token.json` for the
rest of the day, so you won't be asked again until tomorrow.

---

## 2. The Web Dashboard — 7 tabs

Sidebar order (top to bottom):

### Sector Analysis *(default landing tab)*
Ranks NSE's actual sector indices (NIFTY BANK, NIFTY IT, NIFTY AUTO, etc.
— real Kite index instruments, not hand-averaged constituents) by 1-day /
5-day / 1-month momentum, and flags 20-day sector-level breakouts.
`sector_scanner.py` / `/api/scan_sector`.

### Trending Stocks by Sector
Card/block layout, one card per sector, showing its top 6 individual
stocks by **today's (1D) momentum** — same present-day ranking as Sector
Analysis. Sector cards are ordered by Sector Analysis 1D rank. Pulled from
NSE's free per-sector constituent CSVs (`sector_constituents.py`), matched
against your current universe. `trending_scanner.py` / `/api/scan_trending`.

*Caveat:* the per-sector constituent-list URLs follow NSE's standard
naming pattern but weren't all live-verified — if a sector's card is
empty, check the console for a `[warn] couldn't fetch constituents...`
line and fix the URL in `config.SECTOR_CONSTITUENT_URLS`.

### Momentum Breakout
The original 13-condition scanner (daily/weekly EMA trend alignment,
20-day breakout, volume surge, RSI 60-75, ADX>25, market cap floor).
`scanner.py` / `/api/scan`. This is the "flagship" scanner most other
features build on.

### EMA10 Pullback
Simpler scanner: `Close > EMA(10)` but not extended more than
`EMA10_MAX_DISTANCE_PCT` (default 4%) above it — i.e. trading just above
its short-term trend line, not yet run away from it. `ema10_scanner.py` /
`/api/scan_ema10`.

### N-Day Breakout
Mirrors a "Swing Spectrum"-style screener: two side-by-side panels (10-day
/ 50-day), each listing stocks within `NDAY_PROXIMITY_PCT` (default 1%) of
their rolling N-day high (bullish) or low (bearish).
`nday_scanner.py` / `/api/scan_nday?lookback=10|50`.

### Intraday Monitor
Live WebSocket-based alert engine — see **Section 4** below for the full
explanation. Start/Stop buttons, a live-updating watchlist table, and an
"Alerts fired today" feed, all inside the dashboard (`intraday_engine.py`).

### Performance / Backtest
Runs the Momentum Breakout scanner's rules across the **latest 6 months**
(always — see below) and shows win rate, average P&L, and a full trade
log. `backtest.py` / `/api/backtest`.

**Shared dashboard behavior:** every table column is click-to-sort. Each
tab only auto-refreshes (every 5 min) while it's the active tab, so you're
not burning Kite API calls on tabs you're not looking at.

---

## 3. Backtest details

- **Window is always rolling**, never a fixed date range —
  `config.refresh_backtest_window()` recomputes "today minus
  `BACKTEST_MONTHS` (default 6) to today" at the start of every single
  backtest run, so it never goes stale even if the server's been running
  for weeks.
- **Entry**: next trading day's open after the signal day.
- **Exit target**: 15% (`TARGET_MAX_PCT`); `TARGET_MIN_PCT=10` is tracked
  as a `hit_min_target` flag on every trade so you can filter either way.
- **Time stop**: 60 trading days (`MAX_HOLDING_DAYS`) if target never hit.
- **No stop-loss** by default (the original screen didn't define one) —
  set `STOP_LOSS_PCT` in `config.py` to add one.
- **One open position per symbol at a time** — a new signal while a trade
  is still open is skipped (matches how you'd actually trade alerts).
- **Market cap**: Kite has no fundamentals endpoint. Put a CSV at
  `data/market_cap.csv` (columns `symbol,market_cap_cr`) — falls back to a
  slow, parallelized `yfinance` lookup if you skip this.
- **Known bias to keep in mind**: the universe (Nifty 500 by default) is
  *today's* constituents, not what it looked like on each historical date
  — this can inflate the backtest's win rate somewhat (survivorship bias),
  and isn't really fixable without a paid historical-constituent dataset.

**Verifying the numbers yourself:** `python verify_backtest.py
results/trades_<timestamp>.csv` independently recomputes win rate from
scratch (not reusing `backtest.py`'s own math) and flags real logic bugs —
impossible date ordering, overlapping trades, mismatched flags. For a
single symbol/date, `python explain_signal.py SYMBOL YYYY-MM-DD --ema10`
prints every one of the 13 rules with the actual numbers, pass/fail, for
that specific day.

---

## 4. Intraday Monitor (opening-range breakout + volume surge + VWAP)

A live, WebSocket-based engine — genuinely different from the EOD
scanners above (long-running/streaming vs. request-response). Runs either
embedded in the dashboard (**Intraday Monitor** tab, click Start/Stop) or
standalone via `python intraday_monitor.py` (same underlying engine,
`intraday_engine.py`, for anyone who'd rather run it outside the web app,
e.g. on a headless server).

### How it works
1. **Shortlist** (`shortlist.py`): built from the union of two sources —
   **Momentum Breakout** scanner hits and **Trending Stocks by Sector**
   leaders — capped at `INTRADAY_SHORTLIST_MAX` (default 100). Streaming
   the entire NSE universe live isn't practical for a solo setup, so this
   narrows to a high-conviction subset first. Each symbol is tagged with
   *which* source(s) flagged it — visible in the UI as a "confluence"
   indicator (a symbol flagged by both sources is worth more attention
   than one flagged by just one).
2. **Live ticks**: subscribes to the shortlist via `KiteTicker`
   (WebSocket) and builds 1-minute candles from raw ticks in memory
   (`intraday_state.py`), plus a running VWAP.
3. **Opening range**: after the first `INTRADAY_OPENING_RANGE_MINUTES`
   (default 15) minutes, locks in each symbol's high/low/average volume
   for that window.
4. **Trigger** (checked on every tick after that): price breaks above the
   opening range high, AND the current minute's volume exceeds
   `INTRADAY_VOLUME_SURGE_MULTIPLIER`x (default 2x) the opening-range
   average, AND price is above VWAP. All three together fires exactly
   one alert per symbol per day.
5. **Alert delivery**: Telegram message (with confluence sources + a
   one-tap TradingView chart link), plus — if you have the dashboard tab
   open — a browser desktop notification, a short audio beep, and a green
   flash on the table row.

### Setup
1. Telegram bot: see the docstring at the top of `telegram_alerts.py` for
   the exact steps (via @BotFather), then add `TELEGRAM_BOT_TOKEN` /
   `TELEGRAM_CHAT_ID` to `.env`.
2. Test Telegram on its own: `python telegram_alerts.py` — should get a
   test message.
3. Sanity-check the trigger logic (no live connection needed):
   `python test_intraday_state.py` — 5 tests, should all pass. (This
   caught a real bug during development — the opening range's last minute
   was silently being dropped every day — fixed and now covered by a
   regression test.)
4. During market hours, either click **Start** on the Intraday Monitor
   tab, or run `python intraday_monitor.py` standalone.

### Important — please verify on your end
This sandbox has zero ability to connect to Kite's real WebSocket feed, so
one thing genuinely could not be tested end-to-end: the exact field names
in Kite's live tick payload. `intraday_state.py` expects `volume_traded`
(cumulative day volume) and `average_traded_price` (running VWAP) — the
documented names as of this writing. On your **first live run**, check the
console's `[intraday debug] first raw tick received: {...}` printout
against those field names. If they don't match, update the two
`tick.get(...)` calls in `intraday_state.py`'s `process_tick()`.

**What this is NOT:** an auto-trader. It only alerts — you decide whether
and how to enter. The "3-5-8%" language is informational only, not an
automated target/exit.

---

## Smart Money pipeline (sector leaders → structure/momentum signal)

End-to-end flow matching the trading requirement:

1. Rank sector indices → take top `SMART_MONEY_TOP_SECTORS` (default 5,
   optionally require above EMA20).
2. Take top `SMART_MONEY_STOCKS_PER_SECTOR` stocks from each of those
   sectors (today / 1D momentum).
3. On each leader, evaluate the Smart Money / Pine-style gates on closed
   5m candles: multi-TF EMA20+VWAP trend, CHoCH/BOS structure, momentum,
   volume, N-bar breakout.
4. When all gates pass → BUY/SELL with ATR-based entry / SL / target / R:R
   / confidence.
5. Telegram alert via `format_smart_money_alert`.

```bash
python test_smart_money_strategy.py   # offline unit tests
python smart_money_monitor.py         # one-shot scan (+ Telegram)
python smart_money_monitor.py --loop  # re-scan during market hours
python smart_money_monitor.py --no-telegram
```

API: `GET/POST /api/smart_money/scan` (add `?telegram=0` to skip Telegram).
Tunables live under the `SMART_MONEY_*` block in `config.py`.

The intraday ORB shortlist also uses steps 1–2 (`shortlist.py`) so the
WebSocket watchlist tracks the same top-sector leaders.

---

## 5. File map

| File | What it does |
|---|---|
| `app.py` | Flask web app — all 7 dashboard tabs |
| `bootstrap.py` | Single-entry-point setup (deps, `.env`, folders) |
| `config.py` | All tunable settings — thresholds, universe, backtest window, intraday params, Telegram |
| `kite_auth.py` | Manual Kite login flow (no stored credentials) |
| `kite_client.py` | Historical/live data fetching, disk caching, rate limiting |
| `universe.py` | Builds the stock universe (Nifty 500 / all NSE / custom file) |
| `indicators.py` | EMA, SMA, RSI, ADX, rolling-max (shared by all scanners) |
| `scanner.py` | Momentum Breakout (13-condition) scanner |
| `ema10_scanner.py` | EMA10 Pullback scanner |
| `nday_scanner.py` | N-Day Breakout (near N-day high/low) scanner |
| `sector_scanner.py` | Sector Analysis (index-level momentum/breakout) |
| `sector_constituents.py` | Fetches per-sector stock lists from NSE |
| `trending_scanner.py` | Trending Stocks by Sector |
| `live_scan.py` | Wraps all scanners for "run against today's data" |
| `backtest.py` | Trade simulation + rolling-window backtest engine |
| `report.py` | Standalone HTML report generator (for `main.py`'s CLI output) |
| `shortlist.py` | Builds the intraday watchlist (momentum + top-sector leaders) |
| `smart_money_strategy.py` | Pine-style multi-TF / CHoCH-BOS / momentum / volume / breakout gates |
| `smart_money_pipeline.py` | Top sectors → leaders → strategy → signals |
| `smart_money_monitor.py` | CLI runner for the Smart Money pipeline |
| `intraday_state.py` | Per-symbol tick to candle state machine + trigger logic |
| `intraday_engine.py` | Start/stop-able WebSocket engine, embedded in the Flask app |
| `intraday_monitor.py` | Standalone CLI wrapper around the same engine |
| `telegram_alerts.py` | Telegram bot messaging (ORB + Smart Money formats) |
| `main.py` | Standalone CLI backtest runner (alternative to the web app) |
| `explain_signal.py` | Shows exactly why a symbol did/didn't signal on a given date |
| `verify_backtest.py` | Independently re-verifies backtest summary stats |
| `debug_yfinance.py` | Diagnostic for yfinance market-cap lookup issues |
| `test_with_synthetic_data.py` | Scanner/backtest sanity check, no Kite needed |
| `test_intraday_state.py` | Intraday trigger logic sanity check, no Kite needed |
| `test_smart_money_strategy.py` | Smart Money strategy unit tests, no Kite needed |
| `templates/index.html` | The dashboard UI (all 7 tabs, single file) |

---

## 6. Roadmap / not yet built

- **Auto-trading**: deliberately not built. When ready, the plan (per
  earlier discussion) is to have this write signals into your *existing*
  Telegram-to-Zerodha bot's file-based `SIGNAL_SOURCE`, reusing its
  already-tested execution/risk logic rather than duplicating
  order-placement code here.
- **WhatsApp alerts**: Telegram was chosen over WhatsApp since WhatsApp
  has no free API for individuals (Meta Business API needs verification,
  Twilio is paid/sandboxed). Not built.
- Sector-constituent URL verification (see caveat in Section 2).
