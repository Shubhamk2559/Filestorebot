"""
utils/lbclient.py - the MTProto side of Local Backup / Recover.

  * a dedicated Pyrogram client (same BOT_TOKEN, API_ID / API_HASH) that lives only while a backup / recover job runs,
    with several parallel transfers (the shared session in utils/mtproto.py allows only a few). For a backup the pool
    can hold several such clients (LB_CLIENTS): every client has its own MTProto connections to the Telegram DCs.
  * the client says plainly what it got (lbclient.mode / .warnings / .eff): dedicated or shared session, how many
    transfers really run in parallel, whether tgcrypto is installed. The backup panel shows the warnings.
  * describe(msg)   what a Database-Channel message holds (kind, size, name, caption HTML, duration, thumbnail ...)
  * iter_media()    streams a file chunk by chunk (used by Recover-side helpers; the backup uses utils/lbfetch.py)
  * flood helpers   FloodWait / RetryAfter -> sleep and retry

Tuning (env, all optional):
  LB_FILES        files downloaded at the same time during a backup            (default 5)
  LB_TRANSFERS    parallel Telegram requests in total                           (default 16)
  LB_CLIENTS      MTProto clients (= connection sets) used by a backup         (default 2)
  LB_STRIPE       MiB fetched by ONE stream_media call (sequential inside)      (default 4)
  LB_BUFFER_MB    max RAM held by downloaded-but-not-yet-written data           (default 128)
  LB_WRITE_MB     size of one write block into the zip                          (default 8)
  LB_PREFETCH     chunks in parallel per file for iter_media                    (default 4)
"""
import asyncio
import inspect
import logging
import os
from collections import deque

import config

log = logging.getLogger(__name__)


