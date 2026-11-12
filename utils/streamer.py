"""
utils/streamer.py
Streaming engine (the Python port of TG-FileStreamBot's idea).

Telegram's Bot API can only download files up to 20 MB, so for streaming we also open an MTProto
session (Pyrogram) with the SAME bot token. The bot reads the stored file straight out of the
Database Channel and pipes it to the HTTP client chunk by chunk, with full Range support
(seeking in video players, resumable downloads).

Nothing in this module needs aiohttp, so it can be unit-tested with a fake client.
"""
import logging
import re
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import config

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024          # Pyrogram's stream_media works in 1 MiB chunks
INFO_TTL = 300               # seconds a file's metadata stays cached (file references expire eventually)

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass
class FileInfo:
    msg_id: int
    name: str
    size: int
    mime: str
    kind: str
    message: object          # pyrogram Message (needed by stream_media)
    fetched: float = 0.0

    @property
    def streamable(self) -> bool:
        """Can a browser play/show it inline (so a 'Stream' button makes sense)?"""
        m = self.mime or ""
        return m.startswith(("video/", "audio/", "image/")) or m == "application/pdf"


class RangeError(ValueError):
    pass


def parse_range(header: Optional[str], size: int):
    """
    Parse a single HTTP Range header.
    Returns (start, end, partial). partial=False means "no/ignored range, send everything".
    Raises RangeError when the range cannot be satisfied (-> HTTP 416).
    """
    if not header or size <= 0:
        return 0, max(size - 1, 0), False
    m = _RANGE_RE.match(header.strip())
    if not m:                       # multi-range or garbage: just serve the whole file
        return 0, size - 1, False
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return 0, size - 1, False
    if a == "":                     # suffix range: last N bytes
        n = int(b)
        if n == 0:
            raise RangeError("empty suffix range")
        start, end = max(size - n, 0), size - 1
    else:
        start = int(a)
        end = int(b) if b else size - 1
        end = min(end, size - 1)
    if start >= size or start > end:
        raise RangeError("range not satisfiable")
    return start, end, True


def human_size(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class Streamer:
    def __init__(self):
        self.client = None
        self._cache: dict = {}      # (chat_id, msg_id) -> FileInfo
        self.error: str = ""        # why streaming is unavailable (shown in the admin panel)

    # ------------------------------------------------------------ requirements
    @staticmethod
    def missing_config() -> list:
        miss = []
        if not config.API_ID or not config.API_HASH:
            miss.append("API_ID / API_HASH")
        if not config.BASE_URL:
            miss.append("BASE_URL")
        return miss

    @property
    def ready(self) -> bool:
        return self.client is not None

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        miss = self.missing_config()
        if miss:
            self.error = "missing env: " + ", ".join(miss)
            log.info("Streaming disabled (%s). Forward mode still works.", self.error)
            return
        try:
            from pyrogram import Client  # imported here so forward-only setups never need it
        except ImportError:
            self.error = "pyrofork not installed"
            log.warning("Streaming disabled: pyrofork is not installed.")
            return
        try:
            # Created inside the running loop on purpose (Pyrogram binds to the current loop).
            client = Client(
                "streamer", api_id=config.API_ID, api_hash=config.API_HASH,
                bot_token=config.BOT_TOKEN, in_memory=True, no_updates=True,
            )
            await client.start()
            self.client = client
            self.error = ""
            log.info("Streaming engine started (MTProto).")
        except Exception as e:  # bad API_ID/HASH, network...: never take the whole bot down
            self.error = f"MTProto start failed: {type(e).__name__}"
            log.exception("Could not start the streaming engine")

    async def stop(self):
        if self.client:
            try:
                await self.client.stop()
            except Exception:
                pass
            self.client = None

    # ------------------------------------------------------------ metadata
    @staticmethod
    def _to_info(msg) -> Optional[FileInfo]:
        if msg is None or getattr(msg, "empty", False) or not getattr(msg, "media", None):
            return None
        kind = msg.media.value                       # "document", "video", "photo", ...
        obj = getattr(msg, kind, None)
        if obj is None:
            return None
        size = int(getattr(obj, "file_size", 0) or 0)
        if size <= 0:
            return None
        name = getattr(obj, "file_name", None) or f"{kind}_{msg.id}"
        mime = getattr(obj, "mime_type", None) or ("image/jpeg" if kind == "photo" else "application/octet-stream")
        if kind == "photo" and "." not in name:
            name += ".jpg"
        return FileInfo(msg_id=msg.id, name=name, size=size, mime=mime, kind=kind,
                        message=msg, fetched=time.time())

    async def get_infos(self, chat_id: int, ids: list) -> dict:
        """{msg_id: FileInfo} for every id that still exists in the channel (one batched call)."""
        out, need = {}, []
        now = time.time()
        for mid in ids:
            cached = self._cache.get((chat_id, mid))
            if cached and now - cached.fetched < INFO_TTL:
                out[mid] = cached
            else:
                need.append(mid)
        for i in range(0, len(need), 100):
            part = need[i:i + 100]
            msgs = await self.client.get_messages(chat_id, part)
            if not isinstance(msgs, list):
                msgs = [msgs]
            for mid, msg in zip(part, msgs):
                info = self._to_info(msg)
                if info:
                    self._cache[(chat_id, mid)] = info
                    out[mid] = info
        if len(self._cache) > 2000:      # keep memory bounded
            self._cache.clear()
        return out

    async def get_info(self, chat_id: int, msg_id: int) -> Optional[FileInfo]:
        return (await self.get_infos(chat_id, [msg_id])).get(msg_id)

    # ------------------------------------------------------------ data
    async def iter_range(self, info: FileInfo, start: int, end: int) -> AsyncIterator[bytes]:
        """Yield exactly bytes [start, end] of the file."""
        remaining = end - start + 1
        first_chunk, skip = divmod(start, CHUNK)
        n_chunks = -(-(skip + remaining) // CHUNK)   # ceil
        gen = self.client.stream_media(info.message, limit=n_chunks, offset=first_chunk)
        try:
            async for chunk in gen:
                if skip:
                    chunk, skip = chunk[skip:], 0
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                if chunk:
                    remaining -= len(chunk)
                    yield chunk
                if remaining <= 0:
                    break
        finally:
            aclose = getattr(gen, "aclose", None)
            if aclose:
                try:
                    await aclose()
                except Exception:
                    pass


streamer = Streamer()
