import csv
import asyncio
import json
import logging
import os
import re
import calendar
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from kiteconnect import KiteTicker
from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError
from telethon.sessions import SQLiteSession
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

def start_telegram_client_with_retry(client, logger, session_name, attempts=3, delay_seconds=2):
    for attempt in range(1, attempts + 1):
        try:
            client.start()
            return
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == attempts:
                raise
            logger.warning(
                "Telegram session '%s' is temporarily locked. Retrying client startup in %s seconds (%s/%s).",
                session_name,
                delay_seconds,
                attempt,
                attempts,
            )
            time.sleep(delay_seconds)

SOURCE_CHAT = 'Option Playbook by SK'
NOTIFICATION_CHAT = 't.me/testalgotradinganand'
KITE_PRODUCT = "NRML"
KITE_ORDER_TYPE = "LIMIT"
KITE_PRICE_MATCH_TOLERANCE = 1.0
KITE_ENTRY_ABOVE_LADDER_TOLERANCE = 5.0
DAILY_PROFIT_TARGET_RUPEES = get_env_float("DAILY_PROFIT_TARGET_RUPEES", 500.0)
PENDING_SIGNAL_CHECK_INTERVAL_SECONDS = 5
PENDING_TRADE_EXPIRY_MINUTES = get_env_int("PENDING_TRADE_EXPIRY_MINUTES", 120)
ENTRY_ORDER_RETRY_COOLDOWN_SECONDS = get_env_int("ENTRY_ORDER_RETRY_COOLDOWN_SECONDS", 10)
ENTRY_ORDER_MAX_RETRIES = get_env_int("ENTRY_ORDER_MAX_RETRIES", 3)
EXIT_GTT_RETRY_COOLDOWN_SECONDS = get_env_int("EXIT_GTT_RETRY_COOLDOWN_SECONDS", 5)
ACTIVE_TRADE_RECONCILE_INTERVAL_SECONDS = get_env_int("ACTIVE_TRADE_RECONCILE_INTERVAL_SECONDS", 30)
MAX_ENTRY_SLIPPAGE = 3.0
KITE_LTP_URL = "https://api.kite.trade/quote/ltp"
KITE_GTT_URL = "https://api.kite.trade/gtt/triggers"
TELEGRAM_LOG_FILE = os.getenv("BOT_LOG_FILE", "bot_output.log")
PENDING_TRADES_FILE = os.getenv("PENDING_TRADES_FILE", "pending_trades.json")
ACTIVE_TRADES_FILE = os.getenv("ACTIVE_TRADES_FILE", "active_trades.json")
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
RANGE_REGEX = re.compile(rf"\b(?:range|rng|{ENTRY_KEYWORD_REGEX})\b\s*(?:only\s+)?(?:is|at|@|:|-)?\s*(?P<value>{PRICE_VALUE_REGEX})", re.IGNORECASE)
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

class _WALSQLiteSession(SQLiteSession):
    """SQLiteSession that enables WAL mode and a 30-second busy timeout on every
    connection so concurrent readers/writers don't immediately raise
    'database is locked'."""

    def _cursor(self):
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.filename,
                check_same_thread=False,
                timeout=30,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        return self._conn.cursor()


kite_client = None
kite_ticker = None
ticker_connected = False
client = TelegramClient(_WALSQLiteSession('trading_session_new'), API_ID, API_HASH, catch_up=True)
pending_trades = []
active_trades = []
state_lock = threading.RLock()

WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
WAITING_FOR_REENTRY = "WAITING_FOR_REENTRY"
ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
ENTRY_ORDER_PENDING = "ENTRY_ORDER_PENDING"
EXIT_GTT_PENDING = "EXIT_GTT_PENDING"
ACTIVE = "ACTIVE"
EXPIRED = "EXPIRED"

