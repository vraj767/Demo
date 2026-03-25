"""
app/bot/handlers.py
────────────────────
All Telegram bot handlers:
  - /start  — welcome + force-join check
  - /help   — usage guide
  - /stats  — admin only: bot statistics
  - /broadcast — admin only: send message to all users
  - file_handler — document / video / audio → generates shortened link
  - error_handler
"""
import asyncio
import logging
import time
import urllib.parse

import requests as _requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.core.config import (
    ADMIN_USER_ID,
    DEFAULT_TTL_SECONDS,
    REQUIRED_CHANNEL,
    SHRINKME_API_KEY,
    START_TIME,
)
from app.core.storage import (
    file_storage,
    get_all_user_ids,
    get_user_count,
    global_stats,
    is_expired,
    register_file,
    register_user,
)
from app.core.urls import effective_base_url

logger = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".m2ts"}


# ── Admin guard ───────────────────────────────────────────────────────────────
def _is_admin(user_id: int) -> bool:
    """Returns True only if ADMIN_USER_ID is set and matches this user."""
    return ADMIN_USER_ID != 0 and user_id == ADMIN_USER_ID


# ── shrinkme.io helper ────────────────────────────────────────────────────────
def shorten_url(url: str) -> str:
    """
    Shorten via shrinkme.io API. Returns shortened URL on success,
    original URL on any failure — bot never breaks.
    API: GET https://shrinkme.io/api?api=TOKEN&url=ENCODED_URL
    Response: {"shortenedUrl": "https://shrinkme.io/xxxxx"}
    """
    if not SHRINKME_API_KEY:
        return url
    try:
        encoded = urllib.parse.quote(url, safe="")
        api_url = f"https://shrinkme.io/api?api={SHRINKME_API_KEY}&url={encoded}"
        resp    = _requests.get(api_url, timeout=8)
        data    = resp.json()
        short   = data.get("shortenedUrl", "").strip()
        if short and short.startswith("http"):
            logger.info("shrinkme.io: %s → %s", url, short)
            return short
        logger.warning("shrinkme.io unexpected response: %s", data)
    except Exception as exc:
        logger.warning("shrinkme.io failed, using original URL: %s", exc)
    return url


# ── Channel membership check ──────────────────────────────────────────────────
async def _is_member(bot, user_id: int, channel: str) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
        status = member.status
        logger.info("getChatMember @%s user=%s status=%s", channel, user_id, status)
        return status in ("member", "administrator", "creator", "restricted")
    except Exception as exc:
        logger.warning(
            "getChatMember failed for @%s user=%s: %s — failing open.",
            channel, user_id, exc,
        )
        return True


# ── /start ────────────────────────────────────────────────────────────────────
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
        "📁 Just forward or send any file to get started!\n\n"
        "💡 Type /help for detailed instructions.",
        parse_mode="Markdown",
    )


# ── /help ─────────────────────────────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ttl_hours = DEFAULT_TTL_SECONDS // 3600
    await update.message.reply_text(
        "📖 *File To Link Bot — Help Guide*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 *What does this bot do?*\n"
        "Send any file and instantly get a shareable link for streaming "
        "or downloading — no login required, works in any browser.\n\n"
        "📂 *Supported file types:*\n"
        "• 🎬 *Video:* MP4, MKV, AVI, MOV, WEBM, FLV, WMV, M4V, TS\n"
        "• 🎵 *Audio:* MP3, AAC, FLAC, WAV, OGG\n"
        "• 📄 *Documents:* PDF, ZIP, and any other file type\n\n"
        f"⏳ *Link expiry:*\n"
        f"All links expire after *{ttl_hours} hours*. "
        "Simply resend the same file to get a fresh link at any time.\n\n"
        "▶️ *Best way to stream — VLC Player:*\n"
        "1. Open VLC → Media → Open Network Stream\n"
        "2. Paste the link → Press Play\n"
        "✅ Best for HEVC/x265 and AV1 files with full audio support\n\n"
        "⚡ *Fastest download — 1DM (Android):*\n"
        "1. Install *1DM* from the Play Store\n"
        "2. Open the link on your phone\n"
        "3. Tap the *1DM Download* button on the page\n"
        "✅ Multi-threaded, up to 5× faster than the browser\n\n"
        "🔄 *Need a fresh link?*\n"
        "Just forward or resend the same file — a new link is generated instantly.\n\n"
        "💬 *Commands:*\n"
        "• /start — Welcome message\n"
        "• /help — This help guide\n\n"
        "❓ *Common issues:*\n"
        "• *No sound in browser?* → Use VLC instead\n"
        "• *Link expired?* → Resend the file for a new one\n"
        "• *Slow download?* → Use the 1DM button on Android",
        parse_mode="Markdown",
    )


