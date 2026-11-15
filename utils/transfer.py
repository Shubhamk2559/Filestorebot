"""
utils/transfer.py
Transfer engine: copies EVERYTHING (videos, files, photos, text ... with their captions, albums stay albums)
from one chat/channel to another using the OWNER'S OWN Telegram account (user session), not the bot.

Why it is fast and light on the VPS:
  * Nothing is downloaded or uploaded. Telegram copies the messages on its own servers
    (messages.forwardMessages with drop_author=True = no "Forwarded from" tag, caption untouched).
  * Up to 100 messages move in ONE request -> 3000 videos = ~30 requests (a minute or two, RAM stays tiny).
  * FloodWait is handled automatically, progress is saved so a stopped job can be resumed.

Limit by design: if the source chat has "Restrict saving content" (protected content) Telegram itself refuses
to forward/copy, and this module does not try to bypass that - the job stops with a clear message.
"""
import asyncio
import logging
import os
import random
import time
from collections import Counter, defaultdict
from typing import Optional

import config
from database import db

log = logging.getLogger(__name__)

FETCH = 200
CHUNK = 100
DELAY = float(os.getenv("TRANSFER_DELAY", "0.8"))

_col = db.db["transfer"]            # resume points (progress:<src>:<dst>) + the saved login session
_sent = db.db["transfer_sent"]      # ids of the messages this bot copied into a destination (for one-tap delete)


def available() -> Optional[str]:
    """None if the feature can run, otherwise the reason it cannot."""
    if not config.API_ID or not config.API_HASH:
        return "API_ID / API_HASH are missing in .env (get them from https://my.telegram.org)."
    try:
        import pyrogram
    except ImportError:
        return "pyrofork is not installed."
    return None


class UserSession:
    def __init__(self):
        self.client = None
        self.me = None
        self._login = None
        self._phone = None
        self._hash = None

    async def saved_string(self) -> Optional[str]:
        doc = await _col.find_one({"_id": "session"})
        return doc.get("s") if doc else None

    async def _save_string(self, s: str):
        await _col.update_one(
            {"_id": "session"},
            {"$set": {"s": s, "at": time.time()}},
            upsert=True
        )

    async def start(self, session_string: Optional[str] = None):
        from pyrogram import Client

        s = session_string or await self.saved_string()
        if not s:
            return None

        await self.stop()

        c = Client(
            "transfer",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=s,
            in_memory=True,
            no_updates=True,
            sleep_threshold=30
        )

        await c.start()
        self.client = c
        self.me = await c.get_me()

        if session_string:
            await self._save_string(session_string)

        return self.me

    async def stop(self):
        if self.client:
            try:
                await self.client.stop()
            except Exception:
                pass

        self.client = None

    async def logout(self):
        c = self.client
        self.client = None
        self.me = None

        if c:
            try:
                await c.log_out()
            except Exception:
                try:
                    await c.stop()
                except Exception:
                    pass

        await _col.delete_one({"_id": "session"})

    async def ensure(self):
        if self.client:
            return self.client

        try:
            await self.start()
        except Exception:
            log.exception("Saved user session could not be started")
            return None

        return self.client

    async def send_code(self, phone: str):
        from pyrogram import Client

        await self._drop_login()

        c = Client(
            "transfer_login",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            in_memory=True,
            no_updates=True
        )

        await c.connect()

        try:
            sent = await c.send_code(phone)
        except Exception:
            await c.disconnect()
            raise

        self._login = c
        self._phone = phone
        self._hash = sent.phone_code_hash

    async def sign_in(self, code: str):
        await self._login.sign_in(self._phone, self._hash, code)
        await self._finish_login()
        return True

    async def check_password(self, password: str):
        await self._login.check_password(password)
        await self._finish_login()
        return True

    async def _finish_login(self):
        s = await self._login.export_session_string()
        await self._drop_login()
        await self.start(s)

    async def _drop_login(self):
        c, self._login = self._login, None

        if c:
            try:
                await c.disconnect()
            except Exception:
                pass


session = UserSession()


def kind_icon(chat) -> str:
    t = str(getattr(chat, "type", "")).lower()

    if "channel" in t:
        return "📢"
    if "group" in t:
        return "👥"
    if "bot" in t:
        return "🤖"

    return "👤"


