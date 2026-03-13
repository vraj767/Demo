"""
app/bot/runner.py
──────────────────
Telegram bot polling thread lifecycle:
  - _kick_old_session()  — evicts stale getUpdates slot (prevents 409 Conflict)
  - run_bot()            — runs python-telegram-bot on its own event loop thread

CHANGES:
  6. Registered help_command handler with CommandHandler("help", help_command).
"""
import asyncio
import logging
import time
from threading import Event
from typing import Optional

import requests
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.core.config import BOT_TOKEN
from app.bot.handlers import error_handler, file_handler, help_command, start_command

logger   = logging.getLogger(__name__)
_bot_app: Optional[Application] = None


def _kick_old_session() -> None:
    """
    Evicts any running getUpdates long-poll from a previous deployment.

    Root cause of 409 Conflict:
      Railway hard-kills the old container before its 30s getUpdates call
      returns, leaving Telegram's slot occupied. The new instance immediately
      hits 409.

    Fix: POST getUpdates(timeout=0, offset=-1) right at startup.
    Telegram treats this as a new connection request and terminates the old one.
    """
    if not BOT_TOKEN:
        return
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    try:
        requests.post(f"{base}/deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
    except Exception as exc:
        logger.warning("deleteWebhook failed (non-fatal): %s", exc)
    try:
        requests.post(f"{base}/getUpdates", json={"timeout": 0, "offset": -1}, timeout=10)
        logger.info("Old bot session evicted — polling slot is free")
    except Exception as exc:
        logger.warning("Session-eviction getUpdates failed (non-fatal): %s", exc)
    time.sleep(2)


def run_bot(stop_event: Event) -> None:
    global _bot_app
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    _kick_old_session()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .connect_timeout(15)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help",  help_command))   # ← Feature 6
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO,
        file_handler,
    ))
    app.add_error_handler(error_handler)

    _bot_app = app
    logger.info("Telegram bot polling started")
    try:
        app.run_polling(
            drop_pending_updates=True,
            close_loop=False,
            stop_signals=None,
            poll_interval=0,
            timeout=3,
            bootstrap_retries=10,
        )
    except Exception as exc:
        logger.error("Bot polling exited with error: %s", exc)
    finally:
        logger.info("Bot polling thread finished")


def get_bot_app() -> Optional[Application]:
    return _bot_app
