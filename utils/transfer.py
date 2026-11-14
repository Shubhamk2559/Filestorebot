"""
utils/transfer.py
Transfer engine: copies EVERYTHING (videos, files, photos, text ... with their captions, albums stay albums)
from one chat/channel to another using the OWNER'S OWN Telegram account (user session), not the bot.

Why it is fast and light on the VPS:
  * Nothing is downloaded or uploaded. Telegram copies the messages on its own servers
    (messages.forwardMessages with drop_author=True = no "Forwarded from" tag, caption untouched).
  * Up to 100 messages move in ONE request -> 3000 videos = ~30 requests (a minute or two, RAM stays tiny).
  * FloodWait is handled automatically, progress is saved so a stopped job can be resumed.

Controls: pause / resume / stop (resume later) / cancel (discard), live speed, per-type counts of what the
source holds and what was copied, a "not forwarded" list with retry, and a one-shot delete of everything that was
copied into the destination (message ids are remembered in the DB).

Limit by design: if the source chat has "Restrict saving content" (protected content) Telegram itself refuses
to forward/copy, and this module does not try to bypass that - the job stops with a clear message.
"""
import asyncio
import logging
import os
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import config
from database import db

log = logging.getLogger(__name__)

FETCH = 200                                        # message ids read per request (Telegram max for get_messages)
CHUNK = 100                                        # messages per forward request (Telegram max)
DELAY = float(os.getenv("TRANSFER_DELAY", "0.8"))  # pause between forward requests (raise it if you hit many FloodWaits)

_col = db.db["transfer"]


def available() -> Optional[str]:
    """None if the feature can run, otherwise the reason it cannot."""
    if not config.API_ID or not config.API_HASH:
        return "API_ID / API_HASH are missing in .env (get them from https://my.telegram.org)."
    try:
        import pyrogram  # noqa: F401
    except ImportError:
        return "pyrofork is not installed."
    return None


# ===================================================================== user session
class UserSession:
    def __init__(self):
        self.client = None
        self.me = None
        self._login = None          # client used while the OTP login is in progress
        self._phone = None
        self._hash = None

    # ---------------------------------------------------------- storage
    async def saved_string(self) -> Optional[str]:
        doc = await _col.find_one({"_id": "session"})
        return doc.get("s") if doc else None

    async def _save_string(self, s: str):
        await _col.update_one({"_id": "session"}, {"$set": {"s": s, "at": time.time()}}, upsert=True)

    # ---------------------------------------------------------- client
    async def start(self, session_string: Optional[str] = None):
        """Start the user client from the saved (or given) session string. Returns the User."""
        from pyrogram import Client
        s = session_string or await self.saved_string()
        if not s:
            return None
        await self.stop()
        c = Client("transfer", api_id=config.API_ID, api_hash=config.API_HASH, session_string=s,
                   in_memory=True, no_updates=True, sleep_threshold=30)
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
        """The running user client (starts it from the saved session when needed) or None."""
        if self.client:
            return self.client
        try:
            await self.start()
        except Exception:
            log.exception("Saved user session could not be started")
            return None
        return self.client

    # ---------------------------------------------------------- phone login
    async def send_code(self, phone: str):
        from pyrogram import Client
        await self._drop_login()
        c = Client("transfer_login", api_id=config.API_ID, api_hash=config.API_HASH, in_memory=True, no_updates=True)
        await c.connect()
        try:
            sent = await c.send_code(phone)
        except Exception:
            await c.disconnect()
            raise
        self._login, self._phone, self._hash = c, phone, sent.phone_code_hash

    async def sign_in(self, code: str):
        """Returns True when logged in. Raises SessionPasswordNeeded when the 2-step password is required."""
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


# ===================================================================== helpers
def kind_icon(chat) -> str:
    t = str(getattr(chat, "type", "")).lower()
    if "channel" in t:
        return "📢"
    if "group" in t:                       # group + supergroup
        return "👥"
    if "bot" in t:
        return "🤖"
    return "👤"


