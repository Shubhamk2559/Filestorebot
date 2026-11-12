"""
config.py
Only the bare minimum lives in environment variables. Everything else
(channels, captions, timers, admins...) is configured from the Telegram admin
panel and stored in MongoDB.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = _required("BOT_TOKEN")
MONGO_URI = _required("MONGO_URI")
OWNER_ID = int(_required("OWNER_ID"))

# Kept for Pyrogram compatibility; python-telegram-bot does not need them.
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")

# Public https URL of this deployment, no trailing slash (e.g. https://myapp.koyeb.app). Needed for Stream/Download links.
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

DB_NAME = os.getenv("DB_NAME", "filesharebot")
PORT = int(os.getenv("PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
