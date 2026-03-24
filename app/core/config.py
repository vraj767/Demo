"""
app/core/config.py
──────────────────
Single source of truth for every env-var / constant.
Import from here everywhere — never use os.getenv() elsewhere.

CHANGES:
  - Added SHRINKME_API_KEY: your shrinkme.io API token.
    When set, every file link sent by the bot is shortened through
    shrinkme.io so you earn money from the ad page users see.
    If not set (empty string), links are sent as-is — no ads, no earnings.
    Get your token: shrinkme.io → Login → Tools → API
"""
import os
import time

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "8342249111:AAFO0jdOKvupldf-bHThvE2RWuJUXBevjgg").strip()
BOT_ENABLED      = os.getenv("BOT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
API_ID           = os.getenv("API_ID", "31855573").strip()
API_HASH         = os.getenv("API_HASH", "0716aeef77195fc167d2b7c19aeb5096").strip()
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "https://t.me/file_to_link_bot_fast_download").strip().lstrip("@")

# ── URL Shortener (shrinkme.io) ───────────────────────────────────────────────
# Set this in Railway Variables: SHRINKME_API_KEY = your_token_here
# Get your token: https://shrinkme.io → Login → Tools → API
# Leave empty ("") to disable — links will be sent as plain URLs.
SHRINKME_API_KEY = os.getenv("SHRINKME_API_KEY", "").strip()

# ── HTTP server ───────────────────────────────────────────────────────────────
PORT        = int(os.getenv("PORT", "8080"))
BASE_URL    = os.getenv("BASE_URL", "https://demo-production-1298.up.railway.app").rstrip("/")
FORCE_HTTPS = os.getenv("FORCE_HTTPS", "true").lower() in {"1", "true", "yes", "on"}

# ── Admin auth ────────────────────────────────────────────────────────────────
ADMIN_TOKEN    = os.getenv("ADMIN_TOKEN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Admin")

# ── File storage ──────────────────────────────────────────────────────────────
DEFAULT_TTL_SECONDS      = int(os.getenv("DEFAULT_TTL_SECONDS",      "21600"))  # 6 h
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300"))    # 5 min

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