def chat_title(chat) -> str:
    return (getattr(chat, "title", None) or " ".join(
        x for x in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if x) or str(chat.id))


async def list_dialogs(client, limit: int = 400) -> list:
    """[(chat_id, title, icon)] newest dialogs first - channels, groups, bots and private chats."""
    out = []
    async for d in client.get_dialogs(limit=limit):
        c = d.chat
        out.append((c.id, chat_title(c), kind_icon(c)))
    return out


async def resolve_chat(client, ref: str):
    """Accepts 'me'/'saved', @username, t.me/username, t.me/c/<id>/<msg>, -100... id or a plain numeric id."""
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


# ===================================================================== message kinds
KINDS = [
    ("video", "🎬", "Videos"), ("photo", "🖼", "Photos"), ("document", "📄", "Files"),
    ("audio", "🎵", "Audio"), ("voice", "🎤", "Voice"), ("animation", "🎞", "GIFs"),
    ("video_note", "⭕", "Round videos"), ("sticker", "🧩", "Stickers"),
    ("text", "💬", "Text"), ("other", "📎", "Other"),
]
KIND_INFO = {k: (icon, label) for k, icon, label in KINDS}

# live speed presets (seconds to wait between two forward requests)
SPEEDS = {
    "slow": ("🐢 Slow", max(DELAY * 3, 2.5)),
    "norm": ("⚖️ Normal", DELAY),
    "fast": ("⚡ Fast", 0.3),
}

MAX_FAILED = 2000          # how many "not forwarded" message ids are remembered for the list / retry
FINAL_STATES = ("done", "stopped", "cancelled", "error")
FATAL = ("ForwardsRestricted", "FORWARDS_RESTRICTED", "ChatWriteForbidden", "ChatAdminRequired",
         "UserBannedInChannel", "ChannelPrivate")

_sent = db.db["transfer_sent"]        # every message id this module created in a destination (for one-shot delete)


def kind_of(m) -> str:
    """video / photo / document / audio / voice / animation / video_note / sticker / text / other."""
    for k in ("video", "photo", "animation", "video_note", "voice", "audio", "sticker", "document"):
        if getattr(m, k, None):
            return k
    return "text" if getattr(m, "text", None) else "other"


def _is_flood(e) -> bool:
    return "FloodWait" in type(e).__name__


def _is_fatal(e) -> bool:
    return any(x in type(e).__name__ or x in str(e) for x in FATAL)


