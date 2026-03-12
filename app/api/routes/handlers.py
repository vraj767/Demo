"""
app/api/routes/handlers.py
───────────────────────────
All FastAPI route handler functions.
The FastAPI app (main.py) registers these via include_router or direct decoration.
"""
import logging

import requests as req_lib
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from app.core.auth import require_admin
from app.core.config import DOWNLOAD_LIMIT_PER_MIN, STREAM_LIMIT_PER_MIN
from app.core.mtproto import LargeFileStreamError
from app.core.storage import (
    check_rate_limit,
    file_storage,
    get_file_or_404,
    global_stats,
    increment_stat,
    is_expired,
    now_ts,
)
from app.core.streaming import (
    large_file_response,
    small_file_response,
    resolve_telegram_url,
)
from app.core.config import MAX_BOT_API_FILE_SIZE
from app.core.urls import effective_base_url, update_detected_base_url
from app.api.pages.render import render_admin_page, render_file_page, render_stream_page

logger = logging.getLogger(__name__)


def client_ip(request: Request) -> str:
    return (
        request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
        .split(",")[0].strip()
    )


# ── /healthz ──────────────────────────────────────────────────────────────────
def healthz():
    return {"ok": True, "ts": now_ts()}


# ── / ─────────────────────────────────────────────────────────────────────────
def index(request: Request):
    update_detected_base_url(request)
    from app.core.storage import _storage_lock
    from threading import Lock
    with _storage_lock:
        active = sum(1 for v in file_storage.values() if not is_expired(v))
    return f"""<html><body style="font-family:Arial;margin:2rem;">
        <h1>⚡ Media Bot</h1>
        <p>✅ Server Online | 📁 Active: {active} | 🌐 {effective_base_url()}</p>
    </body></html>"""


# ── /admin ────────────────────────────────────────────────────────────────────
def admin_dashboard(request: Request):
    require_admin(request)
    return HTMLResponse(
        content=render_admin_page(),
        headers={"X-Robots-Tag": "noindex, nofollow, noarchive"},
    )


# ── /file/{hash} ──────────────────────────────────────────────────────────────
def file_page(file_hash: str, request: Request):
    update_detected_base_url(request)
    info = get_file_or_404(file_hash)
    return render_file_page(file_hash, info)


# ── /download/{hash} ──────────────────────────────────────────────────────────
async def download_file(file_hash: str, request: Request):
    update_detected_base_url(request)
    check_rate_limit(client_ip(request), "download", DOWNLOAD_LIMIT_PER_MIN)

    info = get_file_or_404(file_hash)
    increment_stat(file_hash, "downloads", "total_downloads")

    file_size    = int(info.get("file_size") or 0)
    range_header = request.headers.get("Range")

    try:
        if file_size <= MAX_BOT_API_FILE_SIZE:
            url, err = resolve_telegram_url(info["file_id"])
            if err:
                raise HTTPException(status_code=400, detail=err)
            return await small_file_response(url, info["file_name"], as_attachment=True, range_header=range_header)
        return large_file_response(info, as_attachment=True, range_header=range_header)
    except req_lib.exceptions.Timeout as exc:
        raise HTTPException(status_code=504, detail="Download timeout") from exc
    except LargeFileStreamError as exc:
        msg  = str(exc)
        code = 503 if "not ready" in msg.lower() else (416 if "range" in msg.lower() else 500)
        raise HTTPException(status_code=code, detail=msg) from exc


# ── /media/{hash} ─────────────────────────────────────────────────────────────
async def media_file(file_hash: str, request: Request):
    update_detected_base_url(request)
    info         = get_file_or_404(file_hash)
    file_size    = int(info.get("file_size") or 0)
    range_header = request.headers.get("Range")

    try:
        if file_size <= MAX_BOT_API_FILE_SIZE:
            url, err = resolve_telegram_url(info["file_id"])
            if err:
                raise HTTPException(status_code=400, detail=err)
            return await small_file_response(url, info["file_name"], as_attachment=False, range_header=range_header)
        return large_file_response(info, as_attachment=False, range_header=range_header)
    except req_lib.exceptions.Timeout as exc:
        raise HTTPException(status_code=504, detail="Stream timeout") from exc
    except LargeFileStreamError as exc:
        msg  = str(exc)
        code = 503 if "not ready" in msg.lower() else (416 if "range" in msg.lower() else 500)
        raise HTTPException(status_code=code, detail=msg) from exc


# ── /stream/{hash} ────────────────────────────────────────────────────────────
def stream_page(file_hash: str, request: Request):
    update_detected_base_url(request)
    check_rate_limit(client_ip(request), "stream", STREAM_LIMIT_PER_MIN)
    info = get_file_or_404(file_hash)
    increment_stat(file_hash, "streams", "total_streams")
    return render_stream_page(file_hash, info)
