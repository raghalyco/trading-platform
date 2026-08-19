# Trading Signal Engine

An options scalping/swing signal dashboard for NIFTY/SENSEX — same shape as
the reference app you screenshotted: 7-point indicator score, price-action
pattern detection, SCALP + SMART TRADE modes, entry-confidence meter,
caution flags, and a pre-trade risk gate.

## Run locally

### Prerequisites

- Python 3.10+ (3.11–3.13 work; the project was verified on 3.13)
- pip

No broker credentials are required for simulator mode.

### 1. Clone and create a virtual environment

```bash
cd trading-signal-engine

# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the app

From the repo root (with the venv activated):

```bash
uvicorn app.api.main:app --reload --port 8000
```

### 4. Open the dashboard

Open [http://localhost:8000](http://localhost:8000) — the UI polls `/api/signal` every 6s.

Without `app/kite_session.json`, the app uses **simulated data** automatically (`Source: SIMULATOR` in the top bar).

Useful endpoints while running:

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /api/signal?symbol=NIFTY&mode=SCALP` | Live signal payload |
| `GET /api/signal?symbol=NIFTY&mode=SMART_TRADE` | SMART TRADE mode |
| `GET /api/telegram/preview` | RSTA-style Telegram message (no send) |
| `POST /api/telegram/send` | Send to channel (`dry_run: true` without token) |
| `GET /api/report/performance` | Backtest win/loss/timeout on recent 1m bars |
| `GET /docs` | FastAPI Swagger UI |

#### Telegram (optional)

```powershell
$env:TELEGRAM_BOT_TOKEN = "123456:ABC..."
$env:TELEGRAM_CHAT_ID = "@testalgotradinganand"   # bot must be channel admin
uvicorn app.api.main:app --reload --port 8000
```

Smoke test without posting:

```bash
# from repo root
set PYTHONPATH=.
python scripts/test_telegram_and_report.py
```

To stop the server, press `Ctrl+C` in the terminal.

## Architecture

```
app/
  config.py                  <- all thresholds/risk params in one place
  data_feed/
    base.py                  <- abstract interface (get_ohlcv_1m, spot, vix, expiry)
    simulator.py              <- synthetic feed, works with no broker connection
    kite_feed.py              <- Kite Connect implementation (fill in your login flow)
  indicators/
    core.py                   <- EMA, MACD, RSI, ATR, volume spike, trend direction
    multi_tf.py                <- resamples 1m -> 5m/15m, checks trend agreement
  price_action/
    patterns.py                <- double top/bottom detection, PA score, PA bonus points
  signal_engine/
    scorer.py                  <- 7-point EMA/MACD/15M/5M/1M/VOL/RSI scorer -> verdict
    modes.py                    <- SCALP levels + SMART TRADE (CO+FVG) levels, trailing SL
    confidence.py                <- 0-100% confidence meter + caution list (choppy, expiry, wide SL)
    risk.py                      <- daily max-loss cutoff, position sizing, R:R gate
    orchestrator.py               <- generate_signal() ties it all into one payload
  api/main.py                    <- FastAPI backend + serves the dashboard
  frontend/index.html              <- dashboard UI (dark terminal theme, matches your screenshots)
```

Everything upstream of `data_feed/` is broker-agnostic. Right now it uses a
**manual daily login** (no headless automation yet) so you can validate
against real market data before automating anything:

### Going live (manual token, once per morning)

`kiteconnect` is already in `requirements.txt`. With the venv activated:

```bash
# 1. One-time: find your instrument tokens (needs a valid session first — do step 2 once)
python scripts/generate_session.py
# -> enter Kite API key + secret, open the printed login URL in your browser,
#    log in manually, paste the redirect URL back in
# -> saves app/kite_session.json

python scripts/fetch_instrument_tokens.py
# -> paste the printed tokens into INSTRUMENT_TOKENS in app/data_feed/kite_feed.py

# 2. Every morning before market open (access_token expires daily):
python scripts/generate_session.py

# 3. Start the server as normal — it auto-detects the session file
uvicorn app.api.main:app --reload --port 8000
```

After code is update in Ec2, follow below steps
cd ~/trading-platform
git pull origin main
sudo systemctl restart signal-engine.service
sudo systemctl status signal-engine.service