NON_TERMINAL_PENDING_STATUSES = {
    WAITING_FOR_ENTRY,
    ENTRY_TRIGGERED,
    ENTRY_ORDER_PENDING,
    EXIT_GTT_PENDING,
}

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
        if not action and re.search(rf"\b{ENTRY_KEYWORD_REGEX}\b", text, re.IGNORECASE):
            action = "BUY"
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
        # Fetch the latest message to register the channel in Telethon's update
        # tracking feed.  For private/unnamed channels Telethon only starts
        # delivering UpdateNewChannelMessage events after the channel has been
        # queried at least once in the current session.
        await client.get_messages(resolved_reference, limit=1)
        telegram_logger.info(
            "Source chat %s registered in update feed — new messages will be received.",
            source_chat,
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

def summarize_trade_for_log(trade):
    return {
        "event_id": trade.get("event_id", "system"),
        "exchange": trade.get("exchange"),
        "tradingsymbol": trade.get("tradingsymbol"),
        "instrument_token": trade.get("instrument_token"),
        "quantity": trade.get("quantity"),
        "entry_price": trade.get("entry_price"),
        "stop_loss": trade.get("stop_loss"),
        "target_range": trade.get("target_range") or trade.get("targets"),
        "status": trade.get("status"),
    }

def effective_target_price(signal, entry_price, quantity):
    configured_target = last_price_from_range(signal["target_range"])
    target_points, _ = required_profit_capture_points(signal["underlying"], quantity)
    if signal["action"] == "BUY":
        return normalize_order_price(min(configured_target, entry_price + target_points))
    return normalize_order_price(max(configured_target, entry_price - target_points))

def parse_iso_datetime(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)

def parse_optional_iso_datetime(value):
    if not value:
        return None
    return parse_iso_datetime(value)

def next_retry_time(delay_seconds):
    return (datetime.now() + timedelta(seconds=delay_seconds)).isoformat()

def retry_ready(timestamp, now=None):
    retry_at = parse_optional_iso_datetime(timestamp)
    return retry_at is None or (now or datetime.now()) >= retry_at

def transition_trade_status(trade, new_status, reason):
    old_status = trade.get("status")
    if old_status == new_status:
        return

    trade["status"] = new_status
    telegram_logger.info(
        "[%s] Trade state %s -> %s for %s (%s)",
        trade.get("event_id", "system"),
        old_status,
        new_status,
        trade.get("tradingsymbol") or trade.get("symbol"),
        reason,
    )

def remove_pending_trade(trade):
    removed = False
    with state_lock:
        if trade in pending_trades:
            pending_trades.remove(trade)
            removed = True

    if removed:
        persist_pending_trades()
        unsubscribe_inactive_pending_tokens([trade["instrument_token"]])

def expire_pending_trade(trade, reason):
    transition_trade_status(trade, EXPIRED, reason)
    remove_pending_trade(trade)

def pending_trade_expiry_time(trade):
    return parse_iso_datetime(trade["created_time"]) + timedelta(minutes=PENDING_TRADE_EXPIRY_MINUTES)

def pending_trade_is_expired(trade, now=None):
    return (now or datetime.now()) >= pending_trade_expiry_time(trade)

def serialize_trade_for_storage(trade):
    stored_trade = dict(trade)
    stored_trade["created_time"] = parse_iso_datetime(stored_trade["created_time"]).isoformat()
    return stored_trade

def serialize_active_trade_for_storage(trade):
    stored_trade = dict(trade)
    if isinstance(stored_trade.get("trade_key"), tuple):
        stored_trade["trade_key"] = list(stored_trade["trade_key"])
    return stored_trade

def persist_pending_trades():
    with state_lock:
        serializable_trades = [
            serialize_trade_for_storage(trade)
            for trade in pending_trades
            if trade.get("status") in NON_TERMINAL_PENDING_STATUSES
        ]

    temp_path = f"{PENDING_TRADES_FILE}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(serializable_trades, file, indent=2, sort_keys=True)
    os.replace(temp_path, PENDING_TRADES_FILE)

def persist_active_trades():
    with state_lock:
        serializable_trades = [serialize_active_trade_for_storage(trade) for trade in active_trades]

    temp_path = f"{ACTIVE_TRADES_FILE}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(serializable_trades, file, indent=2, sort_keys=True)
    os.replace(temp_path, ACTIVE_TRADES_FILE)

def pending_trade_key(signal, contract, entry_price, telegram_message_id):
    return (
        contract["exchange"],
        contract["tradingsymbol"],
        contract["instrument_token"],
        signal["action"],
        entry_price,
        signal["sl"],
        signal["target_range"],
        telegram_message_id,
    )

def active_streaming_tokens():
    with state_lock:
        return sorted({
            int(trade["instrument_token"])
            for trade in pending_trades
            if trade.get("status") in (
    WAITING_FOR_ENTRY,
    WAITING_FOR_REENTRY,)

        } | {
            int(trade["instrument_token"])
            for trade in active_trades
            if trade.get("instrument_token")
        })

def subscribe_pending_trade_tokens(tokens=None):
    global kite_ticker
    tokens = tokens or active_streaming_tokens()
    if not tokens or not kite_ticker or not ticker_connected:
        return

    try:
        kite_ticker.subscribe(tokens)
        kite_ticker.set_mode(kite_ticker.MODE_LTP, tokens)
        telegram_logger.info("Subscribed KiteTicker LTP stream for instrument tokens: %s", tokens)
    except Exception:
        telegram_logger.exception("KiteTicker subscription failed for instrument tokens: %s", tokens)

def unsubscribe_inactive_pending_tokens(tokens):
    global kite_ticker
    if not tokens or not kite_ticker or not ticker_connected:
        return

    active_tokens = set(active_streaming_tokens())
    stale_tokens = sorted({int(token) for token in tokens if int(token) not in active_tokens})
    if not stale_tokens:
        return

    try:
        kite_ticker.unsubscribe(stale_tokens)
        telegram_logger.info("Unsubscribed KiteTicker stream for inactive tokens: %s", stale_tokens)
    except Exception:
        telegram_logger.exception("KiteTicker unsubscribe failed for tokens: %s", stale_tokens)

def register_pending_trade(signal, contract, entry_price, event_id="system", telegram_message_id=None):
    trade_key = pending_trade_key(signal, contract, entry_price, telegram_message_id)
    created_time = datetime.now()

    with state_lock:
        if any(item.get("trade_key") == trade_key and item.get("status") == WAITING_FOR_ENTRY for item in pending_trades):
            telegram_logger.info("[%s] Pending trade already exists for %s at entry %s", event_id, contract["tradingsymbol"], entry_price)
            return None

        pending_trade = {
            "event_id": event_id,
            "trade_key": trade_key,
            "signal": signal,
            "symbol": contract["tradingsymbol"],
            "tradingsymbol": contract["tradingsymbol"],
            "exchange": contract["exchange"],
            "instrument_token": int(contract["instrument_token"]),
            "entry_price": float(entry_price),
            "stop_loss": first_price(signal["sl"]),
            "targets": signal["target_range"],
            "target_range": signal["target_range"],
            "quantity": contract["quantity"],
            "telegram_message_id": telegram_message_id,
            "created_time": created_time.isoformat(),
            "status": WAITING_FOR_ENTRY,
            "expiry": contract["expiry"],
            "entry_attempt_count": 0,
            "next_entry_attempt_at": None,
            "last_entry_error": None,
            "entry_order_id": None,
            "entry_fill_price": None,
            "entry_fill_confirmed_at": None,
            "exit_gtt_id": None,
            "next_exit_gtt_attempt_at": None,
            "waiting_for_reentry": False,
        }
        pending_trades.append(pending_trade)

    persist_pending_trades()
    subscribe_pending_trade_tokens([pending_trade["instrument_token"]])
    telegram_logger.info(
        "[%s] Pending trade created: symbol=%s instrument_token=%s entry=%s stop_loss=%s targets=%s quantity=%s telegram_message_id=%s created_time=%s status=%s",
        event_id,
        pending_trade["symbol"],
        pending_trade["instrument_token"],
        pending_trade["entry_price"],
        pending_trade["stop_loss"],
        pending_trade["targets"],
        pending_trade["quantity"],
        telegram_message_id,
        pending_trade["created_time"],
        pending_trade["status"],
    )
    return pending_trade

def load_pending_trades():
    if not os.path.exists(PENDING_TRADES_FILE):
        return

    try:
        with open(PENDING_TRADES_FILE, "r", encoding="utf-8") as file:
            stored_trades = json.load(file)
    except Exception:
        telegram_logger.exception("Unable to load pending trades from %s", PENDING_TRADES_FILE)
        return

    now = datetime.now()
    recovered_count = 0
    for trade in stored_trades:
        if trade.get("status") not in NON_TERMINAL_PENDING_STATUSES:
            continue
        if trade.get("status") in {WAITING_FOR_ENTRY, ENTRY_TRIGGERED} and pending_trade_is_expired(trade, now=now):
            trade["status"] = EXPIRED
            telegram_logger.info(
                "[%s] Trade expired during recovery: %s entry=%s created_time=%s",
                trade.get("event_id", "system"),
                trade.get("symbol") or trade.get("tradingsymbol"),
                trade.get("entry_price"),
                trade.get("created_time"),
            )
            continue

        trade["instrument_token"] = int(trade["instrument_token"])
        trade["entry_price"] = float(trade["entry_price"])
        trade["quantity"] = normalize_order_quantity(trade["quantity"])
        if isinstance(trade.get("trade_key"), list):
            trade["trade_key"] = tuple(trade["trade_key"])
        trade["entry_attempt_count"] = int(trade.get("entry_attempt_count") or 0)
        trade.setdefault("next_entry_attempt_at", None)
        trade.setdefault("last_entry_error", None)
        trade.setdefault("entry_order_id", None)
        trade.setdefault("entry_fill_price", None)
        trade.setdefault("entry_fill_confirmed_at", None)
        trade.setdefault("exit_gtt_id", None)
        trade.setdefault("next_exit_gtt_attempt_at", None)
        with state_lock:
            pending_trades.append(trade)
        recovered_count += 1
        telegram_logger.info(
            "[%s] Trade recovered after restart: symbol=%s instrument_token=%s entry=%s expires_at=%s",
            trade.get("event_id", "system"),
            trade.get("symbol") or trade.get("tradingsymbol"),
            trade["instrument_token"],
            trade["entry_price"],
            pending_trade_expiry_time(trade).isoformat(),
        )

    persist_pending_trades()
    if recovered_count:
        subscribe_pending_trade_tokens()

def load_active_trades():
    if not os.path.exists(ACTIVE_TRADES_FILE):
        return

    try:
        with open(ACTIVE_TRADES_FILE, "r", encoding="utf-8") as file:
            stored_trades = json.load(file)
    except Exception:
        telegram_logger.exception("Unable to load active trades from %s", ACTIVE_TRADES_FILE)
        return

    recovered_count = 0
    for trade in stored_trades:
        trade["quantity"] = normalize_order_quantity(trade["quantity"])
        trade["entry_price"] = float(trade["entry_price"])
        trade["sl_price"] = float(trade["sl_price"])
        trade["target_price"] = float(trade["target_price"])
        if trade.get("instrument_token") is not None:
            trade["instrument_token"] = int(trade["instrument_token"])
        if isinstance(trade.get("trade_key"), list):
            trade["trade_key"] = tuple(trade["trade_key"])
        with state_lock:
            active_trades.append(trade)
        recovered_count += 1
        telegram_logger.info(
            "[%s] Active trade recovered after restart: symbol=%s instrument_token=%s entry=%s target=%s exit_gtt_id=%s",
            trade.get("event_id", "system"),
            trade.get("tradingsymbol"),
            trade.get("instrument_token"),
            trade.get("entry_price"),
            trade.get("target_price"),
            trade.get("exit_gtt_id"),
        )

    if recovered_count:
        persist_active_trades()

def place_market_entry_order(trade):
    global kite_client
    if not kite_client:
        kite_client = get_kite_client()

    order_request = {
        "variety": kite_client.VARIETY_REGULAR,
        "exchange": trade["exchange"],
        "tradingsymbol": trade["tradingsymbol"],
        "transaction_type": kite_client.TRANSACTION_TYPE_BUY,
        "quantity": trade["quantity"],
        "product": KITE_PRODUCT,
        "order_type": kite_client.ORDER_TYPE_MARKET,
        "validity": kite_client.VALIDITY_DAY,
        "market_protection": -1,
    }

    try:
        order_id = kite_client.place_order(**order_request)
    except Exception as exc:
        telegram_logger.exception(
            "[%s] BUY placement API error for %s: %s | order_request=%s | trade=%s",
            trade.get("event_id", "system"),
            trade["tradingsymbol"],
            exc,
            order_request,
            summarize_trade_for_log(trade),
        )
        raise

    telegram_logger.info("[%s] BUY placed for %s. order_id=%s quantity=%s", trade.get("event_id", "system"), trade["tradingsymbol"], order_id, trade["quantity"])
    return order_id

def finalize_executed_entry(trade, order):
    signal = trade["signal"]
    order_id = order.get("order_id") or trade.get("entry_order_id")
    entry_price = normalize_order_price(extract_filled_order_price(order) or trade.get("entry_price"))
    target_points, calculation_lot_size = required_profit_capture_points(signal["underlying"], trade["quantity"])
    target_price = effective_target_price(signal, float(entry_price), trade["quantity"])
    exit_action = "SELL"
    event_id = trade.get("event_id", "system")

    telegram_logger.info(
        "[%s] BUY executed for %s. order_id=%s entry_price=%s",
        event_id,
        trade["tradingsymbol"],
        order_id,
        entry_price,
    )
    telegram_logger.info(
        "[%s] Derived target points for %s using daily profit target %s and configured lot size %s (order quantity %s): %s points -> exit target %s",
        event_id,
        signal["underlying"],
        DAILY_PROFIT_TARGET_RUPEES,
        calculation_lot_size,
        trade["quantity"],
        round(target_points, 2),
        target_price,
    )

    exit_payload = build_exit_payload(
        trade["exchange"],
        trade["tradingsymbol"],
        exit_action,
        trade["quantity"],
        first_price(signal["sl"]),
        target_price,
        float(entry_price),
    )
    exit_id = place_gtt_order(exit_payload, event_id=event_id, stage="EXIT", tradingsymbol=trade["tradingsymbol"])
    trade["entry_fill_price"] = float(entry_price)
    trade["entry_fill_confirmed_at"] = datetime.now().isoformat()
    if not exit_id:
        transition_trade_status(trade, EXIT_GTT_PENDING, "buy filled; awaiting exit protection")
        trade["next_exit_gtt_attempt_at"] = next_retry_time(EXIT_GTT_RETRY_COOLDOWN_SECONDS)
        telegram_logger.warning(
            "[%s] Exit protective GTT placement failed for filled BUY order %s on %s. Retrying after cooldown.",
            event_id,
            order_id,
            trade["tradingsymbol"],
        )
        return False

    transition_trade_status(trade, ACTIVE, "exit protection confirmed")
    trade["exit_gtt_id"] = exit_id
    trade["next_exit_gtt_attempt_at"] = None
    register_active_trade(
        signal,
        trade["exchange"],
        trade["quantity"],
        trade["tradingsymbol"],
        trade["expiry"],
        order_id,
        float(entry_price),
        float(target_price),
        instrument_token=trade["instrument_token"],
        exit_gtt_id=exit_id,
        event_id=event_id,
    )
    remove_pending_trade(trade)
    return True

def fetch_kite_order_history(order_id):
    global kite_client
    if not kite_client:
        kite_client = get_kite_client()
    return kite_client.order_history(order_id)

def fetch_kite_positions():
    global kite_client
    if not kite_client:
        kite_client = get_kite_client()
    return kite_client.positions()

def fetch_gtt_trigger(trigger_id):
    request = Request(f"{KITE_GTT_URL}/{trigger_id}", headers=get_auth_headers())
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))

