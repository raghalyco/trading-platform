# Nifty Algo Trading Bot

This project is a Python-based Telegram trading bot that listens for option trade signals, resolves matching contracts through Zerodha Kite, monitors live LTP ticks with KiteTicker, places market entry orders when entry levels are crossed, and places GTT orders for exit management.

## What It Does

- Connects to Telegram using Telethon and listens for new messages from a configured source chat.
- Parses option signals for `NIFTY`, `BANKNIFTY`, and `SENSEX`.
- Matches the signal to a live derivatives contract using Zerodha instrument and initial LTP data.
- Saves valid signals as pending trades with `WAITING_FOR_ENTRY` status.
- Subscribes to live KiteTicker WebSocket ticks and places a BUY market order when LTP crosses the entry price.
- Prevents duplicate BUY orders by moving a pending trade to `ENTRY_TRIGGERED` before placing the market order.
- Monitors triggered entries until the BUY order is complete, then places protective exit GTT orders.
- Persists pending trades in `pending_trades.json` so valid untriggered signals can be recovered after restart.
- Logs the full processing path for each Telegram message so trade decisions and failures can be traced in `bot_output.log`.

## Supported Signal Flow

The main bot logic lives in [index.py](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/index.py).

Current live flow:

1. Receive a Telegram message.
2. Log the message with an event id.
3. Check source chat and market timing restrictions.
4. Parse the trading signal.
5. Resolve the best matching Zerodha contract.
6. Create a persisted pending trade with symbol, instrument token, entry, stop loss, targets, quantity, Telegram message id, created time, and `WAITING_FOR_ENTRY` status.
7. Subscribe to the contract token through KiteTicker.
8. On each tick, check pending trades for that instrument.
9. When LTP crosses the entry price, place one BUY market order and mark the trade `ENTRY_TRIGGERED`.
10. Reconcile the BUY order from the Kite order book.
11. After the BUY completes, mark it `BUY_EXECUTED`, place the protective exit GTT, and monitor the active trade until target or stop loss is hit.
12. If entry is not crossed before the configured expiry window, mark the pending trade `EXPIRED` and stop monitoring it.

## Pending Trade Recovery

Pending entry state is stored in `pending_trades.json`.

On startup, the bot:

- Loads untriggered `WAITING_FOR_ENTRY` trades from the file.
- Drops any pending trade whose expiry window has already passed.
- Re-subscribes recovered instrument tokens through KiteTicker.
- Logs each recovered or expired trade.

The file is runtime state and is ignored by Git.

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

- [index.py](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/index.py): Main bot, Telegram event handling, signal parsing, Kite integration, KiteTicker LTP monitoring, market entry logic, exit GTT logic, persistence, and logging.
- [kite_auth.py](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/kite_auth.py): Zerodha login and access token refresh handling.
- [test_bot.py](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/test_bot.py): Local validation script for parsing, authentication, contract matching, and target-point preview.
- [DEPLOYMENT.md](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/DEPLOYMENT.md): EC2 and CI/CD deployment notes.
- [config.txt](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/config.txt): Zerodha credentials and API settings.
- `access_token.txt`: Persisted Zerodha access token.
- `trading_session.session`: Telethon session file for Telegram login.
- `pending_trades.json`: Runtime pending-trade recovery state.

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
- `PENDING_TRADES_FILE`
- `PENDING_TRADE_EXPIRY_MINUTES`
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
- Pending trade creation
- KiteTicker connection and subscription events
- Tick receipt
- Entry price crossing
- BUY market order placement
- BUY order execution
- Pending trade expiry
- Pending trade recovery after restart
- Exit GTT success or failure
- Monitoring and trade close events
- Startup configuration and fallback warnings

## Deployment Notes

The repository includes EC2 deployment guidance in [DEPLOYMENT.md](c:/Users/anand/source/github/algotradingnifty/nifty-algo-trading/DEPLOYMENT.md).

For production:

- Keep `config.txt`, `access_token.txt`, and Telegram session files on the server.
- Keep `pending_trades.json` on the server if you want restart recovery for untriggered signals.
- Ensure the Telegram session is used from only one environment at a time.
- Confirm the startup log prints the expected profit-target, pending-expiry, and pending-trade-file configuration.

## Current Scope

This bot currently targets option signals for:

- `NIFTY`
- `BANKNIFTY`
- `SENSEX`

Additional indices such as `FINNIFTY` or `MIDCPNIFTY` are not yet wired into the parsing and contract-resolution flow.
