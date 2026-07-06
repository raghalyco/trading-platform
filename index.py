import csv
import asyncio
import json
import logging
import os
import re
import calendar
from datetime import date, datetime
from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError
from kite_auth import get_kite_client

# ==========================================
# 1. BOT CONFIGURATIONS
# ==========================================
API_ID = 35784910  # Update this with your real Telethon API ID
API_HASH = '4a73f7632189dd4b9768b7bab06baa71'  # Update this with your real Telethon API Hash

CONFIG_WARNINGS = []

def get_env_float(name, default):
    raw_value = os.getenv(name)
    if raw_value in (None, ""):
        return default
    try:
        parsed_value = float(raw_value)
    except ValueError:
        CONFIG_WARNINGS.append(f"Invalid float for {name}: {raw_value!r}. Using default {default}.")
        return default

    if parsed_value <= 0:
        CONFIG_WARNINGS.append(f"Non-positive float for {name}: {raw_value!r}. Using default {default}.")
        return default
    return parsed_value

def get_env_int(name, default):
    raw_value = os.getenv(name)
    if raw_value in (None, ""):
        return default
    try:
        parsed_value = int(raw_value)
    except ValueError:
        CONFIG_WARNINGS.append(f"Invalid integer for {name}: {raw_value!r}. Using default {default}.")
        return default

    if parsed_value <= 0:
        CONFIG_WARNINGS.append(f"Non-positive integer for {name}: {raw_value!r}. Using default {default}.")
        return default
    return parsed_value

def log_telegram_session_reset_required(logger, session_name):
    logger.exception(
        "Telegram session '%s' is no longer usable because the auth key was duplicated across multiple IPs. "
        "Stop any other bot instance using this login and recreate %s.session before running again.",
        session_name,
        session_name,
    )

SOURCE_CHAT = 'Option Playbook by SK'
NOTIFICATION_CHAT = 't.me/testalgotradinganand'
KITE_PRODUCT = "NRML"
KITE_ORDER_TYPE = "LIMIT"
KITE_PRICE_MATCH_TOLERANCE = 1.0
KITE_ENTRY_ABOVE_LADDER_TOLERANCE = 5.0
DAILY_PROFIT_TARGET_RUPEES = get_env_float("DAILY_PROFIT_TARGET_RUPEES", 500.0)
PENDING_SIGNAL_CHECK_INTERVAL_SECONDS = 5