# ===================================================================== the job
@dataclass
class Job:
    src: int
    dst: int
    src_title: str
    dst_title: str
    start_id: int = 1
    top_id: int = 0
    scanned_to: int = 0            # highest source message id fully handled (copied or failed) - used by Resume
    scan_pos: int = 0              # how far the source scan has read
    total: int = 0                 # sendable messages found by the scan
    copied: int = 0
    failed: int = 0                # "not forwarded"
    skipped: int = 0               # service messages, deleted ids
    sent_n: int = 0                # messages created in the destination (tracked for delete)
    state: str = "starting"        # starting | scanning | running | done | stopped | cancelled | error
    error: str = ""
    began: float = field(default_factory=time.time)
    ended: float = 0.0
    scan_secs: float = 0.0
    stop: bool = False
    cancelled: bool = False
    paused: bool = False
    paused_total: float = 0.0
    flood_until: float = 0.0
    delay: float = DELAY
    speed_key: str = "norm"
    only_items: Optional[list] = None      # retry run: [(id, group_id, kind), ...] instead of scanning
    seen: Counter = field(default_factory=Counter)          # kind -> messages found in the source
    copied_by: Counter = field(default_factory=Counter)     # kind -> transferred
    failed_by: Counter = field(default_factory=Counter)     # kind -> not forwarded
    fail_reasons: Counter = field(default_factory=Counter)  # error name -> count
    failed_items: list = field(default_factory=list)        # [(id, kind, reason)]
    task: Optional[asyncio.Task] = None
    _paused_at: float = 0.0
    _resume: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __post_init__(self):
        self._resume.set()

    # ---- identity / state
    @property
    def key(self) -> str:
        return f"progress:{self.src}:{self.dst}"

    @property
    def running(self) -> bool:                 # True from start until the job has ended (paused counts as running)
        return self.state not in FINAL_STATES

    @property
    def phase(self) -> str:
        return "scan" if self.state in ("starting", "scanning") else "copy"

    # ---- controls
    def pause(self):
        if self.running and not self.paused:
            self.paused, self._paused_at = True, time.time()
            self._resume.clear()

    def resume(self):
        if self.paused:
            self.paused_total += time.time() - self._paused_at
            self.paused, self._paused_at = False, 0.0
            self._resume.set()

    def request_stop(self, cancel: bool = False):
        self.stop = True
        self.cancelled = self.cancelled or cancel
        self.resume()
        self._resume.set()

    def set_speed(self, key: str):
        if key in SPEEDS:
            self.speed_key, self.delay = key, SPEEDS[key][1]

    # ---- numbers
    def elapsed(self) -> float:
        now = self.ended or time.time()
        pause = self.paused_total + ((time.time() - self._paused_at) if self.paused else 0.0)
        return max(0.0, now - self.began - pause)

    @property
    def done_n(self) -> int:
        return self.copied + self.failed

    @property
    def left(self) -> int:
        return max(0, self.total - self.done_n)

    @property
    def percent(self) -> float:
        if self.phase == "scan":
            span = max(self.top_id - self.start_id + 1, 1)
            return min(100.0, max(0.0, (self.scan_pos - self.start_id + 1) / span * 100))
        if self.total <= 0:
            return 100.0 if self.state == "done" else 0.0
        return min(100.0, self.done_n / self.total * 100)

    @property
    def speed(self) -> float:
        el = self.elapsed() - self.scan_secs
        return self.copied / el if el > 0.5 else 0.0

    @property
    def eta(self) -> Optional[int]:
        sp = self.done_n / max(self.elapsed() - self.scan_secs, 0.001)
        if self.phase == "scan" or self.done_n <= 0 or self.left <= 0 or sp <= 0:
            return None
        return int(self.left / sp)

    @property
    def flood_left(self) -> int:
        return max(0, int(self.flood_until - time.time()))


CURRENT: Optional[Job] = None


async def last_progress(src: int, dst: int) -> int:
    doc = await _col.find_one({"_id": f"progress:{src}:{dst}"})
    return int(doc["last"]) if doc else 0


async def _save_progress(job: Job):
    if job.only_items is not None or job.scanned_to < job.start_id:
        return                                      # retry runs / nothing handled yet must not overwrite the resume point
    await _col.update_one({"_id": job.key}, {"$set": {"last": job.scanned_to, "at": time.time()}}, upsert=True)


async def clear_progress(src: int, dst: int):
    await _col.delete_one({"_id": f"progress:{src}:{dst}"})


# ---------------------------------------------------------------- sent-message tracking (for the one-shot delete)
async def _track(job: Job, ids: list):
    if not ids:
        return
    job.sent_n += len(ids)
    try:
        await _sent.insert_one({"src": job.src, "dst": job.dst, "src_title": job.src_title,
                                "dst_title": job.dst_title, "ids": ids, "job": id(job), "at": time.time()})
    except Exception:
        log.exception("Could not store the copied message ids")


async def tracked() -> list:
    """[{src, dst, src_title, dst_title, n, at}] - what this bot has copied, newest first."""
    pipe = [{"$group": {"_id": {"src": "$src", "dst": "$dst"}, "n": {"$sum": {"$size": "$ids"}},
                        "src_title": {"$last": "$src_title"}, "dst_title": {"$last": "$dst_title"},
                        "at": {"$max": "$at"}}},
            {"$sort": {"at": -1}}, {"$limit": 20}]
    out = []
    async for d in _sent.aggregate(pipe):
        if d["n"] > 0:
            out.append({"src": d["_id"]["src"], "dst": d["_id"]["dst"], "src_title": d["src_title"],
                        "dst_title": d["dst_title"], "n": d["n"], "at": d["at"]})
    return out