def latest_order_history_entry(order_history):
    if not order_history:
        return None
    return order_history[-1]

def attempt_entry_order_placement(trade):
    if trade.get("status") != ENTRY_TRIGGERED:
        return

    now = datetime.now()
    event_id = trade.get("event_id", "system")
    attempt_count = int(trade.get("entry_attempt_count") or 0)
    if attempt_count >= ENTRY_ORDER_MAX_RETRIES:
        expire_pending_trade(trade, "entry order retries exhausted")
        telegram_logger.error(
            "[%s] Entry order retries exhausted for %s after %s attempts",
            event_id,
            trade["tradingsymbol"],
            attempt_count,
        )
        return

    if not retry_ready(trade.get("next_entry_attempt_at"), now=now):
        return

    trade["entry_attempt_count"] = attempt_count + 1
    try:
        trade["entry_order_id"] = place_market_entry_order(trade)
    except Exception as exc:
        trade["last_entry_error"] = str(exc)
        trade["next_entry_attempt_at"] = next_retry_time(ENTRY_ORDER_RETRY_COOLDOWN_SECONDS)
        telegram_logger.warning(
            "[%s] BUY placement attempt %s/%s failed for %s. Cooling down until %s",
            event_id,
            trade["entry_attempt_count"],
            ENTRY_ORDER_MAX_RETRIES,
            trade["tradingsymbol"],
            trade["next_entry_attempt_at"],
        )
        if trade["entry_attempt_count"] >= ENTRY_ORDER_MAX_RETRIES:
            expire_pending_trade(trade, "entry order retries exhausted")
        else:
            persist_pending_trades()
        return

    trade["last_entry_error"] = None
    trade["next_entry_attempt_at"] = None
    transition_trade_status(trade, ENTRY_ORDER_PENDING, "entry order placed")
    persist_pending_trades()
    refresh_entry_order_status(trade)

