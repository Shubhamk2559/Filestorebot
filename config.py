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

# Cloudflare Worker that makes every share link permanent (see cloudflare-worker/worker.js). Optional.
WORKER_URL = os.getenv("WORKER_URL", "").strip().rstrip("/")
WORKER_SECRET = os.getenv("WORKER_SECRET", "").strip()

# Invisible padding so message bubbles are as wide as their inline buttons (0 = off). See utils/padding.py
PAD_WIDTH = int(os.getenv("PAD_WIDTH", "34") or 0)

DB_NAME = os.getenv("DB_NAME", "filesharebot")
PORT = int(os.getenv("PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Storage quota of your MongoDB plan in MB (Atlas free M0 = 512) - used for the "storage left" bar in Stats
MONGO_LIMIT_MB = float(os.getenv("MONGO_LIMIT_MB", "512"))
