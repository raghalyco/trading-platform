import asyncio
import logging
import os

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError

from index import API_HASH, API_ID, SOURCE_CHAT, TELEGRAM_LOG_FILE, extract_signal, log_telegram_session_reset_required, resolve_chat_reference


HISTORY_CHAT = os.getenv("HISTORY_CHAT", SOURCE_CHAT)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
HISTORY_SESSION = os.getenv("HISTORY_SESSION", "trading_session")
HISTORY_SIGNALS_ONLY = os.getenv("HISTORY_SIGNALS_ONLY", "true").strip().lower() in {"1", "true", "yes", "on"}


history_logger = logging.getLogger("telegram_history")
if not history_logger.handlers:
    history_logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(TELEGRAM_LOG_FILE, encoding="utf-8")
    stream_handler = logging.StreamHandler()

    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    history_logger.addHandler(file_handler)
    history_logger.addHandler(stream_handler)
    history_logger.propagate = False


async def main():
    history_logger.info(
        "History fetch started for chat %s using session %s with limit %s. Signals only: %s",
        HISTORY_CHAT,
        HISTORY_SESSION,
        HISTORY_LIMIT,
        HISTORY_SIGNALS_ONLY,
    )

    matched_messages = 0
    async with TelegramClient(HISTORY_SESSION, API_ID, API_HASH) as client:
        history_entity = await resolve_chat_reference(client, HISTORY_CHAT)
        messages = []
        async for message in client.iter_messages(history_entity, limit=HISTORY_LIMIT):
            messages.append(message)

        if not messages:
            history_logger.info("No historical messages found for chat %s", HISTORY_CHAT)
            return

        for message in reversed(messages):
            message_text = (message.raw_text or "<empty>").strip()
            parsed_signal = extract_signal(message_text)
            if HISTORY_SIGNALS_ONLY and not parsed_signal:
                continue

            sender = await message.get_sender()
            sender_name = getattr(sender, "username", None) or getattr(sender, "first_name", None) or "unknown"
            if parsed_signal:
                matched_messages += 1
                history_logger.info(
                    "[history:%s] %s | %s | SIGNAL | %s | parsed=%s",
                    message.id,
                    message.date,
                    sender_name,
                    message_text,
                    parsed_signal,
                )
                continue

            history_logger.info(
                "[history:%s] %s | %s | %s",
                message.id,
                message.date,
                sender_name,
                message_text,
            )

    history_logger.info(
        "History fetch completed for chat %s. Messages scanned: %s | Matching signals logged: %s",
        HISTORY_CHAT,
        len(messages),
        matched_messages,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AuthKeyDuplicatedError:
        log_telegram_session_reset_required(history_logger, HISTORY_SESSION)
        raise