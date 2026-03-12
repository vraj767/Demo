"""
app/bot/handlers.py
────────────────────
All Telegram bot handlers:
  - /start with force-join check
  - file_handler for document / video / audio
  - error_handler
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.core.config import DEFAULT_TTL_SECONDS, REQUIRED_CHANNEL
from app.core.storage import register_file
from app.core.urls import effective_base_url

logger = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".m2ts"}


async def _is_member(bot, user_id: int, channel: str) -> bool:
    """
    Returns True if user is a member of the channel.
    Fails open (returns True) on any API error so users are never permanently
    locked out by a misconfiguration.  Bot must be an ADMIN of the channel.
    """
    try:
        member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
        status = member.status
        logger.info("getChatMember @%s user=%s status=%s", channel, user_id, status)
        return status in ("member", "administrator", "creator", "restricted")
    except Exception as exc:
        logger.warning(
            "getChatMember failed for @%s user=%s: %s — failing open. "
            "Fix: add bot as ADMIN of the channel.",
            channel, user_id, exc,
        )
        return True   # fail-open: never lock users out due to misconfiguration


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user  = update.effective_user
    first = user.first_name if user else "there"

    if REQUIRED_CHANNEL and not await _is_member(context.bot, user.id, REQUIRED_CHANNEL):
        await update.message.reply_text(
            f"👋 Hey *{first}*!\n\n"
            "⚠️ *You must join our channel to use this bot.*\n\n"
            "1️⃣ Click the button below to join\n"
            "2️⃣ Then send /start again",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL}")
            ]]),
        )
        return

    await update.message.reply_text(
        f"👋 Hey *{first}*! Welcome to *File To Link Bot* 🎬\n\n"
        "📤 *How to use:*\n"
        "Simply send me any file — video, audio, or document — "
        "and I'll instantly generate a *direct streaming & download link* for you.\n\n"
        "⚡ *Features:*\n"
        "• 🔗 Instant shareable link\n"
        "• ▶️ Stream directly in browser\n"
        "• ⬇️ Fast direct download\n"
        "• ⏳ Link valid for 6 hours\n\n"
        "📁 Just forward or send any file to get started!",
        parse_mode="Markdown",
    )


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user    = update.effective_user

    if REQUIRED_CHANNEL and user:
        if not await _is_member(context.bot, user.id, REQUIRED_CHANNEL):
            await message.reply_text(
                "⚠️ *Please join our channel first to use this bot.*\n\nAfter joining, resend your file.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL}")
                ]]),
            )
            return

    tg_file   = None
    file_name = "file"
    is_video  = False

    if message.document:
        tg_file   = message.document
        file_name = tg_file.file_name or "document"
        is_video  = any(file_name.lower().endswith(e) for e in _VIDEO_EXTS)
    elif message.video:
        tg_file   = message.video
        file_name = getattr(tg_file, "file_name", None) or "video.mp4"
        is_video  = True
    elif message.audio:
        tg_file   = message.audio
        file_name = getattr(tg_file, "file_name", None) or "audio.mp3"

    if not tg_file:
        return

    file_hash = register_file(
        file_id    = tg_file.file_id,
        file_name  = file_name,
        file_size  = tg_file.file_size or 0,
        is_video   = is_video,
        chat_id    = message.chat_id,
        message_id = message.message_id,
    )

    base      = effective_base_url()
    file_url  = f"{base}/file/{file_hash}"
    file_size = tg_file.file_size or 0
    size_text = (
        f"{file_size / (1024**3):.2f} GB"
        if file_size >= 1024 ** 3
        else f"{file_size / (1024**2):.2f} MB"
    )
    ttl_hours = DEFAULT_TTL_SECONDS // 3600

    await message.reply_text(
        f"💎 *FAST DOWNLOAD LINK GENERATED*\n\n"
        f"🎬 *Title:* `{file_name}`\n"
        f"📦 *Size:* `{size_text}`\n\n"
        f"🔗 {file_url}\n\n"
        f"⏳ *Expiry:* Link expires in {ttl_hours} Hours\n"
        f"💡 *Tip:* Use the buttons below to Stream or Download",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Stream",   url=file_url),
            InlineKeyboardButton("⬇️ Download", url=file_url),
        ]]),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram error:", exc_info=context.error)