def chat_title(chat) -> str:
    return (
        getattr(chat, "title", None)
        or " ".join(
            x for x in (
                getattr(chat, "first_name", None),
                getattr(chat, "last_name", None)
            ) if x
        )
        or str(chat.id)
    )


async def list_dialogs(client, limit: int = 400) -> list:
    out = []

    async for d in client.get_dialogs(limit=limit):
        c = d.chat
        out.append((c.id, chat_title(c), kind_icon(c)))

    return out


async def resolve_chat(client, ref: str):
    ref = (ref or "").strip()

    if ref.lower() in ("me", "saved", "saved messages"):
        return await client.get_chat("me")

    if "t.me/c/" in ref:
        part = ref.split("t.me/c/")[1].split("/")[0]
        return await client.get_chat(int("-100" + part))

    if "t.me/" in ref and "/+" not in ref and "joinchat" not in ref:
        ref = ref.split("t.me/")[1].split("/")[0].split("?")[0]

    ref = ref.lstrip("@")

    try:
        return await client.get_chat(int(ref))
    except ValueError:
        return await client.get_chat(ref)


# ===================================================================== kinds / speeds
KINDS = [
    ("video", "🎬", "Videos"), ("photo", "🖼", "Photos"), ("document", "📄", "Files"),
    ("audio", "🎵", "Audio"), ("voice", "🎙", "Voice"), ("animation", "🎞", "GIFs"),
    ("sticker", "🏷", "Stickers"), ("text", "💬", "Text"), ("other", "📎", "Other"),
]
KIND_INFO = {k: (ic, name) for k, ic, name in KINDS}

# key -> (button label, seconds to wait between two batches of 100 messages)
SPEEDS = {
    "slow": ("🐢 Slow", DELAY * 2.5),
    "normal": ("⚡ Normal", DELAY),
    "fast": ("🚀 Fast", max(DELAY / 3, 0.15)),
}


def kind_of(m) -> str:
    for k in ("video", "photo", "animation", "document", "audio", "voice", "sticker"):
        if getattr(m, k, None):
            return k
    if getattr(m, "video_note", None):
        return "video"
    if getattr(m, "text", None):
        return "text"
    return "other"


def _is_flood(e) -> bool:
    return "FloodWait" in type(e).__name__


def _is_restricted(e) -> bool:
    return "ForwardsRestricted" in type(e).__name__ or "FORWARDS_RESTRICTED" in str(e)


def _flood_secs(e) -> int:
    v = getattr(e, "value", 10)
    try:
        return int(v) + 1
    except Exception:
        return 11


# ===================================================================== transfer job
class Job:
    """One copy run. Everything the progress screen shows lives here."""

    def __init__(self, src: int, dst: int, src_title: str, dst_title: str, start_id: int = 1, only_items=None):
        self.src, self.dst = src, dst
        self.src_title, self.dst_title = src_title, dst_title
        self.start_id = max(1, int(start_id or 1))
        self.only_items = only_items            # None = whole chat; list of (message id, kind) = retry run
        self.top_id = 0
        self.scanned_to = 0
        self.total = 0                          # sendable messages found in the source
        self.copied = 0
        self.failed = 0
        self.skipped = 0
        self.sent_n = 0                         # destination messages recorded for one-tap delete
        self.seen = defaultdict(int)            # kind -> found in source
        self.copied_by = defaultdict(int)
        self.failed_by = defaultdict(int)
        self.fail_reasons = Counter()
        self.failed_items = []                  # (message id, kind, reason)
        self.state = "starting"                 # starting | scanning | running | done | stopped | cancelled | error
        self.phase = "scan"                     # scan | copy
        self.error = ""
        self.began = time.time()
        self.copy_began = 0.0
        self.ended = 0.0
        self.stop = False
        self.cancel = False
        self.paused = False
        self.flood_left = 0
        self.speed_key = "normal"
        self.task: Optional[asyncio.Task] = None
        self._go = asyncio.Event()
        self._go.set()

    # ---- state
    @property
    def key(self) -> str:
        return f"progress:{self.src}:{self.dst}"

    @property
    def running(self) -> bool:
        return self.state in ("starting", "scanning", "running")

    @property
    def left(self) -> int:
        return max(self.total - self.copied - self.failed, 0)

    @property
    def percent(self) -> float:
        if self.state == "done":
            return 100.0
        if self.phase == "scan" or not self.total:
            return 0.0
        return min(100.0, (self.copied + self.failed) / self.total * 100)

    @property
    def speed(self) -> float:
        if not self.copy_began:
            return 0.0
        el = (self.ended or time.time()) - self.copy_began
        return self.copied / el if el > 0 else 0.0

    @property
    def eta(self) -> Optional[int]:
        sp = self.speed
        return int(self.left / sp) if sp > 0 and self.left > 0 else None

    @property
    def delay(self) -> float:
        return SPEEDS.get(self.speed_key, SPEEDS["normal"])[1]

    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.began

    # ---- controls (used by the buttons)
    def pause(self):
        self.paused = True
        self._go.clear()

    def resume(self):
        self.paused = False
        self._go.set()

    def request_stop(self, cancel: bool = False):
        self.stop = True
        self.cancel = self.cancel or cancel
        self._go.set()

    def set_speed(self, key: str):
        if key in SPEEDS:
            self.speed_key = key

    async def gate(self) -> bool:
        """Wait while paused. False = the job must end now."""
        await self._go.wait()
        return not self.stop