KITE_LTP_URL = "https://api.kite.trade/quote/ltp"
KITE_GTT_URL = "https://api.kite.trade/gtt/triggers"
TELEGRAM_LOG_FILE = os.getenv("BOT_LOG_FILE", "bot_output.log")
LOG_ONLY_MODE = os.getenv("LOG_ONLY_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}

UNDERLYING_CONFIG = {
    "NIFTY": {
        "exchange": "NFO",
        "instruments_url": "https://api.kite.trade/instruments/NFO",
        "profit_target_lot_size": get_env_int("NIFTY_PROFIT_TARGET_LOT_SIZE", 65),
    },
    "BANKNIFTY": {
        "exchange": "NFO",
        "instruments_url": "https://api.kite.trade/instruments/NFO",
        "profit_target_lot_size": get_env_int("BANKNIFTY_PROFIT_TARGET_LOT_SIZE", 30),
    },
    "SENSEX": {
        "exchange": "BFO",
        "instruments_url": "https://api.kite.trade/instruments/BFO",
        "profit_target_lot_size": get_env_int("SENSEX_PROFIT_TARGET_LOT_SIZE", 20),
    },
}

PRICE_VALUE_REGEX = r"\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?"
ACTION_REGEX = re.compile(r"\b(?P<action>BUY|SELL)\b", re.IGNORECASE)
ENTRY_KEYWORD_REGEX = r"(?:entry|enty)"
ENTRY_TRIGGER_REGEX = re.compile(rf"\b{ENTRY_KEYWORD_REGEX}\b\s*(?:only\s+)?(?P<direction>above|below)\s*(?:is|at|@|:|-)?\s*(?P<value>{PRICE_VALUE_REGEX})", re.IGNORECASE)

# Updated to support trailing optional expiry variations like '7th July', '14th Jul', 'July End'
SIGNAL_REGEX = re.compile(
    r"\b(?P<underlying>NIFTY|BANKNIFTY|SENSEX)\s*(?P<strike>\d{4,6})\s*(?P<option_type>CE|PE)(?:[ \t]+(?P<expiry_date>\d{1,2}(?:st|nd|rd|th)?[ \t]*[a-zA-Z]+|[a-zA-Z]+[ \t]*(?:monthly|end)?))?\b"
    r"|\b(?P<strike_alt>\d{4,6})\s*(?P<option_type_alt>CE|PE)\b",
    re.IGNORECASE,
)
RANGE_REGEX = re.compile(rf"\b(?:range|rng|{ENTRY_KEYWORD_REGEX})\b\s*(?:is|at|@|:|-)?\s*(?P<value>{PRICE_VALUE_REGEX})", re.IGNORECASE)
TARGET_KEYWORD_REGEX = r"(?:target|taget|tgt)"
TARGET_ONE_REGEX = re.compile(rf"\b{TARGET_KEYWORD_REGEX}\s*1\b\s*(?:is|at|@|:|-)?\s*(?P<value>{PRICE_VALUE_REGEX})", re.IGNORECASE)
TARGET_REGEX = re.compile(rf"\b{TARGET_KEYWORD_REGEX}\b(?!\s*\d)\s*(?:is|at|@|:|-)?\s*(?P<value>{PRICE_VALUE_REGEX})", re.IGNORECASE)
SL_REGEX = re.compile(rf"\b(?:sl|stop\s*loss|stoploss)\b\s*(?:is|at|@|:|-)?\s*(?P<value>{PRICE_VALUE_REGEX})", re.IGNORECASE)

telegram_logger = logging.getLogger("telegram_messages")
if not telegram_logger.handlers:
    telegram_logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(TELEGRAM_LOG_FILE, encoding="utf-8")
    stream_handler = logging.StreamHandler()

    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    telegram_logger.addHandler(file_handler)
    telegram_logger.addHandler(stream_handler)
    telegram_logger.propagate = False

kite_client = None
client = TelegramClient('trading_session', API_ID, API_HASH)
pending_signals = []
pending_entry_batches = []
active_trades = []

# ==========================================
# 2. MATCHING PARSING LOGIC & WRAPPER METHODS
# ==========================================
def parse_message_expiry(date_str):
    """
    Parses complex text variants like '7th July', '14th Jul', or 'July End'
    into a structured date object for comparison with Kite master data.
    """
    if not date_str:
        return None
    try:
        current_year = datetime.now().year
        cleaned = date_str.strip().lower()
        
        # --- Handle Monthly Expiries (e.g., "july monthly", "july end") ---
        if "month" in cleaned or "end" in cleaned:
            for m_idx in range(1, 13):
                m_name = calendar.month_name[m_idx].lower()
                m_short = calendar.month_abbr[m_idx].lower()
                if m_name in cleaned or m_short in cleaned:
                    # Nifty monthly options expire on the Last Thursday of the month
                    last_day = calendar.monthrange(current_year, m_idx)[1]
                    for day in range(last_day, 0, -1):
                        possible_date = date(current_year, m_idx, day)
                        if possible_date.weekday() == 3:  # 3 is Thursday
                            return possible_date
                            
        # --- Handle Standard Weekly Expiries (e.g., "7th July", "14th Jul") ---
        cleaned_weekly = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str.strip(), flags=re.IGNORECASE)
        dated_weekly = f"{cleaned_weekly} {current_year}"
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(dated_weekly, fmt).date()
            except ValueError:
                continue
    except Exception as e:
        telegram_logger.warning("Date parser optimization exception: %s", e)
    return None

def extract_signal(text):
    text = text or ""
    action_match = ACTION_REGEX.search(text)
    signal_match = SIGNAL_REGEX.search(text)
    range_match = RANGE_REGEX.search(text)
    entry_trigger_match = ENTRY_TRIGGER_REGEX.search(text)
    target_match = TARGET_ONE_REGEX.search(text) or TARGET_REGEX.search(text)
    sl_match = SL_REGEX.search(text)
    action = action_match.group("action").upper() if action_match else None
    entry_value = None

    if range_match:
        entry_value = range_match.group("value").replace(" ", "")
    elif entry_trigger_match:
        action = action or "BUY"
        entry_value = entry_trigger_match.group("value").replace(" ", "")
    
    if not action or not signal_match or not entry_value or not target_match or not sl_match:
        return None
        
    return {
        "action": action,
        "underlying": (signal_match.group("underlying") or "NIFTY").upper(),
        "strike": signal_match.group("strike") or signal_match.group("strike_alt"),
        "option_type": (signal_match.group("option_type") or signal_match.group("option_type_alt")).upper(),
        "expiry_date_str": signal_match.group("expiry_date") if signal_match.group("strike") else None,
        "entry_range": entry_value,
        "target_range": target_match.group("value").replace(" ", ""),
        "sl": sl_match.group("value").replace(" ", ""),
    }

def first_price(value):
    return float(value.split("-", 1)[0])

def last_price_from_range(value):
    return float(value.rsplit("-", 1)[-1])

