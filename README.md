# Nifty Algo Trading Bot

This project is a Python-based Telegram trading bot that listens for option trade signals, resolves matching contracts through Zerodha Kite, and places GTT orders for entry and exit management.

## What It Does

- Connects to Telegram using Telethon and listens for new messages from a configured source chat.
- Parses option signals for `NIFTY`, `BANKNIFTY`, and `SENSEX`.
- Matches the signal to a live derivatives contract using Zerodha instrument and LTP data.
- Places entry GTT orders when the contract is within the allowed entry range.
- Monitors pending signals and filled entries, then places protective exit GTT orders.
- Logs the full processing path for each Telegram message so trade decisions and failures can be traced in `bot_output.log`.

## Supported Signal Flow

The main bot logic lives in [index.py](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/index.py).

Current live flow:

1. Receive a Telegram message.
2. Log the message with an event id.
3. Check source chat and market timing restrictions.
4. Parse the trading signal.
5. Resolve the best matching Zerodha contract.
6. Place entry GTT orders.
7. Monitor fills.
8. Place exit GTT orders for stop loss and target.
9. Monitor active trades until target or stop loss is hit.

## Dynamic Profit Target Logic

The bot calculates the target move from a daily rupee goal instead of using a hardcoded point value.

Formula:

`required target points = daily profit target / configured lot size`

Default production values:

- `DAILY_PROFIT_TARGET_RUPEES = 500`
- `NIFTY_PROFIT_TARGET_LOT_SIZE = 65`
- `BANKNIFTY_PROFIT_TARGET_LOT_SIZE = 30`
- `SENSEX_PROFIT_TARGET_LOT_SIZE = 20`

Derived defaults:

- `NIFTY`: `500 / 65 = 7.69` points
- `BANKNIFTY`: `500 / 30 = 16.67` points
- `SENSEX`: `500 / 20 = 25.00` points

The bot still respects the target range provided in the Telegram message, so the computed target acts within the current signal behavior rather than replacing it entirely.

## Main Files

- [index.py](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/index.py): Main bot, Telegram event handling, signal parsing, Kite integration, GTT logic, and logging.
- [kite_auth.py](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/kite_auth.py): Zerodha login and access token refresh handling.
- [test_bot.py](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/test_bot.py): Local validation script for parsing, authentication, contract matching, and target-point preview.
- [DEPLOYMENT.md](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/DEPLOYMENT.md): EC2 and CI/CD deployment notes.
- [config.txt](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/config.txt): Zerodha credentials and API settings.
- `access_token.txt`: Persisted Zerodha access token.
- `trading_session.session`: Telethon session file for Telegram login.

## Requirements

Dependencies are listed in [requirements.txt](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/requirements.txt):

- `requests`
- `pyotp`
- `kiteconnect`
- `telethon`

## Local Run

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r .\requirements.txt
```

Run the local validation script:

```powershell
python .\test_bot.py
```

Run the bot:

```powershell
python .\index.py
```

Watch the logs:

```powershell
Get-Content .\bot_output.log -Wait
```

## Optional Environment Variables

These can be set in live deployment or local testing:

- `BOT_LOG_FILE`
- `LOG_ONLY_MODE`
- `DAILY_PROFIT_TARGET_RUPEES`
- `NIFTY_PROFIT_TARGET_LOT_SIZE`
- `BANKNIFTY_PROFIT_TARGET_LOT_SIZE`
- `SENSEX_PROFIT_TARGET_LOT_SIZE`

## Logging

The bot writes structured operational logs to `bot_output.log` and also prints them to the console.

Logs include:

- Telegram message receipt
- Source chat filtering
- Signal parsing results
- Contract resolution
- Entry GTT success or failure
- Exit GTT success or failure
- Monitoring and trade close events
- Startup configuration and fallback warnings

## Deployment Notes

The repository includes EC2 deployment guidance in [DEPLOYMENT.md](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/DEPLOYMENT.md).

For production:

- Keep `config.txt`, `access_token.txt`, and Telegram session files on the server.
- Ensure the Telegram session is used from only one environment at a time.
- Confirm the startup log prints the expected profit-target configuration.

## Current Scope

This bot currently targets option signals for:

- `NIFTY`
- `BANKNIFTY`
- `SENSEX`

Additional indices such as `FINNIFTY` or `MIDCPNIFTY` are not yet wired into the parsing and contract-resolution flow.