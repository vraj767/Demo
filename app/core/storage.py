"""
app/core/storage.py
────────────────────
In-memory file registry, global stats, rate limiting, and cleanup loop.
All state lives here — import from here, never duplicate dicts elsewhere.

CHANGES:
  - Added user_storage: tracks every user who has ever used the bot.
    Required for /broadcast (send message to all users) and
    /stats (total user count).
  - Added register_user() — called from file_handler on every file upload.
  - Added get_all_user_ids() — returns list of all known user IDs.
"""
import hashlib
import logging
import time
from threading import Lock, Thread
from typing import Dict, List, Optional, Set

from fastapi import HTTPException

from app.core.config import (
    CLEANUP_INTERVAL_SECONDS,
    DEFAULT_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

# ── Storage ───────────────────────────────────────────────────────────────────
file_storage:       Dict[str, dict]                    = {}
rate_limit_storage: Dict[str, Dict[str, List[float]]] = {}

# Tracks every user who has sent a file to the bot.
# Key: user_id (int), Value: dict with first_name and last_seen timestamp.
user_storage: Dict[int, dict] = {}

global_stats: Dict[str, int] = {
    "total_files_uploaded": 0,
    "total_downloads":      0,
    "total_streams":        0,
}

_storage_lock    = Lock()
_cleanup_started = False


# ── User helpers ──────────────────────────────────────────────────────────────
def register_user(user_id: int, first_name: str) -> None:
    """
    Record that this user has used the bot.
    Called every time a user successfully uploads a file.
    Thread-safe — uses the same _storage_lock as file operations.
    """
    with _storage_lock:
        user_storage[user_id] = {
            "first_name": first_name,
            "last_seen":  int(time.time()),
        }


def get_all_user_ids() -> List[int]:
    """Return a snapshot list of all known user IDs. Thread-safe."""
    with _storage_lock:
        return list(user_storage.keys())


def get_user_count() -> int:
    """Return total number of unique users. Thread-safe."""
    with _storage_lock:
        return len(user_storage)


# ── File helpers ──────────────────────────────────────────────────────────────
def now_ts() -> int:
    return int(time.time())


def register_file(
    file_id:    str,
    file_name:  str,
    file_size:  int,
    is_video:   bool = False,
    chat_id:    Optional[int] = None,
    message_id: Optional[int] = None,
    ttl:        int = DEFAULT_TTL_SECONDS,
) -> str:
    raw       = f"{file_id}:{file_name}:{file_size}"
    file_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    expires_at = now_ts() + ttl
    with _storage_lock:
        file_storage[file_hash] = {
            "file_id":    file_id,
            "file_name":  file_name,
            "file_size":  file_size,
            "is_video":   is_video,
            "chat_id":    chat_id,
            "message_id": message_id,
            "expires_at": expires_at,
            "downloads":  0,
            "streams":    0,
        }
        global_stats["total_files_uploaded"] += 1
    logger.info("Registered file hash=%s name=%s size=%d", file_hash, file_name, file_size)
    return file_hash


def is_expired(info: dict) -> bool:
    return now_ts() >= int(info.get("expires_at", 0))


def get_file_or_404(file_hash: str) -> dict:
    with _storage_lock:
        info = file_storage.get(file_hash)
        if not info:
            raise HTTPException(status_code=404, detail="File not found")
        if is_expired(info):
            raise HTTPException(status_code=410, detail="File expired")
        return info


def increment_stat(file_hash: str, field: str, global_key: str) -> None:
    with _storage_lock:
        info = file_storage.get(file_hash)
        if info:
            info[field] = int(info.get(field, 0)) + 1
        global_stats[global_key] += 1


# ── Cleanup worker ─────────────────────────────────────────────────────────────
def _cleanup_loop() -> None:
    while True:
        try:
            ts = now_ts()
            with _storage_lock:
                keys = [k for k, v in file_storage.items() if ts >= int(v.get("expires_at", 0))]
                for k in keys:
                    file_storage.pop(k, None)
            if keys:
                logger.info("Cleanup removed %d expired files", len(keys))
        except Exception:
            logger.exception("Cleanup loop error")
        time.sleep(max(CLEANUP_INTERVAL_SECONDS, 300))


def start_cleanup_worker() -> None:
    global _cleanup_started
    if _cleanup_started:
        return
    _cleanup_started = True
    Thread(target=_cleanup_loop, daemon=True, name="cleanup").start()


# ── Rate limiter ───────────────────────────────────────────────────────────────
def check_rate_limit(ip: str, action: str, limit: int, window: int = 60) -> None:
    now = time.time()
    with _storage_lock:
        per_ip     = rate_limit_storage.setdefault(ip, {"download": [], "stream": []})
        entries    = per_ip.setdefault(action, [])
        cutoff     = now - window
        entries[:] = [ts for ts in entries if ts >= cutoff]
        if len(entries) >= limit:
            raise HTTPException(status_code=429, detail=f"Rate limit exceeded for {action}")
        entries.append(now)