def price_bounds(value):
    prices = [float(part) for part in value.split("-")]
    if len(prices) == 1:
        price = prices[0]
        return price - KITE_PRICE_MATCH_TOLERANCE, price + KITE_PRICE_MATCH_TOLERANCE
    return min(prices), max(prices)

def normalize_order_price(price):
    return int(price) if float(price).is_integer() else round(price, 2)

def normalize_order_quantity(quantity):
    return int(float(quantity))

def normalize_chat_identifier(value):
    if not value:
        return None

    normalized = value.strip().lower()
    normalized = re.sub(r"^https?://", "", normalized)
    normalized = normalized.removeprefix("t.me/")
    normalized = normalized.removeprefix("telegram.me/")
    normalized = normalized.removeprefix("@")
    return normalized.strip("/") or None

def describe_chat_entity(entity):
    if entity is None:
        return {"id": None, "title": None, "username": None, "entity_type": None}

    entity_id = getattr(entity, "id", None)
    if entity_id is None:
        for attribute_name in ("channel_id", "chat_id", "user_id"):
            entity_id = getattr(entity, attribute_name, None)
            if entity_id is not None:
                break

    return {
        "id": entity_id,
        "title": getattr(entity, "title", None),
        "username": getattr(entity, "username", None),
        "entity_type": entity.__class__.__name__,
    }

async def resolve_chat_reference(client, chat_reference):
    try:
        return await client.get_input_entity(chat_reference)
    except ValueError:
        expected_chat = normalize_chat_identifier(chat_reference)
        async for dialog in client.iter_dialogs():
            dialog_entity = dialog.entity
            chat_candidates = {
                normalize_chat_identifier(getattr(dialog_entity, "username", None)),
                normalize_chat_identifier(getattr(dialog_entity, "title", None)),
                normalize_chat_identifier(getattr(dialog, "name", None)),
            }
            if expected_chat in chat_candidates:
                return dialog_entity

    raise ValueError(
        f"Cannot find any Telegram entity corresponding to {chat_reference!r}. Use a dialog title, public username, or chat id."
    )

async def log_resolved_source_chat(client, source_chat):
    try:
        resolved_reference = await resolve_chat_reference(client, source_chat)
        resolved_entity = await client.get_entity(resolved_reference)
        telegram_logger.info(
            "Resolved source chat %s to entity %s using reference %s",
            source_chat,
            describe_chat_entity(resolved_entity),
            describe_chat_entity(resolved_reference),
        )
    except Exception:
        telegram_logger.exception("Unable to resolve configured source chat at startup: %s", source_chat)

def build_event_id(chat, event):
    chat_id = getattr(chat, "id", "unknown-chat")
    message_id = getattr(event, "id", "unknown-message")
    return f"{chat_id}:{message_id}"

def signal_summary(signal):
    return (
        f"{signal['action']} {signal['underlying']} {signal['strike']}{signal['option_type']} "
        f"entry={signal['entry_range']} target={signal['target_range']} sl={signal['sl']}"
    )

def event_matches_source_chat(chat, source_chat):
    expected_chat = normalize_chat_identifier(source_chat)
    if not expected_chat:
        return True

    chat_candidates = {
        normalize_chat_identifier(getattr(chat, "username", None)),
        normalize_chat_identifier(getattr(chat, "title", None)),
    }
    return expected_chat in chat_candidates

def build_entry_prices(entry_range, last_price):
    prices = [float(part) for part in entry_range.split("-")]
    if len(prices) == 1:
        return [normalize_order_price(prices[0])]

    low_price, high_price = min(prices), max(prices)
    if low_price <= last_price <= high_price:
        return [normalize_order_price(last_price)]

    overshoot = last_price - high_price
    if overshoot <= 0 or overshoot > KITE_ENTRY_ABOVE_LADDER_TOLERANCE:
        return [normalize_order_price(low_price)]

    midpoint = (last_price + high_price) / 2
    if float(last_price).is_integer() and float(high_price).is_integer():
        midpoint = float(int(midpoint))

    entry_prices = []
    for price in (last_price, midpoint, high_price):
        normalized_price = normalize_order_price(price)
        if normalized_price not in entry_prices:
            entry_prices.append(normalized_price)
    return entry_prices

def build_entry_payload(exchange, tradingsymbol, action, quantity, entry_price, last_price):
    return {
        "type": "single",
        "condition": json.dumps({"exchange": exchange, "tradingsymbol": tradingsymbol, "trigger_values": [entry_price], "last_price": last_price}),
        "orders": json.dumps([{
            "exchange": exchange, "tradingsymbol": tradingsymbol, "transaction_type": action,
            "quantity": quantity, "order_type": KITE_ORDER_TYPE, "product": KITE_PRODUCT, "price": entry_price
        }])
    }