def refresh_entry_order_status(trade):
    if trade.get("status") != ENTRY_ORDER_PENDING or not trade.get("entry_order_id"):
        return

    event_id = trade.get("event_id", "system")
    try:
        latest_order = latest_order_history_entry(fetch_kite_order_history(trade["entry_order_id"]))
    except Exception:
        telegram_logger.exception(
            "[%s] Unable to fetch order history for %s order_id=%s",
            event_id,
            trade["tradingsymbol"],
            trade.get("entry_order_id"),
        )
        return

    if not latest_order:
        telegram_logger.warning(
            "[%s] Empty order history returned for %s order_id=%s",
            event_id,
            trade["tradingsymbol"],
            trade.get("entry_order_id"),
        )
        return

    status = (latest_order.get("status") or "").upper()
    if status == "COMPLETE":
        finalize_executed_entry(trade, latest_order)
        persist_pending_trades()
        return

    if status in {"REJECTED", "CANCELLED"}:
        telegram_logger.warning(
            "[%s] BUY order did not execute for %s. order_id=%s status=%s",
            event_id,
            trade["tradingsymbol"],
            trade.get("entry_order_id"),
            status,
        )
        expire_pending_trade(trade, f"entry order {status.lower()}")
        return

    telegram_logger.info(
        "[%s] BUY order still pending for %s. order_id=%s status=%s",
        event_id,
        trade["tradingsymbol"],
        trade.get("entry_order_id"),
        status,
    )