The dashboard's top bar shows `Source: LIVE (Kite)` vs `Source: SIMULATOR`
so you always know which one you're looking at. If `kite_session.json` is
missing or the token has expired, it silently falls back to the simulator
and logs a warning - it will never crash on startup because of an expired token.

`scripts/generate_session.py` does **not** automate your login - you still
type your user ID/password/TOTP into Kite's real login page yourself. When
you're ready to automate that step later, `KiteFeed.login_headless()` in
`kite_feed.py` is the stub to fill in (Selenium/Playwright + pyotp).

**Note on testing**: I built and tested this whole project against the
simulator feed. The `KiteFeed`/session-file code is written correctly
against Kite Connect's documented API, but I have not been able to test it
against a live Kite session myself (no network access to Zerodha's domains
from where I built this) - so treat your first run against real data as
the first real test of that path.

## Design decisions baked in (from your open questions)

- **Signal source abstraction**: `DataFeed` is the same pattern you're using
  for file-vs-Telegram — swap the implementation, keep everything else.
- **Paper vs live**: the orchestrator doesn't place orders, it only emits
  signals. Wire a thin `OrderExecutor` (paper: just logs; live: `kite.place_order`)
  behind the risk gate's `can_enter` check — that's your paper/live toggle.
- **Instrument type**: engine works on the underlying index (NIFTY/SENSEX);
  strike/instrument selection (options vs futures) happens one layer up, at
  order-placement time, using `atm_strike` from the signal payload.
- **Daily max-loss cutoff**: `RiskManager` in `risk.py` — locks out new
  entries once `daily_max_loss_pct` of capital is lost or `max_trades_per_day`
  is hit. Call `risk_mgr.record_trade_result(pnl)` after each closed trade.
- **Profit-cap mid-trade behavior**: SMART TRADE mode trails the stop after
  T1 (`trailing_stop_update()` in `modes.py`) instead of hard-capping profit —
  lets winners run while locking in gains. Call it on every tick for open
  SMART TRADE positions once price crosses `target1`.

## Additional signal types (matching rsta.in's feature set)

Added alongside the main 7-point signal:

- **ORB Breakout** (`signal_engine/orb.py`) — fires when a closed 5m candle
  breaks the 9:15-9:30 IST opening range, only active after 9:30 AM.
- **Retest Entry** (`signal_engine/retest.py`) — 4 retest types (ORB level,
  S/R zone, EMA9, VWAP), only fires on candles with 65%+ body strength.
- **Gamma Blast** (`signal_engine/gamma_blast.py`) — expiry-day premium
  compression proxy score (0-8), NIFTY Tuesdays / SENSEX Thursdays. This is
  a simplified proxy from index volatility contraction + VIX + volume, not
  a real options-greeks/IV model — a production version should pull actual
  IV from the option chain instead.
- **Auto Trade Journal** (`signal_engine/journal.py`) — SQLite-backed
  (`app/journal.db`, created automatically, stdlib only). Logs entry/exit,
  hold time, P&L in points and rupees, R:R, result (WIN/LOSS). New endpoints:
  - `POST /api/journal/entry` — log a trade on entry
  - `POST /api/journal/exit` — log the exit, computes P&L automatically
  - `GET /api/journal?result=WIN` — list trades, optional WIN/LOSS/OPEN filter
  - `GET /api/journal/export.csv` — download as CSV