def _env_int(name: str, default: int, lo: int = 1) -> int:
    try:
        return max(lo, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


CHUNK = 1024 * 1024
PREFETCH = _env_int("LB_PREFETCH", 4)                    # chunks fetched in parallel per file (iter_media)
TRANSFERS = max(PREFETCH, _env_int("LB_TRANSFERS", 16))  # parallel Telegram transfers (all clients together)
FILES = _env_int("LB_FILES", 5)                          # files in flight during a backup
CLIENTS = _env_int("LB_CLIENTS", 2)                      # MTProto clients of a backup
STRIPE = _env_int("LB_STRIPE", 4)                        # MiB per stream_media call
BUFFER_MB = _env_int("LB_BUFFER_MB", 128)                # RAM budget of the download buffer
WRITE_MB = _env_int("LB_WRITE_MB", 8)                    # zip write block
KINDS = ("document", "video", "audio", "photo", "animation", "voice", "video_note", "sticker")


def has_tgcrypto() -> bool:
    try:
        import tgcrypto  # noqa: F401
        return True
    except ImportError:
        return False


# ------------------------------------------------------------------ client lifecycle
def _sem_value(client):
    sem = getattr(client, "get_file_semaphore", None)
    return getattr(sem, "_value", None) if sem is not None else None


class LBClient:
    """acquire() / release() are reference counted: the client is started once and stopped when nobody needs it."""

    def __init__(self):
        self.client = None
        self.extras = []            # more dedicated clients (own connections), only for a backup
        self.dedicated = False
        self.eff = None             # parallel get_file transfers ONE client really allows (None = unknown / unlimited)
        self.warnings = []          # plain-text problems, shown in the backup panel
        self.mode = ""
        self._refs = 0
        self._lock = None

    # ---------------------------------------------------------- lifecycle
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

    def _new_client(self, name: str):
        """-> (unstarted Client, constructor option missing?)"""
        from pyrogram import Client
        kwargs = dict(api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN,
                      in_memory=True, no_updates=True, sleep_threshold=60)
        try:
            client, missing = Client(name, max_concurrent_transmissions=TRANSFERS, **kwargs), False
        except TypeError:                       # older build without this option
            client, missing = Client(name, **kwargs), True
        if hasattr(client, "get_file_semaphore"):
            client.get_file_semaphore = asyncio.Semaphore(TRANSFERS)
        return client, missing

    async def _start(self):
        if not config.API_ID or not config.API_HASH:
            raise RuntimeError("API_ID / API_HASH are missing in .env")
        try:
            import pyrogram  # noqa: F401
        except ImportError:
            raise RuntimeError("pyrofork is not installed")
        self.warnings, self.eff, self.mode = [], None, ""
        tg = has_tgcrypto()
        if not tg:
            self.warnings.append("tgcrypto is NOT installed: MTProto crypto runs in pure Python (about 1-2 MB/s). "
                                 "pip install tgcrypto (the Docker image needs gcc to build it).")
        try:
            client, missing = self._new_client("localbackup")
            await client.start()
            sem = getattr(client, "get_file_semaphore", None)
            if sem is not None:                 # make sure start() did not replace it with the default of 1
                if _sem_value(client) is not None and _sem_value(client) < TRANSFERS:
                    client.get_file_semaphore = asyncio.Semaphore(TRANSFERS)
                self.eff = _sem_value(client)
            else:
                self.eff = TRANSFERS
                if missing:
                    self.warnings.append("This pyrofork build has neither max_concurrent_transmissions nor "
                                         "get_file_semaphore: the number of parallel transfers is not under our control.")
            if missing:
                log.warning("Local backup: Client(max_concurrent_transmissions=...) is not supported by this pyrofork "
                            "build; the download semaphore was set directly (%s).", self.eff)
            self.dedicated = True
            self.mode = f"DEDICATED CLIENT · {self.eff or '?'} TRANSFERS"
            log.info("Local backup: DEDICATED MTProto client started (transfers=%s, tgcrypto=%s).", self.eff,
                     "yes" if tg else "NO")
            return client
        except Exception as e:
            log.error("Local backup: dedicated MTProto client FAILED (%s: %s) - falling back to the SHARED session, "
                      "this is much slower.", type(e).__name__, e)
            from utils.mtproto import mtproto
            if mtproto.ready:
                self.dedicated = False
                self.eff = _sem_value(mtproto.client)
                self.mode = f"SHARED SESSION · {self.eff or '?'} TRANSFERS"
                self.warnings.insert(0, f"The dedicated MTProto client could not start ({type(e).__name__}: {str(e)[:120]}). "
                                        f"Using the SHARED bot session ({self.eff or '?'} parallel transfers): the backup "
                                        "will be MUCH slower. Check API_ID / API_HASH and the network, then restart the backup.")
                log.warning("Local backup: SHARED session in use, parallel transfers = %s", self.eff)
                return mtproto.client
            raise RuntimeError(f"MTProto session is not available ({mtproto.error or type(e).__name__})")

    async def pool(self, primary, want: int = None) -> list:
        """The clients a backup should spread its downloads over: `primary` + extra dedicated clients (own
        connections to the DCs). Extra clients that fail to start are skipped with a warning. Never raises."""
        want = CLIENTS if want is None else want
        if primary is not self.client or not self.dedicated or want <= 1 or self._lock is None:
            return [primary]
        async with self._lock:
            need = want - 1 - len(self.extras)
            if need > 0:
                base = len(self.extras) + 2
                res = await asyncio.gather(*[self._extra(f"localbackup{base + i}") for i in range(need)],
                                           return_exceptions=True)
                for r in res:
                    if isinstance(r, BaseException):
                        log.warning("Local backup: extra MTProto client failed to start (%s: %s)", type(r).__name__, r)
                        self.warnings.append(f"An extra MTProto client could not start ({type(r).__name__}); "
                                             "continuing with fewer connections.")
                    else:
                        self.extras.append(r)
            clients = [primary] + list(self.extras)
        self.mode = f"DEDICATED ×{len(clients)} · {self.eff or '?'} TRANSFERS EACH"
        log.info("Local backup: %d MTProto client(s) in the pool.", len(clients))
        return clients

    async def _extra(self, name: str):
        client, _ = self._new_client(name)
        await client.start()
        return client

    def effective_transfers(self, n_clients: int = 1) -> int:
        """How many parallel requests are useful in total."""
        return min(TRANSFERS, self.eff * max(1, n_clients)) if self.eff else TRANSFERS

    async def release(self):
        if self._lock is None:
            return
        async with self._lock:
            self._refs = max(0, self._refs - 1)
            if self._refs == 0 and self.client is not None:
                if self.dedicated:
                    for c in [self.client] + self.extras:
                        try:
                            await c.stop()
                        except Exception:
                            pass
                self.client, self.extras, self.dedicated = None, [], False
                self.eff, self.warnings, self.mode = None, [], ""


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
