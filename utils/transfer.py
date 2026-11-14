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
from dataclasses import dataclass, field
from typing import Optional

import config
from database import db

log = logging.getLogger(__name__)

FETCH = 200                                        # message ids read per request (Telegram max for get_messages)
CHUNK = 100                                        # messages per forward request (Telegram max)
DELAY = float(os.getenv("TRANSFER_DELAY", "0.8"))  # pause between forward requests (raise it if you hit many FloodWaits)

_col = db["transfer"]


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


# ===================================================================== the job
@dataclass
class Job:
    src: int
    dst: int
    src_title: str
    dst_title: str
    start_id: int = 1
    top_id: int = 0
    scanned_to: int = 0            # highest source message id handled so far
    copied: int = 0
    failed: int = 0
    skipped: int = 0               # service messages, deleted ids
    state: str = "starting"        # starting | running | done | stopped | error
    error: str = ""
    began: float = field(default_factory=time.time)
    ended: float = 0.0
    stop: bool = False
    task: Optional[asyncio.Task] = None

    @property
    def key(self) -> str:
        return f"progress:{self.src}:{self.dst}"

    @property
    def running(self) -> bool:
        return self.state in ("starting", "running")

    @property
    def percent(self) -> float:
        span = max(self.top_id - self.start_id + 1, 1)
        return min(100.0, max(0.0, (self.scanned_to - self.start_id + 1) / span * 100))

    @property
    def speed(self) -> float:
        el = (self.ended or time.time()) - self.began
        return self.copied / el if el > 0 else 0.0

    @property
    def eta(self) -> Optional[int]:
        done = self.scanned_to - self.start_id + 1
        el = time.time() - self.began
        left = self.top_id - self.scanned_to
        if done <= 0 or left <= 0 or el <= 0:
            return None
        return int(left / (done / el))


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


def _chunks(buf: list, final: bool):
    """Cut `buf` into <=100 pieces without splitting an album. Yields lists; the unfinished tail stays in `buf`."""
    out = []
    while len(buf) > CHUNK or (final and buf):
        n = min(CHUNK, len(buf))
        if n < len(buf):                                   # don't cut inside an album
            g = getattr(buf[n - 1], "media_group_id", None)
            while g and n < len(buf) and getattr(buf[n], "media_group_id", None) == g:
                n += 1
        out.append(buf[:n])
        del buf[:n]
    return out


async def _forward(client, job: Job, msgs: list):
    """One request, no forward tag (drop_author). Falls back to copy per message if Telegram rejects the batch."""
    from pyrogram.raw import functions
    ids = [m.id for m in msgs]
    try:
        await client.invoke(functions.messages.ForwardMessages(
            from_peer=await client.resolve_peer(job.src),
            id=ids,
            random_id=[random.getrandbits(62) for _ in ids],
            to_peer=await client.resolve_peer(job.dst),
            drop_author=True,
            silent=True,
        ))
        job.copied += len(ids)
        return
    except Exception as e:
        name = type(e).__name__
        if "FloodWait" in name:
            raise
        if "ForwardsRestricted" in name or "FORWARDS_RESTRICTED" in str(e):
            raise
        log.warning("Batch forward failed (%s) - copying one by one", name)

    for m in msgs:                                         # slow path, only for the odd batch
        for attempt in range(3):
            try:
                await m.copy(job.dst)
                job.copied += 1
                break
            except Exception as e:
                name = type(e).__name__
                if "FloodWait" in name:
                    await asyncio.sleep(getattr(e, "value", 5) + 1)
                    continue
                if "ForwardsRestricted" in name:
                    raise
                job.failed += 1
                log.warning("Could not copy message %s: %s", m.id, name)
                break


async def _send(client, job: Job, msgs: list):
    while True:
        try:
            await _forward(client, job, msgs)
            return
        except Exception as e:
            if "FloodWait" in type(e).__name__:
                wait = int(getattr(e, "value", 10)) + 1
                log.info("FloodWait %ss", wait)
                job.state = f"waiting {wait}s (Telegram limit)"
                await asyncio.sleep(wait)
                job.state = "running"
                continue
            raise


async def _run(job: Job):
    client = await session.ensure()
    if not client:
        job.state, job.error = "error", "Login first."
        return
    try:
        src = await client.get_chat(job.src)
        if getattr(src, "has_protected_content", False):
            job.state = "error"
            job.error = ("This source has 'Restrict saving content' turned on. Telegram does not allow "
                         "forwarding/copying from it, so it cannot be transferred.")
            return
        async for m in client.get_chat_history(job.src, limit=1):
            job.top_id = m.id
        if not job.top_id:
            job.state, job.error = "error", "Source chat is empty (or your account cannot read it)."
            return

        job.state = "running"
        job.scanned_to = job.start_id - 1
        buf: list = []
        last_save = time.time()
        pos = job.start_id
        while pos <= job.top_id and not job.stop:
            ids = list(range(pos, min(pos + FETCH, job.top_id + 1)))
            while True:
                try:
                    got = await client.get_messages(job.src, ids)
                    break
                except Exception as e:
                    if "FloodWait" in type(e).__name__:
                        await asyncio.sleep(int(getattr(e, "value", 10)) + 1)
                        continue
                    raise
            if not isinstance(got, list):
                got = [got]
            ok = [m for m in got if _sendable(m)]
            job.skipped += len(ids) - len(ok)
            buf.extend(ok)
            pos = ids[-1] + 1

            last = pos >= job.top_id + 1
            for chunk in _chunks(buf, final=last):
                if job.stop:
                    break
                await _send(client, job, chunk)
                job.scanned_to = chunk[-1].id
                await asyncio.sleep(DELAY)
                if time.time() - last_save > 10:
                    await _save_progress(job)
                    last_save = time.time()
            if not buf:
                job.scanned_to = max(job.scanned_to, pos - 1)

        if job.stop:
            job.state = "stopped"
        else:
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
        await _save_progress(job)


def start_job(job: Job) -> Job:
    global CURRENT
    CURRENT = job
    job.task = asyncio.create_task(_run(job))
    return job


def busy() -> bool:
    return CURRENT is not None and CURRENT.running