CURRENT: Optional[Job] = None


async def last_progress(src: int, dst: int) -> int:
    doc = await _col.find_one({"_id": f"progress:{src}:{dst}"})
    return int(doc["last"]) if doc else 0


async def _save_progress(job: Job):
    await _col.update_one({"_id": job.key}, {"$set": {"last": job.scanned_to, "at": time.time()}}, upsert=True)


async def clear_progress(src: int, dst: int):
    await _col.delete_one({"_id": f"progress:{src}:{dst}"})


def _sendable(m) -> bool:
    return bool(m) and not getattr(m, "empty", False) and not getattr(m, "service", None)


def _split(items: list):
    """Batches of <= CHUNK messages; an album is never cut in half."""
    i = 0
    while i < len(items):
        n = min(CHUNK, len(items) - i)
        if i + n < len(items):
            g = items[i + n - 1][2]
            while g and i + n < len(items) and items[i + n][2] == g:
                n += 1
        yield items[i:i + n]
        i += n


async def _flood(job: Job, wait: int):
    log.info("FloodWait %ss", wait)
    job.flood_left = wait
    while job.flood_left > 0 and not job.stop:
        await asyncio.sleep(1)
        job.flood_left -= 1
    job.flood_left = 0


async def _retry_flood(job: Job, fn):
    while True:
        try:
            return await fn()
        except Exception as e:
            if _is_flood(e):
                await _flood(job, _flood_secs(e))
                if job.stop:
                    raise
                continue
            raise


def _new_ids(res) -> list:
    """Ids of the messages Telegram just created in the destination (from the ForwardMessages reply)."""
    out = []
    for u in getattr(res, "updates", None) or []:
        if type(u).__name__ in ("UpdateNewMessage", "UpdateNewChannelMessage"):
            mid = getattr(getattr(u, "message", None), "id", None)
            if mid:
                out.append(mid)
    return out


async def _record(job: Job, ids: list):
    if not ids:
        return
    job.sent_n += len(ids)
    try:
        await _sent.update_one(
            {"_id": f"sent:{job.src}:{job.dst}"},
            {"$set": {"src": job.src, "dst": job.dst, "src_title": job.src_title, "dst_title": job.dst_title,
                      "at": time.time()},
             "$push": {"ids": {"$each": list(ids)}}},
            upsert=True)
    except Exception:
        log.exception("could not record copied ids")


async def _forward(client, job: Job, ids: list) -> list:
    from pyrogram.raw import functions
    res = await client.invoke(functions.messages.ForwardMessages(
        from_peer=await client.resolve_peer(job.src),
        id=ids,
        random_id=[random.getrandbits(62) for _ in ids],
        to_peer=await client.resolve_peer(job.dst),
        drop_author=True,
        silent=True,
    ))
    return _new_ids(res)


