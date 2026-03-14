"""
app/api/routes/handlers.py
───────────────────────────
All FastAPI route handler functions.
The FastAPI app (main.py) registers these via include_router or direct decoration.

CHANGES:
  - All error conditions now return a styled dark-theme HTML error page
    instead of bare JSON. Each page shows: error code, friendly message,
    and a "← Go Back" button. Covers 404, 410, 429, 400, 503, 504, 416, 500.
  - MTProto readiness is checked BEFORE large_file_response() so errors can
    still return an HTML page (headers not yet sent to browser).
"""
import logging

import requests as req_lib
from fastapi import Request
from fastapi.responses import HTMLResponse

from app.core.auth import require_admin
from app.core.config import DOWNLOAD_LIMIT_PER_MIN, MAX_BOT_API_FILE_SIZE, STREAM_LIMIT_PER_MIN
from app.core.mtproto import LargeFileStreamError, has_mtproto_support, mtproto
from app.core.storage import (
    _storage_lock,
    check_rate_limit,
    file_storage,
    get_file_or_404,
    increment_stat,
    is_expired,
    now_ts,
)
from app.core.streaming import (
    large_file_response,
    resolve_telegram_url,
    small_file_response,
)
from app.core.urls import effective_base_url, update_detected_base_url
from app.api.pages.render import render_admin_page, render_file_page, render_stream_page

logger = logging.getLogger(__name__)


# ── Helper ────────────────────────────────────────────────────────────────────
def client_ip(request: Request) -> str:
    return (
        request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
        .split(",")[0].strip()
    )


# ── Error page builder ────────────────────────────────────────────────────────
def _error_page(code: int, title: str, message: str) -> HTMLResponse:
    """
    Returns a styled dark-theme HTML error page instead of bare JSON.
    Uses the same CSS variables as the file page so it looks consistent.
    """
    icon_map = {
        400: "⚠️", 404: "🔍", 410: "⏳", 416: "📐",
        429: "🚦", 500: "💥", 503: "⚙️", 504: "⏱️",
    }
    icon = icon_map.get(code, "❌")
    html = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{code} — {title}</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800&display=swap" rel="stylesheet">
    <style>
        *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
        :root{{
            --bg:#0d1117;--card:#161b22;--border:#30363d;
            --text:#e6edf3;--muted:#8b949e;--accent:#2f81f7;--orange:#f0883e;
        }}
        html,body{{
            background:var(--bg);color:var(--text);
            font-family:'Outfit',sans-serif;min-height:100vh;
            display:flex;align-items:center;justify-content:center;padding:1.5rem;
        }}
        .card{{
            background:var(--card);border:1px solid var(--border);
            border-radius:20px;padding:2.5rem 2rem;
            max-width:440px;width:100%;text-align:center;
        }}
        .icon{{font-size:3.5rem;margin-bottom:1rem;line-height:1}}
        .code{{
            font-size:.75rem;font-weight:700;letter-spacing:.12em;
            text-transform:uppercase;color:var(--muted);margin-bottom:.5rem;
        }}
        h1{{font-size:1.5rem;font-weight:800;margin-bottom:.75rem;color:var(--text)}}
        p{{font-size:.9rem;color:var(--muted);line-height:1.6;margin-bottom:1.75rem}}
        .btn{{
            display:inline-flex;align-items:center;gap:8px;
            background:rgba(47,129,247,.15);border:1px solid rgba(47,129,247,.3);
            color:#79c0ff;font-family:'Outfit',sans-serif;
            font-size:.88rem;font-weight:600;padding:10px 22px;
            border-radius:10px;text-decoration:none;
            transition:background .15s,transform .1s;cursor:pointer;
        }}
        .btn:hover{{background:rgba(47,129,247,.25);transform:translateY(-1px)}}
        .btn:active{{transform:translateY(0)}}
    </style>
</head>
<body>
<div class="card">
    <div class="icon">{icon}</div>
    <div class="code">Error {code}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    <a class="btn" href="javascript:history.back()">← Go Back</a>
</div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=code)


# ── /healthz ──────────────────────────────────────────────────────────────────
def healthz():
    return {"ok": True, "ts": now_ts()}


# ── / ─────────────────────────────────────────────────────────────────────────
def index(request: Request):
    update_detected_base_url(request)
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
    try:
        info = get_file_or_404(file_hash)
    except Exception as exc:
        code = getattr(exc, "status_code", 404)
        if code == 410:
            return _error_page(410, "Link Expired",
                "This download link has expired. Links are only valid for 6 hours. "
                "Please ask the sender to generate a fresh link.")
        return _error_page(404, "File Not Found",
            "This file doesn't exist or has already been removed. "
            "Please ask the sender to generate a new link.")
    return render_file_page(file_hash, info)