def retry_exit_gtt_for_filled_trade(trade):
    if trade.get("status") != EXIT_GTT_PENDING:
        return

    if not retry_ready(trade.get("next_exit_gtt_attempt_at")):
        return

    order_snapshot = {
        "order_id": trade.get("entry_order_id"),
        "average_price": trade.get("entry_fill_price") or trade.get("entry_price"),
    }
    if finalize_executed_entry(trade, order_snapshot):
        persist_pending_trades()
        return

    persist_pending_trades()

def advance_pending_trade_state(trade):
    status = trade.get("status")
    if status == ENTRY_TRIGGERED:
        attempt_entry_order_placement(trade)
        return
    if status == ENTRY_ORDER_PENDING:
        refresh_entry_order_status(trade)
        return
    if status == EXIT_GTT_PENDING:
        retry_exit_gtt_for_filled_trade(trade)

def active_trade_position_key(trade):
    return trade["exchange"], trade["tradingsymbol"]

def build_open_positions_by_key(positions_response):
    open_positions = {}
    for position in positions_response.get("net", []):
        quantity = int(position.get("quantity") or 0)
        if quantity == 0:
            continue
        position_key = ((position.get("exchange") or "").upper(), position.get("tradingsymbol"))
        open_positions[position_key] = position
    return open_positions

