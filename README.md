# Trading Platform

A personal NSE options/equity trading toolkit built around the Zerodha Kite
Connect API, made up of three independent subsystems that share one daily
broker login:

| Folder | What it does | Details |
|---|---|---|
| [`scanner/`](scanner/README.md) | Local research web app: daily stock scanners, sector analysis, a 6-month backtester, and a live intraday alert engine (opening-range breakout monitor with Telegram alerts). | Runs at `http://127.0.0.1:5000`. |
| [`signal_engine/`](signal_engine/README.md) | Options scalping/swing signal dashboard for NIFTY/SENSEX: 7-point indicator score (EMA/MACD/15M/5M/1M/VOL/RSI), price-action pattern detection, SCALP + SMART TRADE modes, entry-confidence meter, and pre-trade risk gate. | Can run against a `SimulatorFeed` (no broker credentials needed) or live via `KiteFeed`. |
| [`execution/`](execution/README.md) | Telegram-driven execution bot: listens for parsed option trade signals, resolves the matching Kite contract, places market entry orders on live tick data via `KiteTicker`, and manages exits with GTT orders. | Persists pending trades in `pending_trades.json` for restart recovery. |

Each subsystem was originally its own standalone repo (`kite_scanner`,
`trading-signal-engine`, `nifty-algo-trading`) and was merged into this
monorepo with history preserved — see `git log` for the merge commits.

## Shared Kite Connect login

[`shared/kite_auth.py`](shared/kite_auth.py) is a single manual-login flow
used by all three subsystems, so you only log in to Zerodha once per day no
matter which app you start first:

```bash
python -m shared.kite_auth
```

This opens a login URL, you complete the Zerodha login (user ID, password,
TOTP) in a normal browser, then paste back the `request_token` (or the full
redirect URL) when prompted. The resulting access token is cached to
`cache/access_token.json` with today's date and reused by `scanner/`,
`signal_engine/`, and `execution/` until it expires (Kite tokens expire
daily, around midnight IST).

You'll need a **Kite Connect API subscription** (a separate paid product
from your regular Zerodha account, ~₹2000+GST/month, from
https://developers.kite.trade) with `KITE_API_KEY` / `KITE_API_SECRET` set
in `.env` at the repo root (see `.env.example`).

## Running a subsystem

Each subsystem is self-contained with its own dependencies and entry point
— see the linked README above for exact setup steps. In short:

```bash
cd scanner && python app.py           # research/scanner dashboard
cd signal_engine && python -m app.api.main   # signal dashboard
cd execution && python index.py       # Telegram execution bot
```

## Repo layout

```
trading-platform/
├── scanner/         # Kite Scanner Bot (research + intraday alerts)
├── signal_engine/   # Options scalping/swing signal dashboard
├── execution/       # Telegram-driven order execution bot
├── shared/          # kite_auth.py — one login shared by all three
├── cache/           # cached Kite access token (git-ignored)
└── .env             # KITE_API_KEY / KITE_API_SECRET (git-ignored)
```
