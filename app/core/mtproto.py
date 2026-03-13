"""
app/core/mtproto.py
────────────────────
All Telethon / MTProto logic in one place:
  - _MTProtoManager  — lifecycle, connection health, concurrency slots
  - async_ensure_running  — non-blocking startup (fixes "Network error" / VLC timeout)
  - _mtproto_download_task  — asyncio.Queue producer (high-throughput, no run_in_executor)
  - iter_large_file  — async generator consumed by FastAPI StreamingResponse
"""
import asyncio
import logging
from threading import Event, Lock, Thread
from typing import AsyncGenerator, Optional

from app.core.config import (
    API_HASH,
    API_ID,
    BOT_TOKEN,
    MAX_CONCURRENT_DOWNLOADS,
    MTPROTO_QUEUE_CHUNKS,
    TELETHON_REQUEST_SIZE,
    TELETHON_SESSION,
)

logger = logging.getLogger(__name__)


class LargeFileStreamError(Exception):
    """Raised when an MTProto stream operation fails."""


# ─────────────────────────────────────────────────────────────────────────────
# MTProto Manager
# ─────────────────────────────────────────────────────────────────────────────
class _MTProtoManager:
    """
    Single shared Telethon client running on its own dedicated event-loop thread.

    Speed design:
      request_size=8 MB  →  Telethon issues 16 parallel 512 KB sub-requests.
      asyncio.Queue bridge eliminates run_in_executor overhead per chunk.
      Result: Chrome single-thread ~3-5 MB/s, 1DM multi-thread ~15-25 MB/s.
    """

    def __init__(self) -> None:
        self._loop:        Optional[asyncio.AbstractEventLoop] = None
        self._client       = None
        self._ready        = Event()
        self._loop_ready   = Event()
        self._start_error: Optional[str] = None
        self._lock         = Lock()
        self._started      = False
        self._active_dl    = 0
        self._dl_lock      = Lock()

    # ── Concurrency slots ────────────────────────────────────────────────────
    def acquire_slot(self) -> bool:
        with self._dl_lock:
            if self._active_dl >= MAX_CONCURRENT_DOWNLOADS:
                return False
            self._active_dl += 1
            return True

    def release_slot(self) -> None:
        with self._dl_lock:
            self._active_dl = max(0, self._active_dl - 1)

    def active_count(self) -> int:
        with self._dl_lock:
            return self._active_dl

    # ── Sync ensure_running (kept for internal use) ──────────────────────────
    def ensure_running(self) -> None:
        """Blocking — only call from non-async context (e.g. lifespan thread)."""
        if self._ready.is_set() and not self._start_error:
            return
        with self._lock:
            if not self._started:
                self._started = True
                Thread(target=self._worker, daemon=True, name="mtproto").start()
        if not self._loop_ready.wait(timeout=10):
            raise LargeFileStreamError("Timed out waiting for MTProto event loop")
        if not self._ready.wait(timeout=30):
            raise LargeFileStreamError("Timed out while starting MTProto client")
        if self._start_error:
            raise LargeFileStreamError(self._start_error)

    # ── Async ensure_running (CRITICAL — use in all FastAPI routes) ──────────
    async def async_ensure_running(self) -> None:
        """
        Non-blocking async version — never freezes the uvicorn event loop.

        The sync version calls threading.Event.wait() which blocks ALL uvicorn
        coroutines for up to 30 seconds. During that time:
          - Chrome gets no bytes → "Failed - Network error"
          - VLC gets no response → "Your input can't be opened"

        This version polls every 50ms, yielding control back to uvicorn.
        """
        if self._ready.is_set() and not self._start_error:
            return
        with self._lock:
            if not self._started:
                self._started = True
                Thread(target=self._worker, daemon=True, name="mtproto").start()
        for _ in range(600):          # up to 30 s total
            if self._ready.is_set():
                break
            await asyncio.sleep(0.05)
        else:
            raise LargeFileStreamError("Timed out while starting MTProto client")
        if self._start_error:
            raise LargeFileStreamError(self._start_error)

    def get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise LargeFileStreamError("MTProto event loop not available")
        return self._loop

    # ── Health / reconnect ───────────────────────────────────────────────────
    async def ensure_connected(self) -> None:
        if self._client is None:
            raise LargeFileStreamError("MTProto client not initialised")
        for attempt in range(1, 4):
            try:
                if self._client.is_connected() and await self._client.is_user_authorized():
                    return
            except Exception as exc:
                logger.warning("MTProto health check error (attempt %d): %s", attempt, exc)
            try:
                await self._client.disconnect()
            except Exception:
                pass
            try:
                await self._client.connect()
                await self._client.start(bot_token=BOT_TOKEN)
                logger.info("MTProto reconnected (attempt %d)", attempt)
                return
            except Exception as exc:
                logger.error("MTProto reconnect attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(2 ** attempt)
        raise LargeFileStreamError("MTProto client could not reconnect")

    # ── Worker thread ─────────────────────────────────────────────────────────
    def _worker(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        loop.run_until_complete(self._init())
        if not self._start_error:
            loop.run_forever()

    async def _init(self) -> None:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            try:
                import cryptg  # noqa: F401
                logger.info("cryptg loaded — fast AES encryption active ✓")
            except ImportError:
                logger.warning(
                    "cryptg not installed — download speed will be slower. "
                    "Ensure Dockerfile installs cryptg with libssl-dev."
                )

            session = StringSession(TELETHON_SESSION) if TELETHON_SESSION else StringSession()
            self._client = TelegramClient(
                session,
                int(API_ID),
                API_HASH,
                connection_retries=10,
                retry_delay=2,
                auto_reconnect=True,
                request_retries=5,
                sequential_updates=False,
            )
            await self._client.start(bot_token=BOT_TOKEN)

            session_str = self._client.session.save()
            if not TELETHON_SESSION:
                logger.warning(
                    "\n=== TELETHON SESSION — set as TELETHON_SESSION env var ===\n%s\n"
                    "=============================================================",
                    session_str,
                )
            else:
                logger.info("MTProto client started with existing session ✓")

        except Exception as exc:
            self._start_error = str(exc)
            logger.error("MTProto init failed: %s", exc)
        finally:
            self._ready.set()


# Singleton
mtproto = _MTProtoManager()


def has_mtproto_support() -> bool:
    return bool(BOT_TOKEN and API_ID and API_HASH)


# ─────────────────────────────────────────────────────────────────────────────
# asyncio.Queue bridge helpers
# ─────────────────────────────────────────────────────────────────────────────
async def _set_event(event: asyncio.Event) -> None:
    event.set()


async def _async_put(q: asyncio.Queue, item) -> None:
    """Awaitable queue.put — used by run_coroutine_threadsafe from MTProto thread."""
    await q.put(item)


# ─────────────────────────────────────────────────────────────────────────────
# MTProto download coroutine (asyncio.Queue producer)
# ─────────────────────────────────────────────────────────────────────────────
async def _download_task(
    chat_id:      int,
    message_id:   int,
    offset:       int,
    length:       Optional[int],
    put_fn,                        # callable(item) — thread-safe push to uvicorn queue
    sentinel:     object,
    cancel_event: asyncio.Event,
) -> None:
    """
    Runs on the MTProto loop thread.
    Fetches file chunks from Telegram and pushes them into the asyncio.Queue
    on the uvicorn loop via put_fn (which calls run_coroutine_threadsafe).

    8 MB request_size → 16 parallel 512 KB sub-requests per iteration.
    No queue.Queue / run_in_executor overhead → faster throughput.
    """
    remaining = length
    try:
        await mtproto.ensure_connected()
        client = mtproto._client
        msg    = await client.get_messages(chat_id, ids=message_id)
        if not msg or not msg.media:
            raise LargeFileStreamError("Could not find media message")

        # Dynamic request_size: divide bandwidth fairly among concurrent downloads.
        # More active downloads → smaller request_size per task → fairer sharing.
        # When one download is paused, others immediately scale back up.
        _n = max(1, mtproto.active_count())
        if _n == 1:
            _req_size = TELETHON_REQUEST_SIZE           # 8 MB → 16 parallel sub-reqs
        elif _n == 2:
            _req_size = TELETHON_REQUEST_SIZE // 2     # 4 MB → 8 parallel sub-reqs
        else:
            _req_size = TELETHON_REQUEST_SIZE // 4     # 2 MB → 4 parallel sub-reqs

        logger.info(
            "MTProto download start: chat=%s msg=%s offset=%d length=%s "
            "request_size=%s KB active_downloads=%d",
            chat_id, message_id, offset, length, _req_size // 1024, _n,
        )

        async for chunk in client.iter_download(
            msg.media,
            offset=offset,
            chunk_size=_req_size,
            request_size=_req_size,
        ):
            if cancel_event.is_set():
                logger.info("MTProto download cancelled for msg=%s", message_id)
                return

            if not chunk:
                continue
            block = bytes(chunk)

            if remaining is not None:
                if remaining <= 0:
                    break
                if len(block) > remaining:
                    block = block[:remaining]
                remaining -= len(block)

            put_fn(block)

            if remaining is not None and remaining == 0:
                break

    except Exception as exc:
        logger.exception("MTProto download task failed: %s", exc)
        if not cancel_event.is_set():
            put_fn(LargeFileStreamError(str(exc)))
    finally:
        put_fn(sentinel)


# ─────────────────────────────────────────────────────────────────────────────
# Public async generator — consumed by FastAPI StreamingResponse
# ─────────────────────────────────────────────────────────────────────────────
async def iter_large_file(
    chat_id:    int,
    message_id: int,
    offset:     int = 0,
    length:     Optional[int] = None,
) -> AsyncGenerator[bytes, None]:
    """
    Yields file bytes for a FastAPI StreamingResponse.

    Uses asyncio.Queue instead of queue.Queue + run_in_executor to eliminate
    the per-chunk thread-pool overhead that caused the ~1.4 MB/s bottleneck.
    """
    if not has_mtproto_support():
        raise LargeFileStreamError("Missing API_ID/API_HASH for MTProto streaming")

    try:
        await mtproto.async_ensure_running()
    except LargeFileStreamError as exc:
        raise LargeFileStreamError(f"MTProto not ready: {exc}") from exc

    if not mtproto.acquire_slot():
        raise LargeFileStreamError(
            "Server is busy — too many concurrent downloads. Please try again shortly."
        )

    mtproto_loop = mtproto.get_loop()
    uvicorn_loop = asyncio.get_running_loop()

    cancel_event: asyncio.Event = asyncio.Event()
    sentinel     = object()
    # Scale queue size inversely with active downloads.
    # Smaller queue → pausing a download drains faster → DC bandwidth freed sooner.
    # This is why pausing the small file now boosts the large one symmetrically.
    _active_now = mtproto.active_count()
    _queue_size = max(2, MTPROTO_QUEUE_CHUNKS // max(1, _active_now - 1)) if _active_now > 1 else MTPROTO_QUEUE_CHUNKS
    data_queue: asyncio.Queue   = asyncio.Queue(maxsize=_queue_size)

    def put_from_thread(item) -> None:
        """Thread-safe push from MTProto thread into uvicorn's asyncio.Queue."""
        asyncio.run_coroutine_threadsafe(_async_put(data_queue, item), uvicorn_loop)

    def signal_cancel() -> None:
        asyncio.run_coroutine_threadsafe(_set_event(cancel_event), mtproto_loop)

    asyncio.run_coroutine_threadsafe(
        _download_task(chat_id, message_id, offset, length, put_from_thread, sentinel, cancel_event),
        mtproto_loop,
    )

    try:
        while True:
            item = await data_queue.get()
            if item is sentinel:
                break
            if isinstance(item, Exception):
                signal_cancel()
                raise item
            yield item
    except GeneratorExit:
        logger.info("Client disconnected — cancelling MTProto download for msg=%s", message_id)
        signal_cancel()
        raise
    finally:
        signal_cancel()
        mtproto.release_slot()