def build_exit_payload(exchange, tradingsymbol, exit_action, quantity, sl_price, target_price, last_price):
    return {
        "type": "two-leg",
        "condition": json.dumps({"exchange": exchange, "tradingsymbol": tradingsymbol, "trigger_values": [sl_price, target_price], "last_price": last_price}),
        "orders": json.dumps([
            {"exchange": exchange, "tradingsymbol": tradingsymbol, "transaction_type": exit_action, "quantity": quantity, "order_type": KITE_ORDER_TYPE, "product": KITE_PRODUCT, "price": sl_price},
            {"exchange": exchange, "tradingsymbol": tradingsymbol, "transaction_type": exit_action, "quantity": quantity, "order_type": KITE_ORDER_TYPE, "product": KITE_PRODUCT, "price": target_price}
        ])
    }

def target_lot_size_for_underlying(underlying, fallback_quantity):
    configured_lot_size = normalize_order_quantity(
        UNDERLYING_CONFIG.get(underlying, {}).get("profit_target_lot_size") or 0
    )
    if configured_lot_size > 0:
        return configured_lot_size

    normalized_quantity = normalize_order_quantity(fallback_quantity)
    if normalized_quantity <= 0:
        raise ValueError("Lot size must be greater than zero to derive target points")
    return normalized_quantity

def required_profit_capture_points(underlying, fallback_quantity):
    lot_size = target_lot_size_for_underlying(underlying, fallback_quantity)
    return DAILY_PROFIT_TARGET_RUPEES / lot_size, lot_size

def profit_target_config_summary():
    return {
        underlying: config["profit_target_lot_size"]
        for underlying, config in UNDERLYING_CONFIG.items()
    }

def effective_target_price(signal, entry_price, quantity):
    configured_target = last_price_from_range(signal["target_range"])
    target_points, _ = required_profit_capture_points(signal["underlying"], quantity)
    if signal["action"] == "BUY":
        return normalize_order_price(min(configured_target, entry_price + target_points))
    return normalize_order_price(max(configured_target, entry_price - target_points))

def resolve_signal_contract(signal):
    instruments = fetch_kite_instruments(signal["underlying"])
    candidates = candidate_kite_instruments(signal, instruments)
    if not candidates:
        return None

    ltp_by_symbol = fetch_kite_ltp(candidates)
    priced_candidates = [
        {
            "exchange": item["exchange"],
            "quantity": item["quantity"],
            "tradingsymbol": item["tradingsymbol"],
            "expiry": item["expiry"].isoformat(),
            "last_price": ltp_by_symbol[(item["exchange"], item["tradingsymbol"])],
        }
        for item in candidates
        if (item["exchange"], item["tradingsymbol"]) in ltp_by_symbol
    ]
    if not priced_candidates:
        return None

    target_price = last_price_from_range(signal["entry_range"])
    return min(priced_candidates, key=lambda item: abs(item["last_price"] - target_price))

def should_monitor_signal(entry_range, last_price):
    _, high_price = price_bounds(entry_range)
    return last_price > high_price + KITE_ENTRY_ABOVE_LADDER_TOLERANCE

def can_activate_monitored_signal(entry_range, last_price):
    low_price, high_price = price_bounds(entry_range)
    return low_price <= last_price <= high_price + KITE_ENTRY_ABOVE_LADDER_TOLERANCE

def contract_within_entry_window(signal, contract):
    low_price, high_price = price_bounds(signal["entry_range"])
    return low_price <= contract["last_price"] <= high_price + KITE_ENTRY_ABOVE_LADDER_TOLERANCE

def queue_signal_for_monitoring(signal, contract, event_id="system"):
    signal_key = (
        contract["exchange"],
        contract["tradingsymbol"],
        contract["quantity"],
        signal["action"],
        signal["entry_range"],
        signal["target_range"],
        signal["sl"],
    )
    if any(item["signal_key"] == signal_key for item in pending_signals):
        telegram_logger.info("[%s] Signal already pending for monitoring: %s", event_id, contract["tradingsymbol"])
        return

    pending_signals.append({
        "event_id": event_id,
        "signal_key": signal_key,
        "signal": signal,
        "exchange": contract["exchange"],
        "quantity": contract["quantity"],
        "tradingsymbol": contract["tradingsymbol"],
        "expiry": contract["expiry"],
    })
    telegram_logger.info(
        "[%s] Signal queued for monitoring: %s currently at %s for entry %s",
        event_id,
        contract["tradingsymbol"],
        contract["last_price"],
        signal["entry_range"],
    )

