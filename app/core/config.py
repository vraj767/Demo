"""
app/core/config.py
──────────────────
Single source of truth for every env-var / constant.
Import from here everywhere — never use os.getenv() elsewhere.

CHANGES:
  1. All secrets (BOT_TOKEN, API_ID, API_HASH, BASE_URL, ADMIN_USERNAME,
     ADMIN_PASSWORD) now default to "" — must be set as Railway env vars.
  2. REQUIRED_CHANNEL sanitized: any format works:
       "https://t.me/mychannel" → "mychannel"
       "t.me/mychannel"         → "mychannel"
       "@mychannel"             → "mychannel"
       "mychannel"              → "mychannel"
  3. Added STORAGE_PATH for persistent JSON file storage.
"""
import os
import re
import time


def _sanitize_channel(raw: str) -> str:
    """Strip any URL prefix and leading @ → bare username for get_chat_member."""
    s = raw.strip()
    s = re.sub(r'^https?://', '', s)   # remove https:// or http://
    s = re.sub(r'^t\.me/', '', s)      # remove t.me/
    s = s.lstrip('@')                  # remove leading @
    return s


# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "").strip()
BOT_ENABLED      = os.getenv("BOT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
API_ID           = os.getenv("API_ID", "").strip()
API_HASH         = os.getenv("API_HASH", "").strip()
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "").strip()
REQUIRED_CHANNEL = _sanitize_channel(os.getenv("REQUIRED_CHANNEL", ""))

# ── HTTP server ───────────────────────────────────────────────────────────────
PORT        = int(os.getenv("PORT", "8080"))
BASE_URL    = os.getenv("BASE_URL", "").rstrip("/")
FORCE_HTTPS = os.getenv("FORCE_HTTPS", "true").lower() in {"1", "true", "yes", "on"}

# ── Admin auth ────────────────────────────────────────────────────────────────
ADMIN_TOKEN    = os.getenv("ADMIN_TOKEN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# ── File storage ──────────────────────────────────────────────────────────────
DEFAULT_TTL_SECONDS      = int(os.getenv("DEFAULT_TTL_SECONDS",      "21600"))  # 6 h
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300"))    # 5 min
STORAGE_PATH             = os.getenv("STORAGE_PATH", "./data/storage.json")

# ── Download engine ───────────────────────────────────────────────────────────
TELETHON_REQUEST_SIZE    = int(os.getenv("TELETHON_REQUEST_SIZE", str(8 * 1024 * 1024)))
STREAM_CHUNK_SIZE        = int(os.getenv("STREAM_CHUNK_SIZE",     str(8 * 1024 * 1024)))
MTPROTO_QUEUE_CHUNKS     = int(os.getenv("MTPROTO_QUEUE_CHUNKS",  "8"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "16"))
MAX_BOT_API_FILE_SIZE    = 20 * 1024 * 1024   # Telegram Bot API hard limit

# ── Rate limits ───────────────────────────────────────────────────────────────
DOWNLOAD_LIMIT_PER_MIN = int(os.getenv("DOWNLOAD_LIMIT_PER_MIN", "30"))
STREAM_LIMIT_PER_MIN   = int(os.getenv("STREAM_LIMIT_PER_MIN",   "30"))

# ── Runtime ───────────────────────────────────────────────────────────────────
START_TIME = time.time()