async def tracked_ids(src: int, dst: int) -> list:
    ids: list = []
    async for d in _sent.find({"src": src, "dst": dst}, {"ids": 1}):
        ids.extend(d.get("ids", []))
    return ids


async def _untrack(dst: int, ids: list, src: Optional[int] = None):
    q = {"dst": dst} if src is None else {"src": src, "dst": dst}
    await _sent.update_many(q, {"$pull": {"ids": {"$in": ids}}})
    await _sent.delete_many({**q, "ids": {"$size": 0}})


def _new_ids(res) -> list:
    """Message ids that a ForwardMessages call created in the destination."""
    ups = getattr(res, "updates", None)
    if ups is None and getattr(res, "update", None) is not None:
        ups = [res.update]
    out = []
    for u in ups or []:
        if type(u).__name__ in ("UpdateNewMessage", "UpdateNewChannelMessage"):
            mid = getattr(getattr(u, "message", None), "id", None)
            if mid:
                out.append(int(mid))
    return out


# ---------------------------------------------------------------- scan + copy
def _sendable(m) -> bool:
    return bool(m) and not getattr(m, "empty", False) and not getattr(m, "service", None)


async def _gate(job: Job):
    """Blocks while the job is paused (Stop / Cancel wake it up)."""
    while job.paused and not job.stop:
        await job._resume.wait()


async def _sleep(job: Job, sec: float):
    end = time.time() + sec
    while not job.stop and time.time() < end:
        await asyncio.sleep(min(1.0, max(0.0, end - time.time())))


def _cut(items: list, i: int) -> int:
    """End index of the next <=100 chunk starting at i, never splitting an album."""
    n = min(i + CHUNK, len(items))
    g = items[n - 1][1]
    while g and n < len(items) and items[n][1] == g:
        n += 1
    return n


def _fail(job: Job, mid: int, kind: str, reason: str):
    job.failed += 1
    job.failed_by[kind] += 1
    job.fail_reasons[reason] += 1
    if len(job.failed_items) < MAX_FAILED:
        job.failed_items.append((mid, kind, reason))


async def _scan(client, job: Job) -> list:
    """Read the source once: count every type and keep only (id, album id, kind) of what can be copied."""
    items: list = []
    pos = job.start_id
    while pos <= job.top_id and not job.stop:
        await _gate(job)
        if job.stop:
            break
        ids = list(range(pos, min(pos + FETCH, job.top_id + 1)))
        while True:
            try:
                got = await client.get_messages(job.src, ids)
                break
            except Exception as e:
                if _is_flood(e):
                    job.flood_until = time.time() + int(getattr(e, "value", 10)) + 1
                    await _sleep(job, job.flood_left)
                    if job.stop:
                        return items
                    continue
                raise
        if not isinstance(got, list):
            got = [got]
        for m in got:
            if _sendable(m):
                k = kind_of(m)
                items.append((m.id, getattr(m, "media_group_id", None), k))
                job.seen[k] += 1
            else:
                job.skipped += 1
        job.skipped += max(0, len(ids) - len(got))
        pos = ids[-1] + 1
        job.scan_pos = pos - 1
        job.total = len(items)
    return items


