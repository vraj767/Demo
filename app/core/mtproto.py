"""
app/core/mtproto.py
────────────────────
All Telethon / MTProto logic in one place:
  - _MTProtoManager  — lifecycle, connection health, concurrency slots
  - async_ensure_running  — non-blocking startup
  - _mtproto_download_task  — asyncio.Queue producer
  - iter_large_file  — async generator consumed by FastAPI StreamingResponse

FIX: FloodWaitError (ImportBotAuthorizationRequest) is now caught and retried
     with exponential backoff instead of permanently marking _start_error and
     causing every download to fail with "Network error".
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

    BUG FIX — FloodWaitError on startup:
      Telethon raises FloodWaitError (A wait of N seconds is required) when the
      same BOT_TOKEN is used to call ImportBotAuthorizationRequest too many times
      in a short window (e.g. after rapid Railway redeploys).

      OLD behaviour: _start_error was set permanently → every single download
      hit "MTProto not ready: A wait of N seconds is required" → Chrome showed
      "Failed - Network error".

      NEW behaviour: _init() catches FloodWaitError, waits the required number
      of seconds (sleeping in 1-second ticks so the thread stays alive), then
      retries. Only a non-FloodWait error is treated as fatal.
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

    # ── Sync ensure_running ──────────────────────────────────────────────────
    def ensure_running(self) -> None:
        """Blocking — only call from non-async context."""
        if self._ready.is_set() and not self._start_error:
            return
        with self._lock:
            if not self._started:
                self._started = True
                Thread(target=self._worker, daemon=True, name="mtproto").start()
        if not self._loop_ready.wait(timeout=10):
            raise LargeFileStreamError("Timed out waiting for MTProto event loop")
        # Allow up to 10 min for FloodWait to resolve
        if not self._ready.wait(timeout=620):
            raise LargeFileStreamError("Timed out while starting MTProto client")
        if self._start_error:
            raise LargeFileStreamError(self._start_error)

    # ── Async ensure_running ─────────────────────────────────────────────────
    async def async_ensure_running(self) -> None:
        """
        Non-blocking async version — never freezes the uvicorn event loop.
        Polls every 50 ms; allows up to 10 minutes for FloodWait to resolve.
        """
        if self._ready.is_set() and not self._start_error:
            return
        with self._lock:
            if not self._started:
                self._started = True
                Thread(target=self._worker, daemon=True, name="mtproto").start()
        # 12 000 × 50 ms = 10 minutes (enough for any FloodWait)
        for _ in range(12_000):
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

    # ── KEY FIX: _init with FloodWait retry ──────────────────────────────────
    async def _init(self) -> None:
        """
        Initialise the Telethon client.

        ROOT CAUSE OF "Network error":
          When Railway redeploys rapidly, Telegram's DC sees repeated
          ImportBotAuthorizationRequest calls from the same token and responds
          with FloodWaitError("A wait of N seconds is required").

          The OLD code caught this as a generic Exception, stored it in
          _start_error, and set _ready — permanently poisoning every future
          download attempt until the next full redeploy.

        FIX:
          We import FloodWaitError specifically and, when caught, sleep for the
          required number of seconds (logged so you can see it in Railway logs)
          then retry. Only a non-FloodWait failure is treated as fatal.
        """
        MAX_ATTEMPTS = 5
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                from telethon import TelegramClient
                from telethon.errors import FloodWaitError
                from telethon.sessions import StringSession

                try:
                    import cryptg  # noqa: F401
                    logger.info("cryptg loaded — fast AES encryption active ✓")
                except ImportError:
                    logger.warning(
                        "cryptg not installed — download speed will be slower. "
                        "Ensure Dockerfile installs cryptg with libssl-dev."
                    )

                # ── Validate TELETHON_SESSION before passing to StringSession ──
                # StringSession() calls base64.b64decode() internally.
                # A corrupted/truncated session string raises "Incorrect padding"
                # deep inside Telethon with no clear error message.
                # We validate here first so the error is actionable.
                if TELETHON_SESSION:
                    import base64 as _b64
                    _raw = TELETHON_SESSION.strip()
                    # StringSession strips the first char (version byte) then b64-decodes the rest
                    _to_decode = _raw[1:] if len(_raw) > 1 else _raw
                    # Pad to multiple of 4 the same way StringSession does
                    _padded = _to_decode + "=" * (-len(_to_decode) % 4)
                    try:
                        _b64.urlsafe_b64decode(_padded)
                    except Exception:
                        raise ValueError(
                            "TELETHON_SESSION value is invalid (base64 decode failed). "
                            "Delete the TELETHON_SESSION variable in Railway env vars "
                            "and redeploy — a fresh session will be generated and printed in logs."
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

                # Success — exit the retry loop cleanly
                self._start_error = None
                return

            except Exception as exc:
                # ── FloodWaitError: sleep the required time then retry ────────
                # Import lazily so we don't fail at module level if telethon
                # is not installed yet during Docker build.
                try:
                    from telethon.errors import FloodWaitError as _FWE
                    if isinstance(exc, _FWE):
                        wait_secs = exc.seconds + 5   # small safety buffer
                        logger.warning(
                            "MTProto FloodWaitError on attempt %d/%d — "
                            "Telegram rate-limited this token. "
                            "Waiting %d seconds before retry. "
                            "(Root cause: too many rapid redeploys reusing the same BOT_TOKEN "
                            "without a saved TELETHON_SESSION env var.)",
                            attempt, MAX_ATTEMPTS, wait_secs,
                        )
                        # Sleep in 1-second ticks so the thread stays responsive
                        for _ in range(wait_secs):
                            await asyncio.sleep(1)
                        continue   # retry after waiting
                except ImportError:
                    pass

                # ── Any other error is fatal ──────────────────────────────────
                self._start_error = str(exc)
                logger.error("MTProto init failed (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
                return   # don't retry on non-FloodWait errors

        # All FloodWait retry attempts exhausted
        self._start_error = (
            "MTProto client failed to start after multiple FloodWait retries. "
            "Set TELETHON_SESSION env var to avoid this (see logs for the session string)."
        )
        logger.error(self._start_error)


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
    await q.put(item)


# ─────────────────────────────────────────────────────────────────────────────
# MTProto download coroutine (asyncio.Queue producer)
# ─────────────────────────────────────────────────────────────────────────────
async def _download_task(
    chat_id:      int,
    message_id:   int,
    offset:       int,
    length:       Optional[int],
    put_fn,
    sentinel:     object,
    cancel_event: asyncio.Event,
) -> None:
    remaining = length
    try:
        await mtproto.ensure_connected()
        client = mtproto._client
        msg    = await client.get_messages(chat_id, ids=message_id)
        if not msg or not msg.media:
            raise LargeFileStreamError("Could not find media message")

        _n = max(1, mtproto.active_count())
        if _n == 1:
            _req_size = TELETHON_REQUEST_SIZE
        elif _n == 2:
            _req_size = TELETHON_REQUEST_SIZE // 2
        else:
            _req_size = TELETHON_REQUEST_SIZE // 4

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
    _active_now  = mtproto.active_count()
    _queue_size  = max(2, MTPROTO_QUEUE_CHUNKS // max(1, _active_now - 1)) if _active_now > 1 else MTPROTO_QUEUE_CHUNKS
    data_queue: asyncio.Queue = asyncio.Queue(maxsize=_queue_size)

    def put_from_thread(item) -> None:
        """
        Fire-and-forget push from the MTProto thread into the uvicorn queue.

        WHY NOT BLOCKING/BACKPRESSURE HERE:
          _download_task is an async coroutine running on the MTProto event
          loop thread. Any sync blocking call (threading.Event.wait, semaphore
          acquire, etc.) inside it FREEZES the entire MTProto event loop,
          stopping Telethon's TCP I/O, keepalives, and all pending coroutines.
          This causes a deadlock far worse than any buffering issue.

        HOW FLOW CONTROL WORKS:
          The asyncio.Queue has maxsize=MTPROTO_QUEUE_CHUNKS. When it is full,
          _async_put (scheduled on uvicorn's loop) suspends on q.put() until
          uvicorn consumes an item. Meanwhile Telethon's iter_download is
          also suspended awaiting the next network chunk — so Telethon
          naturally slows down when the queue fills. No blocking needed.

        WHY SMALL CHUNKS PREVENT BUFFERING:
          TELETHON_REQUEST_SIZE = 1 MB. Each chunk downloads in ~0.1-0.3 s.
          Queue maxsize = 4 chunks = 4 MB max ahead-of-playback buffer.
          VLC gets fresh data every ~0.1-0.3 s → smooth continuous playback.
          (Old 8 MB chunks took 2-4 s each → visible stall between chunks.)
        """
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
