"""
app/core/urls.py
─────────────────
Base URL detection from incoming request headers.
"""
from threading import Lock

from fastapi import Request

from app.core.config import BASE_URL, FORCE_HTTPS

_detected_base_url = ""
_detect_lock       = Lock()


def _is_local(host: str) -> bool:
    return host.split(":", 1)[0].lower() in {"localhost", "127.0.0.1", "0.0.0.0"}


def update_detected_base_url(request: Request) -> None:
    global _detected_base_url
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if not host:
        return
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = proto or request.url.scheme
    if FORCE_HTTPS and not _is_local(host):
        scheme = "https"
    with _detect_lock:
        _detected_base_url = f"{scheme}://{host}".rstrip("/")


def effective_base_url() -> str:
    if BASE_URL:
        return BASE_URL
    with _detect_lock:
        return _detected_base_url