async def _forward(client, job: Job, chunk: list):
    """One request, no forward tag (drop_author). Falls back to copy per message if Telegram rejects the batch."""
    from pyrogram.raw import functions
    ids = [c[0] for c in chunk]
    try:
        res = await client.invoke(functions.messages.ForwardMessages(
            from_peer=await client.resolve_peer(job.src),
            id=ids,
            random_id=[random.getrandbits(62) for _ in ids],
            to_peer=await client.resolve_peer(job.dst),
            drop_author=True,
            silent=True,
        ))
    except Exception as e:
        if _is_flood(e) or _is_fatal(e):
            raise
        log.warning("Batch forward failed (%s) - copying one by one", type(e).__name__)
    else:
        new = _new_ids(res)
        await _track(job, new)
        ok = min(len(new), len(ids)) if new else len(ids)
        job.copied += ok
        for _id, _g, k in chunk:
            job.copied_by[k] += 1
        if ok < len(ids):                                  # Telegram created fewer messages than asked
            miss = len(ids) - ok
            job.failed += miss
            job.fail_reasons["NotForwarded"] += miss
        return

    for _id, _g, k in chunk:                               # slow path, only for the odd batch
        for _attempt in range(3):
            try:
                sent = await client.copy_message(job.dst, job.src, _id)
                await _track(job, [sent.id])
                job.copied += 1
                job.copied_by[k] += 1
                break
            except Exception as e:
                if _is_flood(e):
                    await _sleep(job, int(getattr(e, "value", 5)) + 1)
                    continue
                if _is_fatal(e):
                    raise
                _fail(job, _id, k, type(e).__name__)
                log.warning("Could not copy message %s: %s", _id, type(e).__name__)
                break
        else:
            _fail(job, _id, k, "FloodWait")


async def _send(client, job: Job, chunk: list) -> bool:
    """True when the chunk was handled, False when Stop/Cancel interrupted a FloodWait before it was sent."""
    while True:
        try:
            await _forward(client, job, chunk)
            return True
        except Exception as e:
            if _is_flood(e):
                wait = int(getattr(e, "value", 10)) + 1
                log.info("FloodWait %ss", wait)
                job.flood_until = time.time() + wait
                await _sleep(job, wait)
                if job.stop:
                    return False
                continue
            raise


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
            job.error = ("This source has 'Restrict saving content' turned on. Telegram does not allow "
                         "forwarding/copying from it, so it cannot be transferred.")
            return

        if job.only_items is not None:                     # retry of the "not forwarded" ones
            items = list(job.only_items)
            for _id, _g, k in items:
                job.seen[k] += 1
            job.total = len(items)
        else:
            async for m in client.get_chat_history(job.src, limit=1):
                job.top_id = m.id
            if not job.top_id:
                job.state, job.error = "error", "Source chat is empty (or your account cannot read it)."
                return
            job.state = "scanning"
            job.scanned_to = job.start_id - 1
            items = await _scan(client, job)
            job.total = len(items)
            if job.stop:
                job.state = "cancelled" if job.cancelled else "stopped"
                return

        job.scan_secs = job.elapsed()
        job.state = "running"
        i, last_save = 0, time.time()
        while i < len(items) and not job.stop:
            await _gate(job)
            if job.stop:
                break
            j = _cut(items, i)
            chunk = items[i:j]
            if not await _send(client, job, chunk):
                break
            i = j
            job.scanned_to = chunk[-1][0]
            await _sleep(job, job.delay)
            if time.time() - last_save > 10:
                await _save_progress(job)
                last_save = time.time()

        if job.stop:
            job.state = "cancelled" if job.cancelled else "stopped"
        else:
            if job.only_items is None:
                job.scanned_to = job.top_id
            job.state = "done"
    except Exception as e:
        name = type(e).__name__
        log.exception("Transfer failed")
        job.state = "error"
        if "ForwardsRestricted" in name or "FORWARDS_RESTRICTED" in str(e):
            job.error = "Telegram refused: this chat restricts saving/forwarding content."
        elif "ChatWriteForbidden" in name or "ChatAdminRequired" in name:
            job.error = "Your account cannot post in the destination chat."
        else:
            job.error = f"{name}: {e}"
    finally:
        job.ended = time.time()
        job.paused = False
        try:
            if job.state == "cancelled":
                await clear_progress(job.src, job.dst)     # a cancelled run is thrown away - no "Resume" offered
            else:
                await _save_progress(job)
        except Exception:
            log.exception("progress save failed")