# ── /download/{hash} ──────────────────────────────────────────────────────────
async def download_file(file_hash: str, request: Request):
    update_detected_base_url(request)

    try:
        check_rate_limit(client_ip(request), "download", DOWNLOAD_LIMIT_PER_MIN)
    except Exception:
        return _error_page(429, "Too Many Requests",
            "You've made too many download requests. "
            "Please wait a minute and try again.")

    try:
        info = get_file_or_404(file_hash)
    except Exception as exc:
        code = getattr(exc, "status_code", 404)
        if code == 410:
            return _error_page(410, "Link Expired",
                "This download link has expired. Links are only valid for 6 hours. "
                "Please ask the sender to generate a fresh link.")
        return _error_page(404, "File Not Found",
            "This file doesn't exist or has already been removed.")

    file_size    = int(info.get("file_size") or 0)
    range_header = request.headers.get("Range")

    _is_initial = not range_header or range_header.strip().startswith("bytes=0-")
    if _is_initial:
        increment_stat(file_hash, "downloads", "total_downloads")

    try:
        if file_size <= MAX_BOT_API_FILE_SIZE:
            url, err = resolve_telegram_url(info["file_id"])
            if err:
                return _error_page(400, "Cannot Resolve File",
                    f"Could not retrieve the file from Telegram: {err}")
            return await small_file_response(
                url, info["file_name"], as_attachment=True, range_header=range_header
            )
        else:
            # Check MTProto is ready BEFORE returning StreamingResponse.
            # If we don't, FastAPI sends the response headers (Content-Disposition:
            # attachment) to the browser before the error fires — the browser starts
            # a download, gets 0 bytes, and shows "0B · Done". Checking here means
            # we can still return a proper error page.
            if has_mtproto_support():
                await mtproto.async_ensure_running()
            return large_file_response(info, as_attachment=True, range_header=range_header)

    except LargeFileStreamError as exc:
        msg = str(exc)
        logger.error("Large file stream error on download: %s", msg)
        if "not ready" in msg.lower() or "padding" in msg.lower():
            return _error_page(503, "Server Not Ready",
                "The streaming server is still starting up. "
                "Please wait a few seconds and try again.")
        if "range" in msg.lower():
            return _error_page(416, "Range Not Satisfiable",
                "The requested byte range is not valid for this file.")
        if "busy" in msg.lower():
            return _error_page(503, "Server Busy",
                "Too many concurrent downloads. Please try again in a moment.")
        return _error_page(500, "Streaming Error",
            f"An unexpected error occurred while streaming: {msg}")
    except req_lib.exceptions.Timeout:
        return _error_page(504, "Download Timed Out",
            "The connection to Telegram timed out. Please try again in a few seconds.")


# ── /media/{hash} ─────────────────────────────────────────────────────────────
async def media_file(file_hash: str, request: Request):
    update_detected_base_url(request)

    try:
        info = get_file_or_404(file_hash)
    except Exception as exc:
        code = getattr(exc, "status_code", 404)
        if code == 410:
            return _error_page(410, "Link Expired",
                "This stream link has expired. Links are only valid for 6 hours. "
                "Please ask the sender to generate a fresh link.")
        return _error_page(404, "File Not Found",
            "This file doesn't exist or has already been removed.")

    file_size    = int(info.get("file_size") or 0)
    range_header = request.headers.get("Range")

    try:
        if file_size <= MAX_BOT_API_FILE_SIZE:
            url, err = resolve_telegram_url(info["file_id"])
            if err:
                return _error_page(400, "Cannot Resolve File",
                    f"Could not retrieve the file from Telegram: {err}")
            return await small_file_response(
                url, info["file_name"], as_attachment=False, range_header=range_header
            )
        else:
            # Same MTProto readiness check as download_file above
            if has_mtproto_support():
                await mtproto.async_ensure_running()
            return large_file_response(info, as_attachment=False, range_header=range_header)

    except LargeFileStreamError as exc:
        msg = str(exc)
        logger.error("Large file stream error on media: %s", msg)
        if "not ready" in msg.lower() or "padding" in msg.lower():
            return _error_page(503, "Server Not Ready",
                "The streaming server is still starting up. "
                "Please wait a few seconds and try again.")
        if "range" in msg.lower():
            return _error_page(416, "Range Not Satisfiable",
                "The requested byte range is not valid for this file.")
        if "busy" in msg.lower():
            return _error_page(503, "Server Busy",
                "Too many concurrent downloads. Please try again in a moment.")
        return _error_page(500, "Streaming Error",
            f"An unexpected error occurred while streaming: {msg}")
    except req_lib.exceptions.Timeout:
        return _error_page(504, "Stream Timed Out",
            "The connection to Telegram timed out. Please try again in a few seconds.")


# ── /stream/{hash} ────────────────────────────────────────────────────────────
def stream_page(file_hash: str, request: Request):
    update_detected_base_url(request)

    try:
        check_rate_limit(client_ip(request), "stream", STREAM_LIMIT_PER_MIN)
    except Exception:
        return _error_page(429, "Too Many Requests",
            "You've opened too many streams. Please wait a minute and try again.")

    try:
        info = get_file_or_404(file_hash)
    except Exception as exc:
        code = getattr(exc, "status_code", 404)
        if code == 410:
            return _error_page(410, "Link Expired",
                "This stream link has expired. Links are only valid for 6 hours.")
        return _error_page(404, "File Not Found",
            "This file doesn't exist or has already been removed.")

    increment_stat(file_hash, "streams", "total_streams")
    return render_stream_page(file_hash, info)