async def _send(client, job: Job, chunk: list):
    ids = [c[0] for c in chunk]
    new = None
    while True:
        try:
            new = await _forward(client, job, ids)
            break
        except Exception as e:
            if _is_flood(e):
                await _flood(job, _flood_secs(e))
                if job.stop:
                    return
                continue
            if _is_restricted(e):
                raise
            log.warning("Batch forward failed (%s) - copying one by one", type(e).__name__)
            break

    if new is not None:
        job.copied += len(chunk)
        for _mid, k, _g in chunk:
            job.copied_by[k] += 1
        await _record(job, new)
        return

    for mid, k, _g in chunk:
        if job.stop:
            return
        for _attempt in range(3):
            try:
                msg = await client.copy_message(job.dst, job.src, mid)
                job.copied += 1
                job.copied_by[k] += 1
                await _record(job, [msg.id] if msg is not None and getattr(msg, "id", None) else [])
                break
            except Exception as e:
                if _is_flood(e):
                    await _flood(job, _flood_secs(e))
                    continue
                if _is_restricted(e):
                    raise
                _fail(job, mid, k, type(e).__name__)
                break
        else:
            _fail(job, mid, k, "FloodWait")


def _fail(job: Job, mid: int, kind: str, why: str):
    job.failed += 1
    job.failed_by[kind] += 1
    job.fail_reasons[why] += 1
    job.failed_items.append((mid, kind, why))
    log.warning("Could not copy message %s: %s", mid, why)


async def _scan(client, job: Job) -> list:
    """Read the source once and remember (id, kind, album id) of every copyable message."""
    job.state, job.phase = "scanning", "scan"
    async for m in client.get_chat_history(job.src, limit=1):
        job.top_id = m.id
    items: list = []
    pos = job.start_id
    while pos <= job.top_id:
        if not await job.gate():
            return items
        ids = list(range(pos, min(pos + FETCH, job.top_id + 1)))
        got = await _retry_flood(job, lambda: client.get_messages(job.src, ids))
        if not isinstance(got, list):
            got = [got]
        for m in got:
            if _sendable(m):
                k = kind_of(m)
                items.append((m.id, k, getattr(m, "media_group_id", None)))
                job.total += 1
                job.seen[k] += 1
            else:
                job.skipped += 1
        pos = ids[-1] + 1
    return items


async def _run(job: Job):
    client = await session.ensure()
    if not client:
        job.state, job.error = "error", "Login first."
        job.ended = time.time()
        return

    try:
        src = await client.get_chat(job.src)
        if getattr(src, "has_protected_content", False):
            job.state = "error"
            job.error = ("This source chat has 'Restrict saving content' turned on. Telegram does not allow "
                         "forwarding/copying from it, so it cannot be transferred.")
            return

        if job.only_items is None:
            items = await _scan(client, job)
        else:
            items = [(mid, k, None) for mid, k in job.only_items]
            job.total = len(items)
            for _mid, k, _g in items:
                job.seen[k] += 1

        if not job.stop:
            if not items:
                job.state = "error"
                job.error = "Nothing to copy (source empty, start ID too high, or your account cannot read it)."
                return
            job.phase, job.state = "copy", "running"
            job.copy_began = time.time()
            last_save = time.time()
            for chunk in _split(items):
                if not await job.gate():
                    break
                await _send(client, job, chunk)
                job.scanned_to = max(job.scanned_to, chunk[-1][0])
                if job.only_items is None and time.time() - last_save > 10:
                    await _save_progress(job)
                    last_save = time.time()
                if not job.stop:
                    await asyncio.sleep(job.delay)

        if job.stop:
            job.state = "cancelled" if job.cancel else "stopped"
        else:
            if job.only_items is None:
                job.scanned_to = max(job.scanned_to, job.top_id)
            job.state = "done"

    except Exception as e:
        name = type(e).__name__
        if job.stop and _is_flood(e):
            job.state = "cancelled" if job.cancel else "stopped"
        else:
            log.exception("Transfer failed")
            job.state = "error"
            if _is_restricted(e):
                job.error = "Telegram refused: this chat restricts saving/forwarding content."
            elif "ChatWriteForbidden" in name or "ChatAdminRequired" in name:
                job.error = "Your account cannot post in the destination chat."
            else:
                job.error = f"{name}: {e}"

    finally:
        job.ended = time.time()
        job.flood_left = 0
        if job.only_items is None:
            try:
                if job.state == "cancelled":
                    await clear_progress(job.src, job.dst)
                elif job.scanned_to:
                    await _save_progress(job)
            except Exception:
                log.exception("could not save progress")


def start_job(job: Job) -> Job:
    global CURRENT
    CURRENT = job
    job.task = asyncio.create_task(_run(job))
    return job


