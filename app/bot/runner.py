"""
app/bot/runner.py
──────────────────
Telegram bot polling thread lifecycle.

FIXES applied:
  1. _kick_old_session() now uses a LONG-POLL eviction call (timeout=5) then
     a second timeout=0 confirm call, with up to 5 retries and 10 s final wait.
     This is the correct Telegram-documented way to forcibly terminate any
     existing getUpdates session before starting a new one.
  2. Conflict errors are caught inside run_bot() and trigger a fresh
     re-eviction + re-poll so the bot self-heals without a full redeploy.
"""
import asyncio
import logging
import time
from threading import Event
from typing import Optional

import requests
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.core.config import BOT_TOKEN, API_ID, API_HASH
from app.bot.handlers import error_handler, file_handler, start_command

logger   = logging.getLogger(__name__)
_bot_app: Optional[Application] = None


def _validate_config() -> bool:
    """Check required env vars. Returns True if all present."""
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if missing:
        logger.error(
            "BOT STARTUP ABORTED — Missing Railway environment variables: %s\n"
            "Go to Railway Dashboard → your service → Variables and add them.\n"
            "  BOT_TOKEN  → token from @BotFather\n"
            "  API_ID     → from https://my.telegram.org/apps\n"
            "  API_HASH   → from https://my.telegram.org/apps",
            ", ".join(missing),
        )
        return False
    return True


def _kick_old_session() -> None:
    """
    Forcibly terminates any previous getUpdates long-poll on Telegram's side.

    Strategy (Telegram-documented):
      Step 1: Call getUpdates with timeout=5 — this acts as a "claim" and
              forces Telegram to terminate the old session immediately.
      Step 2: Call getUpdates with timeout=0, offset=-1 — confirms the slot
              is now ours and drains any lingering state.
      Step 3: Wait 10 s — gives Telegram's infrastructure time to fully
              propagate the session change across all DC nodes.

    Why 10 s? Telegram uses multiple data centres. The session termination
    must propagate to all of them. 5 s was sometimes not enough when the
    previous instance was connected to a different DC than the new one.
    """
    if not BOT_TOKEN:
        return
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    # Step 0: delete any webhook (no-op if not set, but clears stale state)
    try:
        requests.post(f"{base}/deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
        logger.info("deleteWebhook OK")
    except Exception as exc:
        logger.warning("deleteWebhook failed (non-fatal): %s", exc)

    # Step 1+2: eviction with retries
    for attempt in range(1, 6):
        try:
            # Long-poll claim: forces Telegram to kill the old session
            r1 = requests.post(
                f"{base}/getUpdates",
                json={"timeout": 5, "offset": -1},
                timeout=15,
            )
            logger.info("Eviction long-poll attempt %d — HTTP %d", attempt, r1.status_code)

            # Confirm: quick drain
            r2 = requests.post(
                f"{base}/getUpdates",
                json={"timeout": 0, "offset": -1},
                timeout=10,
            )
            logger.info("Eviction confirm attempt %d — HTTP %d", attempt, r2.status_code)

            if r1.status_code == 200 and r2.status_code == 200:
                logger.info("Old bot session evicted on attempt %d ✓", attempt)
                break
        except Exception as exc:
            logger.warning("Eviction attempt %d failed: %s", attempt, exc)
        time.sleep(3)

    logger.info("Waiting 10 s for Telegram to fully release the polling slot …")
    time.sleep(10)


def run_bot(stop_event: Event) -> None:
    global _bot_app

    if not _validate_config():
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    MAX_RESTARTS = 5
    for restart in range(MAX_RESTARTS):
        if restart > 0:
            logger.info("Bot polling restart attempt %d/%d …", restart + 1, MAX_RESTARTS)

        _kick_old_session()

        try:
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
            logger.info("Telegram bot polling started ✓ (restart=%d)", restart)
            app.run_polling(
                drop_pending_updates=True,
                close_loop=False,
                stop_signals=None,
                poll_interval=0,
                timeout=3,
                bootstrap_retries=10,
            )
            # Normal exit — don't restart
            break

        except Conflict as exc:
            # 409 Conflict: another instance is still holding the slot.
            # Re-evict and retry immediately.
            logger.warning(
                "409 Conflict on restart %d — re-evicting and retrying: %s", restart, exc
            )
            time.sleep(5)
            continue

        except Exception as exc:
            logger.error("Bot polling exited with error: %s", exc)
            break

    logger.info("Bot polling thread finished")


def get_bot_app() -> Optional[Application]:
    return _bot_app
