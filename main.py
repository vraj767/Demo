"""
main.py
────────
FastAPI application factory.
Wires together: lifespan, middleware, all route handlers.

BUGS FIXED IN THIS VERSION:
  1. NameError "_prewarm_mtproto is not defined":
     asyncio.create_task(_prewarm_mtproto()) was called but the function was
     never defined in this file. This caused MTProto to NEVER start in the
     background, so every first streaming request had to wait 10-15 seconds
     for cold MTProto startup → VLC/browser stalled and buffered endlessly.
     Fix: Define _prewarm_mtproto() as an async function before lifespan.

  2. Healthcheck timeout still 100s in railway.toml:
     Fixed to 300s as a safety net for slow DC migrations on fresh deploys.

  3. "Post-deploy not started" is NORMAL — Railway only runs post-deploy if you
     configure a post-deploy command. It is NOT an error.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from threading import Event, Thread
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import FORCE_HTTPS, PORT
from app.core.mtproto import has_mtproto_support, mtproto
from app.core.storage import start_cleanup_worker
from app.core.urls import update_detected_base_url
from app.bot.runner import get_bot_app, run_bot
from app.api.routes.handlers import (
    admin_dashboard,
    download_file,
    file_page,
    healthz,
    index,
    media_file,
    stream_page,
)

logger = logging.getLogger(__name__)


# ── MTProto background pre-warm ───────────────────────────────────────────────
async def _prewarm_mtproto() -> None:
    """
    Background task: connect MTProto AFTER lifespan yields so it never
    blocks the healthcheck. Errors are logged but never propagate —
    the first download request calls async_ensure_running() itself.

    WHY THIS MATTERS FOR BUFFERING:
      If MTProto is cold when VLC or the browser opens /media/{hash}, the
      streaming response stalls for 10-15 seconds while Telethon connects
      and authenticates with Telegram's DCs. VLC shows this as endless
      buffering. Pre-warming here means MTProto is ready before the first
      user request arrives.
    """
    if not has_mtproto_support():
        return
    try:
        await mtproto.async_ensure_running()
        logger.info("MTProto client pre-warmed ✓")
    except Exception as exc:
        logger.warning("MTProto pre-warm failed (will retry on first request): %s", exc)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 1. Start file-expiry cleanup worker
    start_cleanup_worker()

    # 2. Start Telegram bot polling thread
    stop_ev    = Event()
    bot_thread = Thread(target=run_bot, args=(stop_ev,), daemon=True, name="bot-polling")
    bot_thread.start()
    logger.info("Telegram bot thread started")

    # 3. Pre-warm MTProto in the BACKGROUND — CRITICAL for two reasons:
    #    a) Must NOT await before yield, otherwise /healthz is blocked until
    #       MTProto connects (causes Railway healthcheck failures).
    #    b) Must start BEFORE first user request to avoid cold-start buffering
    #       in VLC and browsers.
    asyncio.create_task(_prewarm_mtproto())

    # Yield immediately → uvicorn starts serving → /healthz returns 200 at once
    yield

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    logger.info("Shutting down Telegram bot …")
    stop_ev.set()
    bot_app = get_bot_app()
    if bot_app is not None:
        try:
            sd_loop = asyncio.new_event_loop()
            sd_loop.run_until_complete(bot_app.updater.stop())
            sd_loop.run_until_complete(bot_app.stop())
            sd_loop.run_until_complete(bot_app.shutdown())
            sd_loop.close()
        except Exception as exc:
            logger.warning("Bot shutdown error (non-fatal): %s", exc)
    bot_thread.join(timeout=8)
    logger.info("Bot thread joined — shutdown complete")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="File-to-Link Bot", lifespan=lifespan)


# ── HTTPS redirect middleware ──────────────────────────────────────────────────
def _is_local(host: str) -> bool:
    return host.split(":", 1)[0].lower() in {"localhost", "127.0.0.1", "0.0.0.0"}


@app.middleware("http")
async def force_https_middleware(request: Request, call_next):
    update_detected_base_url(request)
    host   = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    proto  = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    skip   = request.url.path in {"/", "/healthz"}

    if FORCE_HTTPS and host and not _is_local(host) and not skip:
        if not (request.url.scheme == "https" or proto == "https"):
            parsed    = urlsplit(str(request.url))
            https_url = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
            return RedirectResponse(url=https_url, status_code=308)

    return await call_next(request)


# ── Routes ────────────────────────────────────────────────────────────────────
app.get("/healthz")(healthz)
app.get("/",                          response_class=HTMLResponse)(index)
app.get("/admin",                     response_class=HTMLResponse)(admin_dashboard)
app.get("/file/{file_hash}",          response_class=HTMLResponse)(file_page)
app.get("/stream/{file_hash}",        response_class=HTMLResponse)(stream_page)
app.get("/download/{file_hash}")(download_file)
app.get("/media/{file_hash}")(media_file)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    import uvicorn
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logger.info("Starting File-to-Link on port %d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, workers=1, log_level="warning")


if __name__ == "__main__":
    main()
