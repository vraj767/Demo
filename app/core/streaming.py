"""
app/core/streaming.py
──────────────────────
HTTP response builders shared by download, media, and stream routes.
"""
import asyncio
import logging
import os
import re
from typing import Dict, Optional

import requests
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.core.config import MAX_BOT_API_FILE_SIZE, STREAM_CHUNK_SIZE, BOT_TOKEN
from app.core.mtproto import LargeFileStreamError, iter_large_file

logger = logging.getLogger(__name__)

# ── MIME map ──────────────────────────────────────────────────────────────────
_MIME_MAP: Dict[str, str] = {
    ".mp4": "video/mp4",         ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",  ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",   ".webm": "video/webm",
    ".flv": "video/x-flv",       ".wmv": "video/x-ms-wmv",
    ".ts":  "video/mp2t",        ".m2ts": "video/mp2t",
    ".mp3": "audio/mpeg",        ".aac": "audio/aac",
    ".ogg": "audio/ogg",         ".flac": "audio/flac",
    ".wav": "audio/wav",         ".pdf": "application/pdf",
}


def mime_for_file(file_name: str) -> str:
    ext = os.path.splitext(file_name.lower())[1]
    return _MIME_MAP.get(ext, "application/octet-stream")


# ── Range header parser ───────────────────────────────────────────────────────
def parse_range_header(range_header: Optional[str], file_size: int):
    if not range_header or file_size <= 0:
        return None
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not m:
        raise LargeFileStreamError("Invalid Range header")
    s_raw, e_raw = m.groups()
    if not s_raw and not e_raw:
        raise LargeFileStreamError("Invalid Range header")
    if s_raw and e_raw:
        start, end = int(s_raw), int(e_raw)
    elif s_raw:
        start, end = int(s_raw), file_size - 1
    else:
        suf = int(e_raw)
        start, end = max(file_size - suf, 0), file_size - 1
    if start < 0 or end >= file_size or start > end:
        raise LargeFileStreamError("Range not satisfiable")
    return start, end


# ── Telegram Bot API URL resolver (≤ 20 MB files) ───────────────────────────
def resolve_telegram_url(file_id: str):
    if not BOT_TOKEN:
        return None, "BOT_TOKEN is missing"
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=20,
        )
    except requests.exceptions.RequestException as exc:
        return None, f"Could not reach Telegram API: {exc}"
    if r.status_code != 200:
        return None, f"Telegram API returned HTTP {r.status_code}"
    data = r.json()
    if not data.get("ok"):
        return None, data.get("description", "Unknown error")
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{data['result']['file_path']}", None


# ── Response builders ─────────────────────────────────────────────────────────
async def small_file_response(
    telegram_url:  str,
    file_name:     str,
    as_attachment: bool,
    range_header:  Optional[str],
) -> StreamingResponse:
    hdrs = {"Range": range_header} if range_header else None
    loop = asyncio.get_running_loop()
    upstream = await loop.run_in_executor(
        None,
        lambda: requests.get(telegram_url, headers=hdrs, stream=True, timeout=300),
    )
    upstream.raise_for_status()

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    headers: Dict[str, str] = {
        "Content-Type":  "application/octet-stream",
        "Cache-Control": "no-cache",
        "Accept-Ranges": "bytes",
    }
    if as_attachment:
        headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
    if upstream.headers.get("Content-Range"):
        headers["Content-Range"]  = upstream.headers["Content-Range"]
    if upstream.headers.get("Content-Length"):
        headers["Content-Length"] = upstream.headers["Content-Length"]

    return StreamingResponse(generate(), status_code=upstream.status_code, headers=headers)


def large_file_response(
    info:          dict,
    as_attachment: bool,
    range_header:  Optional[str],
) -> StreamingResponse:
    chat_id    = info.get("chat_id")
    message_id = info.get("message_id")
    file_name  = info["file_name"]
    file_size  = int(info.get("file_size") or 0)

    if chat_id is None or message_id is None:
        raise HTTPException(status_code=500, detail="Missing chat/message IDs for large-file streaming")

    range_tuple = parse_range_header(range_header, file_size)
    status_code = 200

    headers: Dict[str, str] = {
        "Content-Type":           mime_for_file(file_name),
        "Cache-Control":          "no-cache",
        "Accept-Ranges":          "bytes",
        "X-Content-Type-Options": "nosniff",
    }
    if as_attachment:
        headers["Content-Disposition"] = f'attachment; filename="{file_name}"'

    if range_tuple:
        start, end  = range_tuple
        length      = end - start + 1
        status_code = 206
        headers["Content-Range"]  = f"bytes {start}-{end}/{file_size}"
        headers["Content-Length"] = str(length)
        body = iter_large_file(chat_id, message_id, offset=start, length=length)
    else:
        if file_size > 0:
            headers["Content-Length"] = str(file_size)
        body = iter_large_file(chat_id, message_id)

    return StreamingResponse(body, status_code=status_code, headers=headers)


def build_file_response(
    info:          dict,
    as_attachment: bool,
    range_header:  Optional[str],
):
    """Route dispatch: small files via Bot API, large files via MTProto."""
    file_size = int(info.get("file_size") or 0)
    if file_size <= MAX_BOT_API_FILE_SIZE:
        url, err = resolve_telegram_url(info["file_id"])
        if err:
            raise HTTPException(status_code=400, detail=err)
        import asyncio
        # This is a sync wrapper — callers that are async should await directly
        raise _NeedsSmallFileResponse(url, info["file_name"], as_attachment, range_header)
    return large_file_response(info, as_attachment, range_header)


class _NeedsSmallFileResponse(Exception):
    """Internal signal: caller must await small_file_response()."""
    def __init__(self, url, file_name, as_attachment, range_header):
        self.url          = url
        self.file_name    = file_name
        self.as_attachment = as_attachment
        self.range_header = range_header