All three new signal types are included in every `/api/signal` response
under `orb`, `retest`, and `gamma_blast` keys - the dashboard doesn't render
them as separate cards yet (that's a frontend addition, not yet built).

**Timezone note**: ORB and Gamma Blast both check IST-specific windows
(9:30 AM open, expiry weekday). They use `zoneinfo.ZoneInfo("Asia/Kolkata")`
explicitly rather than server local time, since your EC2 instance may run
in UTC - this was tested and confirmed correct in a UTC sandbox.

**Not yet added**: Telegram push alerts (needs a bot token + chat ID from
you), and dashboard UI cards for ORB/Retest/Gamma Blast (backend is ready,
frontend still only shows the main SCALP/SMART TRADE card).

## Regime detection (trend vs range)

Added `signal_engine/regime.py`, using Wilder's ADX (`indicators/adx.py`)
on the 5-min timeframe:

- **ADX >= 30** -> `STRONG_TREND`
- **ADX >= 20** -> `TRENDING`
- **ADX < 20** -> `RANGE`

ORB Breakout and Retest Entry signals are both breakout-style setups that
produce more false signals in range-bound conditions than in trending
ones. `gate_breakout_signal()` suppresses them (sets `active: False` with
an explanatory reason) whenever the regime is `RANGE` - this doesn't
touch the main 7-point scorer or SCALP/SMART TRADE levels, only ORB/Retest.

Verified both directions with synthetic data: stays active in a genuine
trend (ADX 41.9 in a live test run), correctly suppressed in a genuinely
range-bound series (ADX 19.0). The dashboard now shows a `Regime: ...`
badge in the top bar (color-coded: amber=RANGE, cyan=TRENDING, green=STRONG_TREND).

## SMART TRADE's own scoring system (6-point, not 7)

Your screenshot revealed SMART TRADE uses a genuinely different scorer than
SCALP - `EMA, ADX, OB, FVG, VOL, OI` instead of SCALP's 7-point
`EMA, MACD, 15M, 5M, 1M, VOL, RSI`. Added:

- **Order Block detection** (`signal_engine/order_block.py`) - a
  smart-money-concept pattern: the last opposite-colored candle right
  before a strong "displacement" move. Returns a quality label like
  `"Moderate OB (14.3pts SL)"` matching your screenshot's format.
  This is a simplified rule-based approximation, not a full discretionary
  SMC read (which also considers liquidity sweeps and market structure
  shifts that a fixed formula can't fully capture).
- **ADX exposed as its own scoring component** (`indicators/adx.py`) -
  `directional_indicators()` now returns +DI/-DI separately, used as a
  directional vote (CE if +DI > -DI, PE if -DI > +DI, NEUTRAL if ADX < 15
  i.e. too weak a trend to trust direction from).
- **Direction-agnostic FVG detector** (`detect_fvg_either` in `modes.py`) -
  checks both bullish and bearish gaps for scoring purposes, separate
  from the existing side-specific `detect_fvg` used for level calculation.
- **Expiry date display** (`current_expiry_date()` in `modes.py`) - computes
  the nearest upcoming weekly expiry (NIFTY Tue / SENSEX Thu) as `DDMMMYY`.
- **New `smart_scorer.py`** ties these together into the 6-point score,
  wired into the orchestrator only for `SMART_TRADE` mode - SCALP's
  original 7-point scorer is untouched.

### PCR / Open Interest - honest gap, not faked

Your screenshot also showed `PCR:1` and an `OI` scoring component. **This
is not implemented with real data yet, and I'm not going to fake it.**
PCR requires live options-chain Open Interest - a fundamentally different
Kite API surface than the index-only feed this engine currently uses
(fetching hundreds of option instrument tokens across strikes for the
current expiry, then batch-quoting them for OI). `signal_engine/options_data.py`
has the correct calculation function (`calculate_pcr`) and a clearly-stubbed
`fetch_option_chain_oi()` with the exact implementation steps documented -
until that's wired up, the OI component always reports `NEUTRAL` and the
dashboard shows "PCR: not live" rather than a fabricated number.

### Dual price display (index points vs option premium) - not implemented

Your screenshot shows both index-point levels AND an approximate option
premium (e.g. entry 24016.3 / premium 121). Reproducing this accurately
needs a live option quote for the specific strike+expiry+CE/PE contract
you're trading, not a synthetic estimate - a wrong premium number could
directly mislead a trade decision, so this isn't faked either. Once
`fetch_option_chain_oi()` is wired up, adding the matching premium quote
alongside it is a natural next step.

## Manual trade calculator (strike selection + fixed points)

Added for a manual trading style (pick a specific OTM strike, use fixed
premium-point SL/T1/T2 rather than ATR-based auto-sizing) - a genuinely
different philosophy than SCALP/SMART TRADE's dynamic levels, not a
replacement for them:

- `signal_engine/strike_selector.py` - `pick_strike(spot, symbol, otm_steps, side)`
  rounds to the nearest tradeable strike then steps further OTM;
  `fixed_points_levels()` computes entry/T1/T2/SL from a real premium plus
  your fixed point offsets.
- `KiteFeed.get_option_ltp()` - fetches the live premium of ONE specific
  option contract (underlying + expiry + strike + CE/PE), via
  `kite.instruments("NFO")` (cached per-process) + `kite.quote()`. This is
  simpler than the full option-chain OI/PCR work still stubbed out, since
  it only needs one contract's quote, not hundreds.
- `POST /api/manual-trade` - `{symbol, side, otm_steps, sl_points, t1_points, t2_points}`
  -> picks the strike, fetches the real live premium (Kite mode only -
  simulator mode honestly returns an error rather than a fake premium),
  returns entry/T1/T2/SL in real premium points.

Verified: strike selection matches a real example exactly (spot 24226,
NIFTY, 1 OTM step, CE -> 24300, matching manual calculation). The live
premium fetch path is written correctly against Kite's documented API
shape but - same as `KiteFeed`'s other live-data methods - hasn't been
tested against an actual live session from this environment.

**Math check worth knowing**: if your SL is bigger than your T1 (e.g.
SL=30, T1=20), your R:R on T1 is below 1:1 - you're risking more than
that first target pays out. Not wrong by itself (some styles treat T1 as
a quick partial-exit, not the main payoff), but worth confirming that's
intentional before sizing real trades around it.

## Surfacing backend data into the UI (P&L header + estimated premium)

Previously the journal and estimated-premium calculations existed but
weren't visible anywhere on the dashboard. Added:

- **P&L Today** chip in the top bar, next to Spot/ATM - pulls from the new
  `journal.daily_summary()` (`GET /api/journal/daily-summary`), showing
  today's realized P&L in rupees and trade count, color-coded green/red.
  Refreshes alongside the main signal every 6s.
- **Estimated premium note** on the main signal card - shows the ATM
  estimated premium with an explicit "(est - not a live quote)" label,
  matching the reference app's own honest "(est)" tagging rather than
  presenting it as a real price.

Tested end-to-end: logged a real trade through the journal endpoints,
confirmed the daily summary correctly aggregated it (-Rs 3000, 1 loss),
and confirmed the dashboard still loads clean with both new elements wired in.

## Journal UI (trade history table, IN/EXIT buttons)

Previously the journal was API-only. Added to the dashboard:

- **IN / SKIP buttons** on the main signal card - IN logs a real journal
  entry via `POST /api/journal/entry` using the current signal's
  symbol/side/mode/entry price, then disables itself and shows
  `OPEN #<id>` until that trade is exited. SKIP is currently a no-op hook
  (nothing to log for a skipped signal, but left in place if you want
  skip-rate tracking later).
- **Trade journal table** below the secondary signal cards - shows every
  logged trade (time, symbol, side, entry, exit, P&L), color-coded by
  result (green=WIN, red=LOSS, cyan=OPEN), with an **EXIT** button on any
  open row.
- **Export CSV** link, using the existing `/api/journal/export.csv` endpoint.
- Open trades persist across page refreshes - `loadJournal()` checks for
  any `OPEN` row on load and re-disables the IN button accordingly, so
  reloading the page doesn't lose track of an open position.

**Known limitation, stated plainly**: EXIT currently uses the *current
index level* as the exit price proxy, not a real live premium (same
"honest estimate, not fabricated" stance as `estimated_premium` elsewhere
in this project) - accurate P&L in rupees still depends on wiring
`KiteFeed.get_option_ltp()` in for the actual traded contract.

Tested end-to-end: opened a real trade via the UI's IN button, confirmed
it shows in the journal table as OPEN, confirmed the dashboard and signal
endpoints keep working alongside it.

## Still open (needs your input before going live)


- Telegram message parser for the signal-source abstraction (format not yet specified).
- Actual position-tracking/EXIT flow shown in your Sensex screenshot — the
  engine currently only *generates* signals, it doesn't track open positions.
  Add a `positions.py` module keyed by order ID once order placement is wired in.
- Real instrument tokens + lot sizes for NIFTY/SENSEX options in `kite_feed.py`.
- CI/CD (GitHub Actions) deploy step to your EC2 box — not included here,
  since it depends on your existing deployment setup.
