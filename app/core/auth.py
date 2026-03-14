"""
app/core/auth.py
─────────────────
Admin authentication — bearer token or HTTP Basic.
"""
import base64
import binascii

from fastapi import HTTPException, Request

from app.core.config import ADMIN_PASSWORD, ADMIN_TOKEN, ADMIN_USERNAME


def require_admin(request: Request) -> None:
    """Raises 401 if the request is not admin-authenticated."""
    if not _is_authorized(request):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


def _is_authorized(request: Request) -> bool:
    token       = request.query_params.get("token") or request.headers.get("x-admin-token", "")
    auth_header = request.headers.get("authorization", "")

    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if ADMIN_TOKEN and token == ADMIN_TOKEN:
        return True

    if ADMIN_USERNAME and ADMIN_PASSWORD and auth_header.lower().startswith("basic "):
        raw = auth_header.split(" ", 1)[1].strip()
        try:
            decoded = base64.b64decode(raw).decode()
        except (binascii.Error, UnicodeDecodeError):
            return False
        if ":" not in decoded:
            return False
        u, p = decoded.split(":", 1)
        return u == ADMIN_USERNAME and p == ADMIN_PASSWORD

    return False
