"""
Repo-wide config shared by scanner/, signal_engine/, and execution/.

Loads trading-platform/.env once (without overriding any env vars a
subsystem already set for itself, e.g. scanner/app.py loading its own
local .env before importing this module) and exposes the credentials
every subsystem needs to talk to the same Kite Connect app and the same
Telegram bot.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
CACHE_DIR = REPO_ROOT / "cache"
ACCESS_TOKEN_FILE = CACHE_DIR / "access_token.json"


def _load_dotenv() -> None:
    if not ENV_PATH.is_file():
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

KITE_API_KEY = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
