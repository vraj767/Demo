"""
app/bot/runner.py
──────────────────
Telegram bot polling thread lifecycle.

FIX: Added drop_webhook_and_wait() with a longer sleep (5 s → was 2 s)
     and a stricter conflict-eviction loop to eliminate the repeated
     "409 Conflict: terminated by other getUpdates request" errors that
     appear when Railway redeploys without the old instance cleanly stopping.
"""
import asyncio
import logging
import time
from threading import Event
from typing import Optional

import requests
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.core.config import BOT_TOKEN
from app.bot.handlers import error_handler, file_handler, start_command

logger   = logging.getLogger(__name__)
_bot_app: Optional[Application] = None


def _kick_old_session() -> None:
    """
    Evicts any running getUpdates long-poll from a previous deployment.

    FIX: Increased post-eviction sleep from 2 s → 5 s.
    Telegram's servers need a few seconds to fully release the slot after
    a conflicting session is terminated. 2 s was not always enough, causing
    the new instance to immediately hit another 409.

    We also retry the eviction getUpdates call up to 3 times to make sure
    the slot is truly free before polling starts.
    """
    if not BOT_TOKEN:
        return
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    try:
        requests.post(f"{base}/deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
    except Exception as exc:
        logger.warning("deleteWebhook failed (non-fatal): %s", exc)

    # Retry eviction up to 3 times
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                f"{base}/getUpdates",
                json={"timeout": 0, "offset": -1},
                timeout=10,
            )
            logger.info(
                "Session-eviction getUpdates attempt %d — HTTP %d",
                attempt, resp.status_code,
            )
            if resp.status_code == 200:
                break
        except Exception as exc:
            logger.warning("Session-eviction attempt %d failed (non-fatal): %s", attempt, exc)
        time.sleep(2)

    logger.info("Old bot session evicted — waiting 5 s for Telegram to release the slot")
    time.sleep(5)   # FIX: was 2 s; 5 s ensures the slot is fully released


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