def gtt_trigger_status(payload):
    data = payload.get("data") or {}
    return (data.get("status") or payload.get("status") or "").upper()

def active_trade_reconcile_reason(gtt_payload):
    if not gtt_payload:
        return "POSITION_FLAT"

    status = gtt_trigger_status(gtt_payload)
    if status == "TRIGGERED":
        return "EXIT_GTT_TRIGGERED"
    if status:
        return f"POSITION_FLAT_{status}"
    return "POSITION_FLAT"

def reconcile_active_trade(trade, open_positions_by_key):
    position = open_positions_by_key.get(active_trade_position_key(trade))
    if position and int(position.get("quantity") or 0) != 0:
        return False

    gtt_payload = None
    exit_gtt_id = trade.get("exit_gtt_id")
    if exit_gtt_id:
        try:
            gtt_payload = fetch_gtt_trigger(exit_gtt_id)
        except HTTPError as exc:
            telegram_logger.warning(
                "[%s] Unable to fetch exit GTT %s for %s during reconciliation. HTTP %s",
                trade.get("event_id", "system"),
                exit_gtt_id,
                trade["tradingsymbol"],
                exc.code,
            )
        except URLError as exc:
            telegram_logger.warning(
                "[%s] Network failure fetching exit GTT %s for %s during reconciliation: %s",
                trade.get("event_id", "system"),
                exit_gtt_id,
                trade["tradingsymbol"],
                exc,
            )
        except Exception:
            telegram_logger.exception(
                "[%s] Unexpected failure fetching exit GTT %s for %s during reconciliation",
                trade.get("event_id", "system"),
                exit_gtt_id,
                trade["tradingsymbol"],
            )

    exit_reason = active_trade_reconcile_reason(gtt_payload)
    current_price = float((position or {}).get("last_price") or trade["target_price"])
    telegram_logger.info(
        "[%s] Active trade reconciled closed for %s. reason=%s exit_gtt_id=%s",
        trade.get("event_id", "system"),
        trade["tradingsymbol"],
        exit_reason,
        exit_gtt_id,
    )
    notify_trade_closed(trade, exit_reason, current_price)
    remove_active_trade(trade)
    return True

def process_pending_trade_tick(trade, ltp):
    event_id = trade.get("event_id", "system")

    with state_lock:
        status = trade.get("status")

        if status not in (WAITING_FOR_ENTRY, WAITING_FOR_REENTRY):
            return

        entry_price = float(trade["entry_price"])

        # ---------------------------------------------------------
        # WAITING_FOR_ENTRY
        # ---------------------------------------------------------
        if status == WAITING_FOR_ENTRY:

            # Price has not reached entry yet
            if ltp < entry_price:
                return

            slippage = ltp - entry_price

            # Price has moved too far above entry.
            # Wait for price to return instead of chasing.
            if slippage > MAX_ENTRY_SLIPPAGE:

                transition_trade_status(
                    trade,
                    WAITING_FOR_REENTRY,
                    f"LTP {ltp} exceeded allowed slippage. Waiting for re-entry."
                )

                telegram_logger.info(
                    "[%s] LTP=%s Entry=%s Slippage=%s exceeds max=%s. "
                    "Waiting for re-entry.",
                    event_id,
                    ltp,
                    entry_price,
                    round(slippage, 2),
                    MAX_ENTRY_SLIPPAGE,
                )

                persist_pending_trades()
                return

        # ---------------------------------------------------------
        # WAITING_FOR_REENTRY
        # ---------------------------------------------------------
        elif status == WAITING_FOR_REENTRY:

            # Still above entry. Continue waiting.
            if ltp > entry_price:
                return

            telegram_logger.info(
                "[%s] Price returned to entry. "
                "LTP=%s Entry=%s. Attempting BUY.",
                event_id,
                ltp,
                entry_price,
            )

        # ---------------------------------------------------------
        # Trigger BUY
        # ---------------------------------------------------------
        transition_trade_status(
            trade,
            ENTRY_TRIGGERED,
            f"LTP reached entry at {ltp}"
        )

    telegram_logger.info(
        "[%s] Entry triggered for %s. LTP=%s Entry=%s Status=%s",
        event_id,
        trade["tradingsymbol"],
        ltp,
        trade["entry_price"],
        trade["status"],
    )

    persist_pending_trades()
    advance_pending_trade_state(trade)