def register_pending_entry_batch(signal, exchange, quantity, tradingsymbol, expiry, entry_prices, event_id="system"):
    batch_key = (
        exchange,
        quantity,
        tradingsymbol,
        signal["action"],
        signal["entry_range"],
        signal["target_range"],
        signal["sl"],
    )
    if any(item["batch_key"] == batch_key for item in pending_entry_batches):
        telegram_logger.info("[%s] Entry batch already pending fill monitoring: %s", event_id, tradingsymbol)
        return

    pending_entry_batches.append({
        "event_id": event_id,
        "batch_key": batch_key,
        "signal": signal,
        "exchange": exchange,
        "quantity": quantity,
        "tradingsymbol": tradingsymbol,
        "expiry": expiry,
        "entry_prices": entry_prices,
        "processed_order_ids": [],
    })
    telegram_logger.info("[%s] Entry fill monitoring started for %s at prices %s", event_id, tradingsymbol, entry_prices)

def active_trade_key(signal, tradingsymbol):
    return (
        signal["underlying"],
        tradingsymbol,
        signal["action"],
        signal["entry_range"],
        signal["target_range"],
        signal["sl"],
    )

def register_active_trade(signal, exchange, quantity, tradingsymbol, expiry, entry_order_id, entry_price, target_price, event_id="system"):
    trade_key = active_trade_key(signal, tradingsymbol) + (entry_order_id,)
    if any(item["trade_key"] == trade_key for item in active_trades):
        telegram_logger.info("[%s] Trade already active for monitoring: %s", event_id, tradingsymbol)
        return

    active_trades.append({
        "event_id": event_id,
        "trade_key": trade_key,
        "signal": signal,
        "exchange": exchange,
        "quantity": quantity,
        "tradingsymbol": tradingsymbol,
        "expiry": expiry,
        "entry_order_id": entry_order_id,
        "entry_price": entry_price,
        "sl_price": first_price(signal["sl"]),
        "target_price": target_price,
    })
    telegram_logger.info(
        "[%s] Trade monitoring started for %s with SL %s and TARGET %s",
        event_id,
        tradingsymbol,
        signal["sl"],
        target_price,
    )

def trade_exit_reached(trade, current_price):
    action = trade["signal"]["action"]
    sl_price = trade["sl_price"]
    target_price = trade["target_price"]

    if action == "BUY":
        if current_price <= sl_price:
            return "SL"
        if current_price >= target_price:
            return "TARGET"
        return None

    if current_price >= sl_price:
        return "SL"
    if current_price <= target_price:
        return "TARGET"
    return None

async def send_chat_notification(message):
    try:
        await client.send_message(NOTIFICATION_CHAT, message)
        telegram_logger.info("Notification sent to %s: %s", NOTIFICATION_CHAT, message)
    except Exception:
        telegram_logger.exception("Notification send failed")

def notify_trade_closed(trade, exit_reason, current_price):
    message = (
        f"Trade closed for {trade['tradingsymbol']} | Reason: {exit_reason} | "
        f"Price: {current_price} | Entry: {trade['entry_price']} | "
        f"SL: {trade['signal']['sl']} | Target: {trade['signal']['target_range']}"
    )
    if client.loop.is_running():
        client.loop.create_task(send_chat_notification(message))
        return

    telegram_logger.warning("Notification skipped because Telegram client loop is not running: %s", message)

def process_pending_signals_once():
    if not pending_signals:
        return

    ltp_by_symbol = fetch_kite_ltp(pending_signals)
    remaining_signals = []

    for pending_signal in pending_signals:
        event_id = pending_signal.get("event_id", "system")
        exchange = pending_signal["exchange"]
        tradingsymbol = pending_signal["tradingsymbol"]
        current_price = ltp_by_symbol.get((exchange, tradingsymbol))
        if current_price is None:
            remaining_signals.append(pending_signal)
            continue

        signal = pending_signal["signal"]
        if can_activate_monitored_signal(signal["entry_range"], current_price):
            telegram_logger.info("[%s] Pending signal activated for %s at %s", event_id, tradingsymbol, current_price)
            execute_trade_pipeline(
                signal,
                contract={
                    "exchange": exchange,
                    "quantity": pending_signal["quantity"],
                    "tradingsymbol": tradingsymbol,
                    "expiry": pending_signal["expiry"],
                    "last_price": current_price,
                },
                allow_monitor_queue=False,
                event_id=event_id,
            )
            continue

        remaining_signals.append(pending_signal)

    pending_signals[:] = remaining_signals

def fetch_kite_orders():
    global kite_client
    if not kite_client:
        kite_client = get_kite_client()
    return kite_client.orders()

def extract_filled_order_price(order):
    for field in ("average_price", "price", "trigger_price"):
        value = order.get(field)
        if value not in (None, "", 0, 0.0):
            return float(value)
    return None