# ── /stats (admin only) ───────────────────────────────────────────────────────
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_admin(user.id):
        return   # silently ignore non-admins

    uptime_secs  = int(time.time() - START_TIME)
    uptime_hours = uptime_secs // 3600
    uptime_mins  = (uptime_secs % 3600) // 60
    uptime_secs  = uptime_secs % 60
    uptime_str   = (
        f"{uptime_hours}h {uptime_mins}m {uptime_secs}s"
        if uptime_hours > 0
        else f"{uptime_mins}m {uptime_secs}s"
    )

    active_links    = sum(1 for v in file_storage.values() if not is_expired(v))
    total_users     = get_user_count()
    total_uploaded  = global_stats.get("total_files_uploaded", 0)
    total_downloads = global_stats.get("total_downloads", 0)
    total_streams   = global_stats.get("total_streams", 0)

    await update.message.reply_text(
        "📊 *Bot Statistics*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 *Total Users:* `{total_users}`\n"
        f"🔗 *Active Links:* `{active_links}`\n\n"
        f"📁 *Files Uploaded:* `{total_uploaded}`\n"
        f"⬇️ *Total Downloads:* `{total_downloads}`\n"
        f"▶️ *Total Streams:* `{total_streams}`\n\n"
        f"⏱️ *Uptime:* `{uptime_str}`\n\n"
        "_Stats reset on each redeploy._",
        parse_mode="Markdown",
    )


# ── /broadcast (admin only) ───────────────────────────────────────────────────
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_admin(user.id):
        return   # silently ignore non-admins

    if not context.args:
        await update.message.reply_text(
            "⚠️ *Usage:* `/broadcast your message here`\n\n"
            "_Example:_ `/broadcast Hey everyone! New feature added 🎉`",
            parse_mode="Markdown",
        )
        return

    message_text = " ".join(context.args)
    user_ids     = get_all_user_ids()
    total        = len(user_ids)

    if total == 0:
        await update.message.reply_text(
            "⚠️ No users found. Users are tracked after they send their first file."
        )
        return

    progress_msg = await update.message.reply_text(
        f"📢 *Broadcasting to {total} users...*",
        parse_mode="Markdown",
    )

    sent = failed = blocked = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id    = uid,
                text       = f"📢 *Message from Admin:*\n\n{message_text}",
                parse_mode = "Markdown",
            )
            sent += 1
        except Exception as exc:
            err = str(exc).lower()
            if "blocked" in err or "forbidden" in err or "deactivated" in err:
                blocked += 1
            else:
                failed += 1
            logger.warning("Broadcast failed for user %s: %s", uid, exc)
        await asyncio.sleep(0.05)   # stay under Telegram's 30 msg/s rate limit

    await progress_msg.edit_text(
        f"✅ *Broadcast Complete*\n\n"
        f"📤 *Sent:* `{sent}` / `{total}`\n"
        f"🚫 *Blocked/Left:* `{blocked}`\n"
        f"❌ *Failed:* `{failed}`",
        parse_mode="Markdown",
    )


# ── file_handler ──────────────────────────────────────────────────────────────
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

    # Track user for broadcast & stats
    if user:
        register_user(user.id, user.first_name or "")

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
    short_url = shorten_url(file_url)   # shrinkme.io — falls back to original if unavailable

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
        f"🔗 {short_url}\n\n"
        f"⏳ *Expiry:* Link expires in {ttl_hours} Hours\n"
        f"💡 *Tip:* Tap the button below to open the download page",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Open Download Page", url=short_url),
        ]]),
    )


# ── error_handler ─────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram error:", exc_info=context.error)