def process_ticker_ticks(ticks):
    expired_tokens = set()
    closed_tokens = set()
    for tick in ticks:
        instrument_token = int(tick.get("instrument_token") or 0)
        ltp = tick.get("last_price")
        if not instrument_token or ltp is None:
            continue

        with state_lock:
            matching_trades = [
                trade
                for trade in pending_trades
                if int(trade["instrument_token"]) == instrument_token and trade.get("status")in (
    WAITING_FOR_ENTRY,
    WAITING_FOR_REENTRY,)
            ]
            matching_active_trades = [
                trade
                for trade in active_trades
                if int(trade.get("instrument_token") or 0) == instrument_token
            ]

        telegram_logger.info(
            "Tick received: instrument_token=%s ltp=%s pending_trades=%s active_trades=%s",
            instrument_token,
            ltp,
            len(matching_trades),
            len(matching_active_trades),
        )

        for trade in matching_trades:
            if pending_trade_is_expired(trade):
                trade["status"] = EXPIRED
                expired_tokens.add(instrument_token)
                telegram_logger.info(
                    "[%s] Trade expired: %s entry=%s created_time=%s expiry_minutes=%s",
                    trade.get("event_id", "system"),
                    trade["tradingsymbol"],
                    trade["entry_price"],
                    trade["created_time"],
                    PENDING_TRADE_EXPIRY_MINUTES,
                )
                persist_pending_trades()
                continue

            process_pending_trade_tick(trade, float(ltp))

        for trade in matching_active_trades:
            exit_reason = trade_exit_reached(trade, float(ltp))
            if not exit_reason:
                continue

            telegram_logger.info(
                "[%s] Trade monitoring closed for %s: %s hit at %s",
                trade.get("event_id", "system"),
                trade["tradingsymbol"],
                exit_reason,
                ltp,
            )
            notify_trade_closed(trade, exit_reason, float(ltp))
            remove_active_trade(trade)
            closed_tokens.add(instrument_token)

    if expired_tokens:
        unsubscribe_inactive_pending_tokens(expired_tokens)
    if closed_tokens:
        unsubscribe_inactive_pending_tokens(closed_tokens)

def start_kite_ticker():
    global kite_client, kite_ticker
    if not kite_client:
        kite_client = get_kite_client()

    kite_ticker = KiteTicker(kite_client.api_key, kite_client.access_token)

    def on_connect(ws, response):
        global ticker_connected
        ticker_connected = True
        telegram_logger.info("KiteTicker connected: %s", response)
        subscribe_pending_trade_tokens()

    def on_ticks(ws, ticks):
        try:
            process_ticker_ticks(ticks)
        except Exception:
            telegram_logger.exception("KiteTicker tick processing failed")

    def on_close(ws, code, reason):
        global ticker_connected
        ticker_connected = False
        telegram_logger.warning("KiteTicker closed: code=%s reason=%s", code, reason)

    def on_error(ws, code, reason):
        telegram_logger.error("KiteTicker error: code=%s reason=%s", code, reason)

    def on_reconnect(ws, attempts_count):
        telegram_logger.warning("KiteTicker reconnect attempt: %s", attempts_count)

    def on_noreconnect(ws):
        telegram_logger.error("KiteTicker reconnect attempts exhausted")

    kite_ticker.on_connect = on_connect
    kite_ticker.on_ticks = on_ticks
    kite_ticker.on_close = on_close
    kite_ticker.on_error = on_error
    kite_ticker.on_reconnect = on_reconnect
    kite_ticker.on_noreconnect = on_noreconnect
    kite_ticker.connect(threaded=True)

