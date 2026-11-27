"""
utils/lbclient.py - the MTProto side of Local Backup / Recover.

  * a dedicated Pyrogram client (same BOT_TOKEN, API_ID / API_HASH) that lives only while a backup / recover job runs,
    with several parallel transfers (the shared session in utils/mtproto.py allows only one)
  * describe(msg)   what a Database-Channel message holds (kind, size, name, caption HTML, duration, thumbnail ...)
  * iter_media()    streams a file chunk by chunk, several 1 MiB chunks in parallel, never more than a few MB in RAM
  * flood helpers   FloodWait / RetryAfter -> sleep and retry
"""
import asyncio
import inspect
import logging
import os
from collections import deque

import config

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
PREFETCH = max(1, int(os.getenv("LB_PREFETCH", "4")))            # chunks fetched in parallel per file
TRANSFERS = max(PREFETCH, int(os.getenv("LB_TRANSFERS", "6")))   # parallel Telegram transfers of the client
KINDS = ("document", "video", "audio", "photo", "animation", "voice", "video_note", "sticker")


# ------------------------------------------------------------------ client lifecycle
class LBClient:
    """acquire() / release() are reference counted: the client is started once and stopped when nobody needs it."""

    def __init__(self):
        self.client = None
        self.dedicated = False
        self._refs = 0
        self._lock = None

    async def acquire(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._refs += 1
            if self.client is not None:
                return self.client
            try:
                self.client = await self._start()
            except Exception:
                self._refs -= 1
                raise
            return self.client

    async def _start(self):
        if not config.API_ID or not config.API_HASH:
            raise RuntimeError("API_ID / API_HASH are missing in .env")
        try:
            from pyrogram import Client
        except ImportError:
            raise RuntimeError("pyrofork is not installed")
        kwargs = dict(api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN,
                      in_memory=True, no_updates=True, sleep_threshold=60)
        try:
            try:
                client = Client("localbackup", max_concurrent_transmissions=TRANSFERS, **kwargs)
            except TypeError:                       # older build without this option
                client = Client("localbackup", **kwargs)
            if hasattr(client, "get_file_semaphore"):
                client.get_file_semaphore = asyncio.Semaphore(TRANSFERS)
            await client.start()
            self.dedicated = True
            log.info("Local backup: dedicated MTProto client started (%d transfers).", TRANSFERS)
            return client
        except Exception as e:
            log.warning("Dedicated MTProto client failed (%s); using the shared session.", e)
            from utils.mtproto import mtproto
            if mtproto.ready:
                self.dedicated = False
                return mtproto.client
            raise RuntimeError(f"MTProto session is not available ({mtproto.error or type(e).__name__})")

    async def release(self):
        if self._lock is None:
            return
        async with self._lock:
            self._refs = max(0, self._refs - 1)
            if self._refs == 0 and self.client is not None:
                if self.dedicated:
                    try:
                        await self.client.stop()
                    except Exception:
                        pass
                self.client, self.dedicated = None, False


lbclient = LBClient()


def kwfilter(fn, **kw):
    """Only the keyword arguments `fn` accepts (Pyrogram forks differ slightly between versions)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return kw
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return kw
    return {k: v for k, v in kw.items() if k in params}


# ------------------------------------------------------------------ flood control
def flood_seconds(e):
    """Seconds to wait if `e` is a Telegram flood error (Pyrogram FloodWait / PTB RetryAfter), else None."""
    name = type(e).__name__
    if name in ("FloodWait", "SlowmodeWait", "FloodPremiumWait"):
        return float(getattr(e, "value", 5) or 5)
    if name == "RetryAfter":
        ra = getattr(e, "retry_after", 5)
        return float(ra.total_seconds() if hasattr(ra, "total_seconds") else ra)
    return None


async def with_flood(fn, *args, tries: int = 12, **kwargs):
    """Await fn(...); on a flood error sleep as long as Telegram asks and try again (the wait is not a failure)."""
    for _ in range(tries):
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            wait = flood_seconds(e)
            if wait is None:
                raise
            log.warning("Flood wait: sleeping %.0fs", wait + 1)
            await asyncio.sleep(wait + 1)
    return await fn(*args, **kwargs)


# ------------------------------------------------------------------ reading messages
def _thumb_id(obj):
    thumbs = getattr(obj, "thumbs", None) or []
    best = None
    for t in thumbs:                                     # the biggest one that Telegram accepts back (<= 320 px)
        if getattr(t, "file_id", None) and (getattr(t, "width", 0) or 0) <= 320:
            if best is None or (t.width or 0) > (best.width or 0):
                best = t
    if best is None and thumbs and getattr(thumbs[0], "file_id", None):
        best = thumbs[0]
    return best.file_id if best else None


def describe(msg):
    """-> (info dict, "") for a message that holds a file, else (None, reason)."""
    if msg is None or getattr(msg, "empty", False):
        return None, "message deleted from the channel"
    kind = None
    media = getattr(msg, "media", None)
    if media is not None:
        kind = getattr(media, "value", str(media))
    if kind not in KINDS:
        kind = next((k for k in KINDS if getattr(msg, k, None)), None)
    obj = getattr(msg, kind, None) if kind else None
    if not obj:
        return None, "not a file (text / poll / other)"
    cap = getattr(msg, "caption", None)
    date = getattr(msg, "date", None)
    return {
        "kind": kind,
        "size": int(getattr(obj, "file_size", 0) or 0),
        "file_name": getattr(obj, "file_name", None),
        "mime": getattr(obj, "mime_type", None) or ("image/jpeg" if kind == "photo" else None),
        "duration": getattr(obj, "duration", None),
        "width": getattr(obj, "width", None),
        "height": getattr(obj, "height", None),
        "performer": getattr(obj, "performer", None),
        "title": getattr(obj, "title", None),
        "length": getattr(obj, "length", None),
        "supports_streaming": getattr(obj, "supports_streaming", None),
        "caption_html": (getattr(cap, "html", None) or str(cap)) if cap else "",
        "caption": str(cap) if cap else "",
        "date": date.timestamp() if date else 0,
        "thumb_fid": _thumb_id(obj) if kind in ("video", "audio", "document", "animation", "video_note") else None,
    }, ""


async def get_messages(client, chat_id, ids: list) -> list:
    """Messages for `ids` (same order, None where Telegram returned nothing)."""
    res = await with_flood(client.get_messages, chat_id, ids)
    if not isinstance(res, list):
        res = [res]
    return list(res) + [None] * (len(ids) - len(res))


# ------------------------------------------------------------------ downloading
async def _fetch(client, msg, index: int) -> bytes:
    for attempt in range(4):
        try:
            async for chunk in client.stream_media(msg, limit=1, offset=index):
                return chunk
            return b""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            wait = flood_seconds(e)
            if wait is not None:
                await asyncio.sleep(wait + 1)
                continue
            if attempt == 3:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
    return b""


async def iter_media(client, msg, size: int, prefetch: int = PREFETCH):
    """Yield the file's bytes in order. Several chunks are requested in parallel so the speed does not depend on the
    round-trip time of a single chunk. At most `prefetch` MiB are in RAM at any time."""
    if size <= 0:                                        # size unknown: one sequential stream
        async for chunk in client.stream_media(msg):
            yield chunk
        return
    total = -(-size // CHUNK)
    pending, nxt = deque(), 0
    try:
        while nxt < total or pending:
            while nxt < total and len(pending) < prefetch:
                pending.append(asyncio.ensure_future(_fetch(client, msg, nxt)))
                nxt += 1
            chunk = await pending.popleft()
            if not chunk:
                raise IOError("Telegram returned fewer bytes than the file size")
            yield chunk
    finally:
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def download_thumb(client, file_id):
    """Thumbnail bytes (small JPEG) or None."""
    if not file_id:
        return None
    try:
        buf = await with_flood(client.download_media, file_id, in_memory=True)
        data = bytes(buf.getbuffer()) if buf is not None else b""
        return data if 0 < len(data) <= 512 * 1024 else None
    except Exception as e:
        log.info("Thumbnail not available: %s", e)
        return None