def order_matches_entry_batch(order, batch):
    if order.get("tradingsymbol") != batch["tradingsymbol"]:
        return False
    if (order.get("transaction_type") or "").upper() != batch["signal"]["action"]:
        return False
    if (order.get("status") or "").upper() != "COMPLETE":
        return False
    if int(order.get("quantity") or 0) != batch["quantity"]:
        return False

    filled_price = extract_filled_order_price(order)
    if filled_price is None:
        return False
    return any(abs(filled_price - entry_price) <= KITE_PRICE_MATCH_TOLERANCE for entry_price in batch["entry_prices"])

def process_pending_entry_batches_once():
    if not pending_entry_batches:
        return

    orders = fetch_kite_orders()
    remaining_batches = []

    for batch in pending_entry_batches:
        event_id = batch.get("event_id", "system")
        signal = batch["signal"]
        tradingsymbol = batch["tradingsymbol"]
        new_fills = []

        for order in orders:
            order_id = order.get("order_id")
            if not order_id or order_id in batch["processed_order_ids"]:
                continue
            if not order_matches_entry_batch(order, batch):
                continue
            new_fills.append(order)

        for order in new_fills:
            order_id = order.get("order_id")
            entry_price = normalize_order_price(extract_filled_order_price(order))
            target_points, calculation_lot_size = required_profit_capture_points(signal["underlying"], batch["quantity"])
            target_price = effective_target_price(signal, float(entry_price), batch["quantity"])
            exit_action = "SELL" if signal["action"] == "BUY" else "BUY"
            telegram_logger.info(
                "[%s] Entry order fill detected for %s. order_id=%s entry_price=%s",
                event_id,
                tradingsymbol,
                order_id,
                entry_price,
            )
            telegram_logger.info(
                "[%s] Derived target points for %s using daily profit target %s and configured lot size %s (order quantity %s): %s points -> exit target %s",
                event_id,
                signal["underlying"],
                DAILY_PROFIT_TARGET_RUPEES,
                calculation_lot_size,
                batch["quantity"],
                round(target_points, 2),
                target_price,
            )
            exit_payload = build_exit_payload(
                batch["exchange"],
                tradingsymbol,
                exit_action,
                batch["quantity"],
                first_price(signal["sl"]),
                target_price,
                float(entry_price),
            )
            exit_id = place_gtt_order(exit_payload, event_id=event_id, stage="EXIT", tradingsymbol=tradingsymbol)
            if not exit_id:
                telegram_logger.warning(
                    "[%s] Exit protective GTT placement failed for filled entry order %s on %s. Will retry on next monitor cycle.",
                    event_id,
                    order_id,
                    tradingsymbol,
                )
                continue

            batch["processed_order_ids"].append(order_id)
            register_active_trade(
                signal,
                batch["exchange"],
                batch["quantity"],
                tradingsymbol,
                batch["expiry"],
                order_id,
                float(entry_price),
                float(target_price),
                event_id=event_id,
            )

        if len(batch["processed_order_ids"]) < len(batch["entry_prices"]):
            remaining_batches.append(batch)

    pending_entry_batches[:] = remaining_batches

def process_active_trades_once():
    if not active_trades:
        return

    ltp_by_symbol = fetch_kite_ltp(active_trades)
    remaining_trades = []

    for trade in active_trades:
        event_id = trade.get("event_id", "system")
        exchange = trade["exchange"]
        tradingsymbol = trade["tradingsymbol"]
        current_price = ltp_by_symbol.get((exchange, tradingsymbol))
        if current_price is None:
            remaining_trades.append(trade)
            continue

        exit_reason = trade_exit_reached(trade, current_price)
        if exit_reason:
            telegram_logger.info("[%s] Trade monitoring closed for %s: %s hit at %s", event_id, tradingsymbol, exit_reason, current_price)
            notify_trade_closed(trade, exit_reason, current_price)
            continue

        remaining_trades.append(trade)

    active_trades[:] = remaining_trades

async def monitor_pending_signals():
    while True:
        try:
            process_pending_signals_once()
            process_pending_entry_batches_once()
            process_active_trades_once()
        except Exception:
            telegram_logger.exception("Pending signal monitor error")
        await asyncio.sleep(PENDING_SIGNAL_CHECK_INTERVAL_SECONDS)

def get_auth_headers():
    global kite_client
    if not kite_client:
        kite_client = get_kite_client()
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {kite_client.api_key}:{kite_client.access_token}",
    }

def fetch_kite_instruments(underlying):
    instruments_url = UNDERLYING_CONFIG[underlying]["instruments_url"]
    request = Request(instruments_url, headers=get_auth_headers())
    with urlopen(request, timeout=20) as response:
        content = response.read().decode("utf-8")
    return list(csv.DictReader(StringIO(content)))