def resolve_signal_contract(signal):
    instruments = fetch_kite_instruments(signal["underlying"])
    candidates = candidate_kite_instruments(signal, instruments)
    if not candidates:
        return None

    ltp_by_symbol = fetch_kite_ltp(candidates)
    priced_candidates = [
        {
            "exchange": item["exchange"],
            "instrument_token": item["instrument_token"],
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

def active_trade_key(signal, tradingsymbol):
    return (
        signal["underlying"],
        tradingsymbol,
        signal["action"],
        signal["entry_range"],
        signal["target_range"],
        signal["sl"],
    )

def remove_active_trade(trade):
    removed = False
    with state_lock:
        if trade in active_trades:
            active_trades.remove(trade)
            removed = True

    if removed:
        persist_active_trades()
        unsubscribe_inactive_pending_tokens([trade["instrument_token"]])

def register_active_trade(signal, exchange, quantity, tradingsymbol, expiry, entry_order_id, entry_price, target_price, instrument_token=None, exit_gtt_id=None, event_id="system"):
    trade_key = active_trade_key(signal, tradingsymbol) + (entry_order_id,)
    if any(item["trade_key"] == trade_key for item in active_trades):
        telegram_logger.info("[%s] Trade already active for monitoring: %s", event_id, tradingsymbol)
        return

    active_trade = {
        "event_id": event_id,
        "trade_key": trade_key,
        "signal": signal,
        "exchange": exchange,
        "instrument_token": instrument_token,
        "quantity": quantity,
        "tradingsymbol": tradingsymbol,
        "expiry": expiry,
        "entry_order_id": entry_order_id,
        "entry_price": entry_price,
        "sl_price": first_price(signal["sl"]),
        "target_price": target_price,
        "exit_gtt_id": exit_gtt_id,
    }
    active_trades.append(active_trade)
    persist_active_trades()
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

def extract_filled_order_price(order):
    for field in ("average_price", "price", "trigger_price"):
        value = order.get(field)
        if value not in (None, "", 0, 0.0):
            return float(value)
    return None

def process_pending_trade_states_once():
    with state_lock:
        trades_to_advance = [
            trade
            for trade in pending_trades
            if trade.get("status") in {ENTRY_TRIGGERED, ENTRY_ORDER_PENDING, EXIT_GTT_PENDING}
        ]

    for trade in trades_to_advance:
        advance_pending_trade_state(trade)

def reconcile_active_trades_once():
    with state_lock:
        trades_to_reconcile = list(active_trades)

    if not trades_to_reconcile:
        return

    try:
        open_positions_by_key = build_open_positions_by_key(fetch_kite_positions())
    except Exception:
        telegram_logger.exception("Unable to fetch positions during active trade reconciliation")
        return

    for trade in trades_to_reconcile:
        reconcile_active_trade(trade, open_positions_by_key)

def expire_pending_trades_once():
    now = datetime.now()
    expired_trades = []
    with state_lock:
        for trade in pending_trades:
            if trade.get("status") not in {WAITING_FOR_ENTRY, ENTRY_TRIGGERED}:
                continue
            if not pending_trade_is_expired(trade, now=now):
                continue
            transition_trade_status(trade, EXPIRED, "pending trade expired before entry order placement")
            expired_trades.append(trade)

    if not expired_trades:
        return

    for trade in expired_trades:
        telegram_logger.info(
            "[%s] Trade expired: %s entry=%s created_time=%s expiry_minutes=%s",
            trade.get("event_id", "system"),
            trade["tradingsymbol"],
            trade["entry_price"],
            trade["created_time"],
            PENDING_TRADE_EXPIRY_MINUTES,
        )

    with state_lock:
        for trade in expired_trades:
            if trade in pending_trades:
                pending_trades.remove(trade)

    persist_pending_trades()
    unsubscribe_inactive_pending_tokens([trade["instrument_token"] for trade in expired_trades])

async def monitor_pending_signals():
    last_active_trade_reconcile = datetime.min
    while True:
        try:
            expire_pending_trades_once()
            process_pending_trade_states_once()
            now = datetime.now()
            if (now - last_active_trade_reconcile).total_seconds() >= ACTIVE_TRADE_RECONCILE_INTERVAL_SECONDS:
                reconcile_active_trades_once()
                last_active_trade_reconcile = now
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
            "instrument_token": int(instrument["instrument_token"]),
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
        telegram_logger.exception(
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
        telegram_logger.exception(
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

def execute_trade_pipeline(signal, contract=None, event_id="system", telegram_message_id=None):
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
        "[%s] Resolved contract %s expiry=%s ltp=%s quantity=%s instrument_token=%s",
        event_id,
        contract["tradingsymbol"],
        contract["expiry"],
        contract["last_price"],
        contract["quantity"],
        contract["instrument_token"],
    )

    entry_price = normalize_order_price(last_price_from_range(signal["entry_range"]))
    register_pending_trade(
        signal,
        contract,
        entry_price,
        event_id=event_id,
        telegram_message_id=telegram_message_id,
    )

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
        end_time = datetime.strptime("15:30:00", "%H:%M:%S").time()
        
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
        execute_trade_pipeline(signal, event_id=event_id, telegram_message_id=getattr(event, "id", None))
    except Exception:
        telegram_logger.exception("Loop error")

if __name__ == "__main__":
    telegram_logger.info(
        "Starting bot with source chat %s, log file %s, log-only mode %s, daily profit target %s, target lot sizes %s, pending trade expiry minutes %s, pending trades file %s",
        SOURCE_CHAT,
        TELEGRAM_LOG_FILE,
        LOG_ONLY_MODE,
        DAILY_PROFIT_TARGET_RUPEES,
        profit_target_config_summary(),
        PENDING_TRADE_EXPIRY_MINUTES,
        PENDING_TRADES_FILE,
    )
    for warning_message in CONFIG_WARNINGS:
        telegram_logger.warning("Configuration fallback applied: %s", warning_message)
    try:
        load_pending_trades()
        load_active_trades()
        if not LOG_ONLY_MODE:
            start_kite_ticker()
        start_telegram_client_with_retry(client, telegram_logger, "trading_session_new")
        client.loop.run_until_complete(log_resolved_source_chat(client, SOURCE_CHAT))
        if not LOG_ONLY_MODE:
            reconcile_active_trades_once()
        client.loop.create_task(monitor_pending_signals())
        client.run_until_disconnected()
    except AuthKeyDuplicatedError:
        log_telegram_session_reset_required(telegram_logger, "trading_session")
        raise
    except Exception:
        telegram_logger.exception("Bot startup failed")
        raise