def busy() -> bool:
    return CURRENT is not None and CURRENT.running


def retry_job(job: Job) -> Optional[Job]:
    """A new job that only re-sends the messages the finished job could not forward."""
    if not job or job.running or not job.failed_items:
        return None
    items = [(mid, k) for mid, k, _why in job.failed_items]
    return Job(src=job.src, dst=job.dst, src_title=job.src_title, dst_title=job.dst_title, only_items=items)


# ===================================================================== tracked copies + delete jobs
async def tracked() -> list:
    """Destinations that hold messages this bot copied: [{src, dst, src_title, dst_title, n}]."""
    out = []
    try:
        cur = _sent.aggregate([
            {"$project": {"src": 1, "dst": 1, "src_title": 1, "dst_title": 1,
                          "n": {"$size": {"$ifNull": ["$ids", []]}}}},
            {"$match": {"n": {"$gt": 0}}},
        ])
        async for d in cur:
            out.append({"src": d.get("src"), "dst": d.get("dst"), "n": d["n"],
                        "src_title": d.get("src_title") or str(d.get("src")),
                        "dst_title": d.get("dst_title") or str(d.get("dst"))})
    except Exception:
        log.exception("could not list tracked copies")
    return out


async def count_chat(client, cid: int) -> int:
    try:
        return int(await client.get_chat_history_count(cid))
    except Exception:
        return 0


class CleanJob:
    """Delete what the bot copied into a chat (mode 'tracked') or every message of a chat (mode 'all')."""

    def __init__(self, dst: int, title: str, mode: str = "tracked", src: int = 0):
        self.dst, self.title, self.mode, self.src = dst, title, mode, src
        self.state = "starting"                 # starting | scanning | running | done | stopped | error
        self.total = self.deleted = self.missing = self.failed = 0
        self.error = ""
        self.stop = False
        self.began = time.time()
        self.task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self.state in ("starting", "scanning", "running")

    @property
    def percent(self) -> float:
        if self.state == "done":
            return 100.0
        if not self.total:
            return 0.0
        return min(100.0, (self.deleted + self.missing + self.failed) / self.total * 100)


CLEAN: Optional[CleanJob] = None


async def _clean_run(cj: CleanJob):
    client = await session.ensure()
    if not client:
        cj.state, cj.error = "error", "Login first."
        return
    key = f"sent:{cj.src}:{cj.dst}"
    try:
        cj.state = "scanning"
        ids: list = []
        if cj.mode == "tracked":
            doc = await _sent.find_one({"_id": key})
            ids = list(doc.get("ids", [])) if doc else []
        else:
            async for m in client.get_chat_history(cj.dst):
                ids.append(m.id)
                cj.total = len(ids)
                if cj.stop:
                    break
        cj.total = len(ids)
        cj.state = "running"

        for i in range(0, len(ids), 100):
            if cj.stop:
                break
            batch = ids[i:i + 100]
            n = None
            while n is None:
                try:
                    n = int(await client.delete_messages(cj.dst, batch) or 0)
                except Exception as e:
                    if _is_flood(e):
                        await asyncio.sleep(_flood_secs(e))
                        continue
                    log.warning("delete batch failed: %s", type(e).__name__)
                    cj.failed += len(batch)
                    cj.error = f"{type(e).__name__}: {e}"
                    n = -1
            if n >= 0:
                n = min(n, len(batch))
                cj.deleted += n
                cj.missing += len(batch) - n
                if cj.mode == "tracked":
                    await _sent.update_one({"_id": key}, {"$pull": {"ids": {"$in": batch}}})
            await asyncio.sleep(0.4)

        if cj.stop:
            cj.state = "stopped"
        else:
            cj.state = "done"
            if cj.mode == "tracked":
                await _sent.delete_one({"_id": key, "ids": []})
            elif not cj.failed:
                await _sent.delete_many({"dst": cj.dst})
    except Exception as e:
        log.exception("Delete job failed")
        cj.state, cj.error = "error", f"{type(e).__name__}: {e}"


def start_clean(cj: CleanJob) -> CleanJob:
    global CLEAN
    CLEAN = cj
    cj.task = asyncio.create_task(_clean_run(cj))
    return cj


def clean_busy() -> bool:
    return CLEAN is not None and CLEAN.running