def candidate_kite_instruments(signal, instruments):
    underlying = signal["underlying"]
    exchange = UNDERLYING_CONFIG[underlying]["exchange"]
    strike = float(signal["strike"])
    option_type = signal["option_type"]
    today = date.today()
    
    target_expiry_date = parse_message_expiry(signal.get("expiry_date_str"))
    
    candidates = []
    for instrument in instruments:
        if instrument.get("name") != underlying or instrument.get("instrument_type") != option_type:
            continue
        if float(instrument.get("strike") or 0) != strike:
            continue
        expiry = datetime.strptime(instrument["expiry"], "%Y-%m-%d").date()
        if expiry < today:
            continue
            
        # Filter down dynamically by explicit calendar target if defined
        if target_expiry_date and expiry != target_expiry_date:
            continue

        quantity = normalize_order_quantity(instrument.get("lot_size") or 0)
        if quantity <= 0:
            continue

        candidates.append({
            "exchange": exchange,
            "quantity": quantity,
            "tradingsymbol": instrument["tradingsymbol"],
            "expiry": expiry,
        })
    return sorted(candidates, key=lambda item: item["expiry"])

def fetch_kite_ltp(instruments):
    if not instruments:
        return {}
    instrument_keys = []
    seen_keys = set()
    for instrument in instruments:
        exchange = instrument["exchange"]
        tradingsymbol = instrument["tradingsymbol"]
        key = (exchange, tradingsymbol)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        instrument_keys.append(key)

    query = urlencode([("i", f"{exchange}:{tradingsymbol}") for exchange, tradingsymbol in instrument_keys])
    request = Request(f"{KITE_LTP_URL}?{query}", headers=get_auth_headers())
    with urlopen(request, timeout=20) as response:
        content = response.read().decode("utf-8")
    data = json.loads(content)["data"]
    return {
        (exchange, tradingsymbol): data[f"{exchange}:{tradingsymbol}"]["last_price"]
        for exchange, tradingsymbol in instrument_keys
        if f"{exchange}:{tradingsymbol}" in data
    }

def find_kite_contract_for_signal(signal):
    try:
        contract = resolve_signal_contract(signal)
        if not contract:
            return None

        if contract_within_entry_window(signal, contract):
            return contract
        return None
    except Exception:
        telegram_logger.exception("Error checking contracts")
        return None

def place_gtt_order(payload, event_id="system", stage="GTT", tradingsymbol="unknown"):
    try:
        data_bytes = urlencode(payload).encode("utf-8")
        request = Request(KITE_GTT_URL, data=data_bytes, headers=get_auth_headers(), method="POST")
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
            trigger_id = result.get("data", {}).get("trigger_id")
            if trigger_id:
                telegram_logger.info("[%s] %s GTT created for %s with trigger_id=%s", event_id, stage, tradingsymbol, trigger_id)
                return trigger_id

            telegram_logger.warning("[%s] %s GTT request returned no trigger_id for %s. Response: %s", event_id, stage, tradingsymbol, result)
            return None
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        telegram_logger.error(
            "[%s] %s GTT request failed for %s with HTTP %s. Response: %s | Payload: %s",
            event_id,
            stage,
            tradingsymbol,
            exc.code,
            error_body,
            payload,
        )
        return None
    except URLError as exc:
        telegram_logger.error(
            "[%s] %s GTT network failure for %s: %s | Payload: %s",
            event_id,
            stage,
            tradingsymbol,
            exc,
            payload,
        )
        return None
    except Exception:
        telegram_logger.exception("[%s] %s GTT unexpected failure for %s. Payload: %s", event_id, stage, tradingsymbol, payload)
        return None

