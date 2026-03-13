"""
app/core/storage.py
────────────────────
File registry, global stats, rate limiting, and cleanup loop.

CHANGES:
  3. Rate limiter memory leak fixed: _cleanup_loop now purges IPs from
     rate_limit_storage whose entire window has expired (no active timestamps
     in the last 60 s). Done inside the same _storage_lock as file cleanup.
  4. Persistent storage: file_storage is backed by a JSON file at STORAGE_PATH.
     - On startup: load all non-expired entries from disk.
     - On register_file: persist to disk immediately.
     - On cleanup: remove expired entries and persist.
     - Writes are atomic: write to .tmp then os.replace().
"""
import hashlib
import json
import logging
import os
import time
from threading import Lock, Thread
from typing import Dict, List, Optional

from fastapi import HTTPException

from app.core.config import (
    CLEANUP_INTERVAL_SECONDS,
    DEFAULT_TTL_SECONDS,
    STORAGE_PATH,
)

logger = logging.getLogger(__name__)

# ── Storage ───────────────────────────────────────────────────────────────────
file_storage:       Dict[str, dict]                    = {}
rate_limit_storage: Dict[str, Dict[str, List[float]]] = {}

global_stats: Dict[str, int] = {
    "total_files_uploaded": 0,
    "total_downloads":      0,
    "total_streams":        0,
}

_storage_lock    = Lock()
_cleanup_started = False


# ── Disk persistence helpers ──────────────────────────────────────────────────

def _ensure_data_dir() -> None:
    """Create the directory for STORAGE_PATH if it doesn't exist."""
    d = os.path.dirname(os.path.abspath(STORAGE_PATH))
    os.makedirs(d, exist_ok=True)


def _load_from_disk() -> None:
    """
    Load non-expired file entries from STORAGE_PATH into file_storage.
    Called once at startup — no lock needed since workers haven't started yet.
    """
    _ensure_data_dir()
    if not os.path.exists(STORAGE_PATH):
        logger.info("No existing storage file at %s — starting fresh", STORAGE_PATH)
        return
    try:
        with open(STORAGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = now_ts()
        loaded = 0
        for file_hash, info in data.items():
            if ts < int(info.get("expires_at", 0)):   # only load non-expired
                file_storage[file_hash] = info
                loaded += 1
        logger.info("Loaded %d active file(s) from disk (%s)", loaded, STORAGE_PATH)
    except Exception:
        logger.exception("Failed to load storage from disk — starting with empty storage")


def _save_to_disk() -> None:
    """
    Atomically write all non-expired file_storage entries to disk.
    Must be called while _storage_lock is held.
    Writes to a .tmp file first, then os.replace() for atomicity.
    """
    try:
        _ensure_data_dir()
        ts = now_ts()
        active = {k: v for k, v in file_storage.items()
                  if ts < int(v.get("expires_at", 0))}
        tmp_path = STORAGE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(active, f, ensure_ascii=False)
        os.replace(tmp_path, STORAGE_PATH)
    except Exception:
        logger.exception("Failed to persist storage to disk")


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
    raw        = f"{file_id}:{file_name}:{file_size}"
    file_hash  = hashlib.sha256(raw.encode()).hexdigest()[:16]
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
        _save_to_disk()   # persist immediately after every registration
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
            ts  = now_ts()
            now = time.time()
            with _storage_lock:
                # ── Remove expired files ──────────────────────────────────────
                expired_files = [
                    k for k, v in file_storage.items()
                    if ts >= int(v.get("expires_at", 0))
                ]
                for k in expired_files:
                    file_storage.pop(k, None)

                # ── Fix #3: Purge stale rate-limit IP entries ─────────────────
                # An IP is considered fully inactive when ALL timestamps across
                # ALL its action lists are older than 60 s (the rate-limit window).
                # Purging these prevents rate_limit_storage from growing forever.
                cutoff = now - 60
                stale_ips = [
                    ip for ip, actions in rate_limit_storage.items()
                    if all(
                        all(ts_val < cutoff for ts_val in ts_list)
                        for ts_list in actions.values()
                    )
                ]
                for ip in stale_ips:
                    rate_limit_storage.pop(ip, None)

                # ── Persist cleaned state to disk ─────────────────────────────
                if expired_files:
                    _save_to_disk()

            if expired_files:
                logger.info("Cleanup removed %d expired file(s)", len(expired_files))
            if stale_ips:
                logger.info("Cleanup purged %d stale rate-limit IP(s)", len(stale_ips))

        except Exception:
            logger.exception("Cleanup loop error")

        time.sleep(max(CLEANUP_INTERVAL_SECONDS, 300))


def start_cleanup_worker() -> None:
    global _cleanup_started
    if _cleanup_started:
        return
    _cleanup_started = True
    _load_from_disk()   # load persisted state before starting the worker
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
