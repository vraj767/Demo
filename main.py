"""
main.py
────────
FastAPI application factory.
Wires together: lifespan, middleware, all route handlers.

CHANGES:
  - Added _setup_bot_commands() — automatically sets two command menus
    on every deploy:
      1. Regular users see: /start, /help
      2. Admin (ADMIN_USER_ID) sees: /start, /help, /stats, /broadcast
    This means when admin types / in the bot chat, all 4 commands appear.
    Regular users only see the 2 public commands.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from threading import Event, Thread
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import ADMIN_USER_ID, BOT_TOKEN, FORCE_HTTPS, PORT
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


# ── Bot command menu setup ─────────────────────────────────────────────────────
async def _setup_bot_commands() -> None:
    """
    Set the bot command menu via the Telegram Bot API.

    Regular users see:
      /start   - Welcome message
      /help    - How to use the bot

    Admin (ADMIN_USER_ID) sees all of the above PLUS:
      /stats     - View bot statistics
      /broadcast - Send message to all users

    This runs once at startup. If it fails (e.g. BOT_TOKEN not set yet),
    it logs a warning and continues — it's non-critical.
    """
    if not BOT_TOKEN:
        return

    import requests as _req

    base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    # ── 1. Public commands (visible to everyone) ──────────────────────────────
    public_commands = [
        {"command": "start", "description": "Welcome message"},
        {"command": "help",  "description": "How to use the bot"},
    ]
    try:
        _req.post(
            f"{base}/setMyCommands",
            json={"commands": public_commands, "scope": {"type": "default"}},
            timeout=10,
        )
        logger.info("Bot public commands set ✓")
    except Exception as exc:
        logger.warning("Could not set public bot commands: %s", exc)

    # ── 2. Admin commands (visible only to ADMIN_USER_ID in private chat) ─────
    if ADMIN_USER_ID:
        admin_commands = [
            {"command": "start",     "description": "Welcome message"},
            {"command": "help",      "description": "How to use the bot"},
            {"command": "stats",     "description": "📊 View bot statistics"},
            {"command": "broadcast", "description": "📢 Send message to all users"},
        ]
        try:
            _req.post(
                f"{base}/setMyCommands",
                json={
                    "commands": admin_commands,
                    "scope": {
                        "type":    "chat",
                        "chat_id": ADMIN_USER_ID,
                    },
                },
                timeout=10,
            )
            logger.info("Bot admin commands set for user %s ✓", ADMIN_USER_ID)
        except Exception as exc:
            logger.warning("Could not set admin bot commands: %s", exc)


# ── MTProto background pre-warm ────────────────────────────────────────────────
async def _prewarm_mtproto() -> None:
    if not has_mtproto_support():
        return
    try:
        await mtproto.async_ensure_running()
        logger.info("MTProto client pre-warmed ✓")
    except Exception as exc:
        logger.warning("MTProto pre-warm failed (will retry on first request): %s", exc)


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 1. Start file-expiry cleanup worker
    start_cleanup_worker()

    # 2. Start Telegram bot polling thread
    stop_ev    = Event()
    bot_thread = Thread(target=run_bot, args=(stop_ev,), daemon=True, name="bot-polling")
    bot_thread.start()
    logger.info("Telegram bot thread started")

    # 3. Set bot command menus (public + admin)
    await _setup_bot_commands()

    # 4. Pre-warm MTProto in background — must NOT block before yield
    #    otherwise /healthz is blocked until MTProto connects
    asyncio.create_task(_prewarm_mtproto())

    # Yield immediately → uvicorn starts serving → /healthz returns 200 instantly
    yield

    # ── Graceful shutdown ──────────────────────────────────────────────────────
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


# ── App ────────────────────────────────────────────────────────────────────────
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


# ── Routes ─────────────────────────────────────────────────────────────────────
app.get("/healthz")(healthz)
app.get("/",                          response_class=HTMLResponse)(index)
app.get("/admin",                     response_class=HTMLResponse)(admin_dashboard)
app.get("/file/{file_hash}",          response_class=HTMLResponse)(file_page)
app.get("/stream/{file_hash}",        response_class=HTMLResponse)(stream_page)
app.get("/download/{file_hash}")(download_file)
app.get("/media/{file_hash}")(media_file)


# ── Entry point ────────────────────────────────────────────────────────────────
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