def execute_trade_pipeline(signal, contract=None, allow_monitor_queue=True, event_id="system"):
    telegram_logger.info(
        "[%s] Executing trade pipeline for %s",
        event_id,
        signal_summary(signal),
    )
    contract = contract or resolve_signal_contract(signal)
    if not contract:
        telegram_logger.warning("[%s] Trade blocked: no matching contracts found for %s", event_id, signal_summary(signal))
        return

    telegram_logger.info(
        "[%s] Resolved contract %s expiry=%s ltp=%s quantity=%s",
        event_id,
        contract["tradingsymbol"],
        contract["expiry"],
        contract["last_price"],
        contract["quantity"],
    )

    if not contract_within_entry_window(signal, contract):
        if allow_monitor_queue:
            if should_monitor_signal(signal["entry_range"], contract["last_price"]):
                queue_signal_for_monitoring(signal, contract, event_id=event_id)
                return
        telegram_logger.warning(
            "[%s] Trade blocked: contract %s price %s is outside entry window %s",
            event_id,
            contract["tradingsymbol"],
            contract["last_price"],
            signal["entry_range"],
        )
        return

    exchange = contract["exchange"]
    quantity = contract["quantity"]
    tradingsymbol = contract['tradingsymbol']
    last_price = contract['last_price']
    entry_prices = build_entry_prices(signal["entry_range"], last_price)

    telegram_logger.info("[%s] Sending entry setup to exchange for %s with quantity %s and prices %s", event_id, tradingsymbol, quantity, entry_prices)
    entry_ids = []
    for entry_price in entry_prices:
        entry_payload = build_entry_payload(exchange, tradingsymbol, signal["action"], quantity, entry_price, last_price)
        entry_id = place_gtt_order(entry_payload, event_id=event_id, stage="ENTRY", tradingsymbol=tradingsymbol)
        if entry_id:
            entry_ids.append(entry_id)
        else:
            telegram_logger.warning("[%s] Entry GTT placement failed for %s at price %s", event_id, tradingsymbol, entry_price)

    if entry_ids:
        register_pending_entry_batch(signal, exchange, quantity, tradingsymbol, contract["expiry"], entry_prices, event_id=event_id)
        return

    telegram_logger.warning("[%s] No entry GTT orders were created for %s", event_id, tradingsymbol)

@client.on(events.NewMessage)
async def handler(event):
    try:
        # Get the current system time
        now = datetime.now()
        message_text = (event.raw_text or "").strip()
        chat = await event.get_chat()
        event_id = build_event_id(chat, event)
        chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or SOURCE_CHAT
        sender = await event.get_sender()
        sender_name = getattr(sender, "username", None) or getattr(sender, "first_name", None) or "unknown"

        telegram_logger.info(
            "[%s] Telegram message received from %s by %s: %s",
            event_id,
            chat_name,
            sender_name,
            message_text or "<empty>",
        )

        if not event_matches_source_chat(chat, SOURCE_CHAT):
            telegram_logger.info("[%s] Message ignored. Source chat %s does not match configured source %s.", event_id, chat_name, SOURCE_CHAT)
            return

        if LOG_ONLY_MODE:
            signal = extract_signal(message_text)
            telegram_logger.info(
                "[%s] LOG_ONLY_MODE enabled. Trading pipeline skipped for source chat message. Signal detected: %s",
                event_id,
                "yes" if signal else "no",
            )
            return

        current_time = now.time()
        
        # Define market monitoring boundaries (09:00:00 to 15:00:00)
        start_time = datetime.strptime("09:00:00", "%H:%M:%S").time()
        end_time = datetime.strptime("15:00:00", "%H:%M:%S").time()
        
        # Boundary 1: Check if the current time falls outside 9 AM - 3 PM
        if not (start_time <= current_time <= end_time):
            telegram_logger.info(
                "[%s] Message ignored. Current time (%s) is outside specified trading hours (09:00 - 15:00).",
                event_id,
                now.strftime("%H:%M:%S"),
            )
            return
            
        # Boundary 2: Check if today is a weekend (5 = Saturday, 6 = Sunday)
        if now.weekday() in [5, 6]:
            telegram_logger.info(
                "[%s] Message ignored. Today is a weekend (%s). Trading pipeline is locked.",
                event_id,
                now.strftime("%A"),
            )
            return
            
        signal = extract_signal(message_text)
        if not signal:
            telegram_logger.info("[%s] Message did not match signal format. No trade pipeline executed.", event_id)
            return
        telegram_logger.info("[%s] Parsed signal: %s", event_id, signal_summary(signal))
        execute_trade_pipeline(signal, event_id=event_id)
    except Exception:
        telegram_logger.exception("Loop error")

if __name__ == "__main__":
    telegram_logger.info(
        "Starting bot with source chat %s, log file %s, log-only mode %s, daily profit target %s, target lot sizes %s",
        SOURCE_CHAT,
        TELEGRAM_LOG_FILE,
        LOG_ONLY_MODE,
        DAILY_PROFIT_TARGET_RUPEES,
        profit_target_config_summary(),
    )
    for warning_message in CONFIG_WARNINGS:
        telegram_logger.warning("Configuration fallback applied: %s", warning_message)
    try:
        client.start()
        client.loop.run_until_complete(log_resolved_source_chat(client, SOURCE_CHAT))
        client.loop.create_task(monitor_pending_signals())
        client.run_until_disconnected()
    except AuthKeyDuplicatedError:
        log_telegram_session_reset_required(telegram_logger, "trading_session")
        raise
    except Exception:
        telegram_logger.exception("Bot startup failed")
        raise