def start_job(job: Job) -> Job:
    global CURRENT
    CURRENT = job
    job.task = asyncio.create_task(_run(job))
    return job


def busy() -> bool:
    return CURRENT is not None and CURRENT.running


def retry_job(old: Job) -> Optional[Job]:
    """New job that sends only the messages the previous run could not forward."""
    if not old.failed_items:
        return None
    items = [(mid, None, k) for mid, k, _r in old.failed_items]
    return Job(src=old.src, dst=old.dst, src_title=old.src_title, dst_title=old.dst_title, only_items=items)


# ===================================================================== one-shot delete (cleanup)
@dataclass
class CleanJob:
    dst: int
    title: str
    mode: str                      # "tracked" = only what the bot copied | "all" = the whole chat
    src: Optional[int] = None      # for "tracked"
    total: int = 0
    deleted: int = 0
    missing: int = 0               # already gone
    failed: int = 0
    state: str = "starting"        # starting | scanning | running | done | stopped | error
    error: str = ""
    began: float = field(default_factory=time.time)
    ended: float = 0.0
    stop: bool = False
    task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self.state in ("starting", "scanning", "running")

    @property
    def percent(self) -> float:
        return min(100.0, (self.deleted + self.missing + self.failed) / self.total * 100) if self.total else 0.0


CLEAN: Optional[CleanJob] = None


async def count_chat(client, chat_id: int) -> int:
    try:
        return int(await client.get_chat_history_count(chat_id))
    except Exception:
        return 0


async def _run_clean(cj: CleanJob):
    client = await session.ensure()
    if not client:
        cj.state, cj.error = "error", "Login first."
        cj.ended = time.time()
        return
    try:
        cj.state = "scanning"
        if cj.mode == "tracked":
            ids = await tracked_ids(cj.src, cj.dst)
        else:
            ids = []
            async for m in client.get_chat_history(cj.dst):
                ids.append(m.id)
                cj.total = len(ids)
                if cj.stop:
                    break
        ids = sorted(set(ids), reverse=True)               # newest first
        cj.total = len(ids)
        cj.state = "running"
        bad = 0
        for i in range(0, len(ids), CHUNK):
            if cj.stop:
                break
            batch = ids[i:i + CHUNK]
            while True:
                try:
                    n = await client.delete_messages(cj.dst, batch)
                    n = n if isinstance(n, int) and n >= 0 else len(batch)
                    cj.deleted += n
                    cj.missing += len(batch) - n
                    bad = 0
                    break
                except Exception as e:
                    if _is_flood(e):
                        await asyncio.sleep(int(getattr(e, "value", 10)) + 1)
                        continue
                    cj.failed += len(batch)
                    cj.error = f"{type(e).__name__}"
                    bad += 1
                    log.warning("delete batch failed: %s", type(e).__name__)
                    break
            if bad:
                if bad >= 3:
                    raise RuntimeError(f"Could not delete ({cj.error}). Is your account allowed to delete there?")
            else:
                await _untrack(cj.dst, batch, cj.src if cj.mode == "tracked" else None)
            await asyncio.sleep(0.4)
        cj.state = "stopped" if cj.stop else "done"
        if cj.state == "done" and cj.mode == "all":
            await _sent.delete_many({"dst": cj.dst})
    except Exception as e:
        log.exception("Cleanup failed")
        cj.state, cj.error = "error", f"{type(e).__name__}: {e}"
    finally:
        cj.ended = time.time()


def start_clean(cj: CleanJob) -> CleanJob:
    global CLEAN
    CLEAN = cj
    cj.task = asyncio.create_task(_run_clean(cj))
    return cj


def clean_busy() -> bool:
    return CLEAN is not None and CLEAN.running
