"""
utils/backup.py - full backup / restore of everything the bot keeps in MongoDB.

A backup is ONE gzip file (or several small parts when the data is big, because the Bot API can only
download files up to 20 MB). It contains:
    settings, users, links, files (caption codes), autoposter channels + posts
    + the Start Image itself (Telegram file ids are bot-specific, so the picture is saved, not the id)
It never contains BOT_TOKEN or MONGO_URI - keep your .env safe separately.

File format (gzip, one JSON object per line):
    {"t":"fsb-backup","v":1,"id":..,"created":..,"bot":..}      first line
    {"t":"asset","name":"start_image","b64":..}                 optional
    {"t":"doc","c":"<collection>","d":{...}}                    one per MongoDB document
    {"t":"end","id":..,"part":1,"of":1}                         last line (proves the file is complete)
"""
import asyncio
import base64
import datetime as dt
import gzip
import html
import io
import logging
import secrets
import time
import zlib

from bson import json_util
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError
from telegram.error import TelegramError

import config
from database import db
from database.db import settings as S
from utils.helpers import call

log = logging.getLogger(__name__)

FORMAT = 1
COLLECTIONS = ("settings", "users", "links", "files", "ap_channels", "ap_posts", "daily_stats")   # whitelist for restore
PART_LIMIT = 15 * 1024 * 1024        # compressed bytes per part (Bot API download limit is 20 MB)
AUTO_EVERY = 24 * 3600
JSON_OPTS = json_util.CANONICAL_JSON_OPTIONS
FILE_PREFIX, FILE_SUFFIX = "fsb-backup-", ".json.gz"


def _line(obj: dict) -> bytes:
    return json_util.dumps(obj, json_options=JSON_OPTS).encode()


class _Part:
    """One gzip part, written incrementally so its real size is known while it grows."""

    def __init__(self, head: dict):
        self._z = zlib.compressobj(6, zlib.DEFLATED, 31)      # wbits 31 = gzip container
        self._chunks, self.size = [], 0
        self.add(_line(head))

    def add(self, line: bytes):
        out = self._z.compress(line + b"\n")
        if out:
            self._chunks.append(out)
            self.size += len(out)

    def finish(self, tail: dict) -> bytes:
        self.add(_line(tail))
        self._chunks.append(self._z.flush())
        return b"".join(self._chunks)


async def _start_image(bot):
    """The Start Image as base64 (None if not set / not downloadable)."""
    fid = S.get("start_image")
    if not fid:
        return None
    try:
        f = await bot.get_file(fid)
        return base64.b64encode(bytes(await f.download_as_bytearray())).decode()
    except Exception as e:
        log.warning("Backup: start image could not be downloaded: %s", e)
        return None


async def build_backup(bot):
    """-> ([(filename, gz_bytes), ...], {collection: count}, header)"""
    me = await bot.get_me()
    bid = secrets.token_hex(3)
    now = dt.datetime.now(db.LIMIT_TZ)
    head = {"t": "fsb-backup", "v": FORMAT, "id": bid, "created": time.time(),
            "created_str": now.strftime("%d %b %Y %H:%M"), "bot": me.username,
            "db_name": config.DB_NAME, "base_url": config.BASE_URL}
    parts, counts = [], {}
    cur = _Part(head)
    img = await _start_image(bot)
    if img:
        cur.add(_line({"t": "asset", "name": "start_image", "b64": img}))
    for name in COLLECTIONS:
        n = 0
        async for d in db.db[name].find({}):
            cur.add(_line({"t": "doc", "c": name, "d": d}))
            n += 1
            if n % 500 == 0:
                await asyncio.sleep(0)                    # keep the bot responsive on big collections
            if cur.size >= PART_LIMIT:
                parts.append(cur)
                cur = _Part(head)
        counts[name] = n
    parts.append(cur)
    total = len(parts)
    out = []
    for i, p in enumerate(parts, 1):
        data = p.finish({"t": "end", "id": bid, "part": i, "of": total})
        suffix = "" if total == 1 else f"-p{i}of{total}"
        out.append((f"{FILE_PREFIX}{now:%Y%m%d-%H%M}-{bid}{suffix}{FILE_SUFFIX}", data))
    head["has_image"] = bool(img)
    return out, counts, head


async def send_backup(bot, chat_id: int, auto: bool = False):
    """Build the backup and send it as document(s). Returns the per-collection counts."""
    files, counts, head = await build_backup(bot)
    total = len(files)
    for i, (fname, data) in enumerate(files, 1):
        cap = (f"💾 <b>{'Auto-backup' if auto else 'Backup'}</b> · {head['created_str']}\n"
               f"👥 {counts['users']} · 🔗 {counts['links']} · 📁 {counts['files']}"
               + (f"\nPart {i}/{total}" if total > 1 else ""))
        await call(bot.send_document, chat_id, document=io.BytesIO(data), filename=fname, caption=cap,
                   read_timeout=180, write_timeout=180, connect_timeout=30)
    await S.set("bk_last", time.time())
    return counts


async def backup_loop(bot):
    """Daily auto-backup to the owner's chat (can be switched off in the Backup screen)."""
    await asyncio.sleep(90)
    while True:
        try:
            if S.get("auto_backup") and time.time() - float(S.get("bk_last") or 0) >= AUTO_EVERY:
                st = await db.stats()
                if st["links"] or st["users"]:            # nothing to protect yet
                    await send_backup(bot, config.OWNER_ID, auto=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Auto-backup failed")
        await asyncio.sleep(1800)


# ------------------------------------------------------------------ reading a backup file
def is_backup_name(name) -> bool:
    return bool(name) and name.startswith(FILE_PREFIX) and name.endswith(FILE_SUFFIX)


def parse_part(data: bytes) -> dict:
    """Validate and decode one backup file. Raises ValueError if it is not a complete backup."""
    try:
        raw = gzip.decompress(data)
    except Exception:
        raise ValueError("not a valid backup file")
    docs = {n: [] for n in COLLECTIONS}
    assets, head, tail = {}, None, None
    for line in raw.split(b"\n"):
        if not line:
            continue
        try:
            o = json_util.loads(line, json_options=JSON_OPTS)
        except Exception:
            raise ValueError("backup file is damaged")
        t = o.get("t")
        if t == "fsb-backup":
            head = o
        elif t == "doc" and o.get("c") in docs:
            docs[o["c"]].append(o["d"])
        elif t == "asset":
            assets[o["name"]] = o["b64"]
        elif t == "end":
            tail = o
    if not head or head.get("v") != FORMAT:
        raise ValueError("not a Filestorebot backup")
    if not tail:
        raise ValueError("backup file is incomplete")
    return {"head": head, "tail": tail, "docs": docs, "assets": assets}


# ------------------------------------------------------------------ restoring
async def _restore_settings(items: list, mode: str):
    """The settings collection holds several documents (_id "main" = panel settings, "ad_stats" = ad counters)."""
    for d in items:
        doc = dict(d)
        sid = doc.pop("_id", None)
        if not sid or not doc:
            continue
        if mode == "full":
            await db.settings_col.replace_one({"_id": sid}, {"_id": sid, **doc}, upsert=True)
            continue
        existing = await db.settings_col.find_one({"_id": sid}) or {}
        missing = {k: v for k, v in doc.items() if k not in existing}
        if missing:
            await db.settings_col.update_one({"_id": sid}, {"$set": missing}, upsert=True)


async def _restore_collection(name: str, items: list, mode: str):
    col = db.db[name]
    if mode == "full":
        await col.delete_many({})
        for i in range(0, len(items), 1000):
            try:
                await col.insert_many(items[i:i + 1000], ordered=False)
            except BulkWriteError as e:
                log.warning("Restore %s: %s", name, str(e)[:200])
    else:   # merge: only documents that do not exist yet are added
        for i in range(0, len(items), 1000):
            ops = [UpdateOne({"_id": d["_id"]},
                             {"$setOnInsert": {k: v for k, v in d.items() if k != "_id"} or {"restored": True}},
                             upsert=True)
                   for d in items[i:i + 1000] if "_id" in d]
            if ops:
                await col.bulk_write(ops, ordered=False)


async def _file_id_ok(bot, fid) -> bool:
    try:
        await bot.get_file(fid)
        return True
    except Exception:
        return False


async def _restore_start_image(bot, chat_id: int, b64, mode: str) -> str:
    """File ids belong to ONE bot: upload the saved picture again so the NEW bot gets its own file id."""
    cur = S.get("start_image")
    if mode == "merge" and cur and await _file_id_ok(bot, cur):
        return "kept"
    if not b64:
        if mode == "full" or cur:
            await S.set("start_image", None)
        return "not in backup"
    try:
        m = await call(bot.send_photo, chat_id, photo=io.BytesIO(base64.b64decode(b64)),
                       caption="🖼 Start image restored.")
        await S.set("start_image", m.photo[-1].file_id)
        return "restored"
    except TelegramError as e:
        log.warning("Start image restore failed: %s", e)
        await S.set("start_image", None)
        return "failed - set it again"


async def check_access(bot) -> list:
    """Chats the (new) bot cannot use yet - it has to be added there as admin."""
    me = await bot.get_me()
    targets = []
    ch = S.get("db_channel")
    if ch:
        targets.append(("Database Channel", ch["id"], ch.get("title"), True))
    for c in S.get("fsub_channels"):
        targets.append(("Force-sub", c["id"], c.get("title"), False))
    for c in await db.ap_list_channels():
        targets.append(("Autoposter", c["_id"], c.get("title"), True))
    problems = []
    for label, cid, title, need_post in targets:
        name = html.escape(str(title or cid))
        try:
            m = await bot.get_chat_member(cid, me.id)
        except TelegramError:
            problems.append(f"{label} <b>{name}</b>: not reachable")
            continue
        if m.status not in ("administrator", "creator"):
            problems.append(f"{label} <b>{name}</b>: bot is not an admin")
        elif need_post and getattr(m, "can_post_messages", None) is False:
            problems.append(f"{label} <b>{name}</b>: needs “Post Messages”")
    return problems


def summarize(parts: list) -> dict:
    """Merge the decoded parts of one backup."""
    docs = {n: [] for n in COLLECTIONS}
    assets = {}
    for p in parts:
        for n in COLLECTIONS:
            docs[n].extend(p["docs"].get(n, []))
        assets.update(p["assets"])
    return {"docs": docs, "assets": assets, "head": parts[0]["head"]}


async def restore(bot, chat_id: int, parts: list, mode: str) -> str:
    """mode = 'full' (database becomes exactly the backup) or 'merge' (only add what is missing). Returns the report."""
    data = summarize(parts)
    docs = data["docs"]
    for name in COLLECTIONS:
        if name == "settings":
            await _restore_settings(docs[name], mode)
        else:
            await _restore_collection(name, docs[name], mode)
    await S.load()
    img = await _restore_start_image(bot, chat_id, data["assets"].get("start_image"), mode)
    problems = await check_access(bot)

    ap_ch, ap_posts = len(docs["ap_channels"]), len(docs["ap_posts"])
    text = ("✅ <b>Restore complete</b>\n\n"
            f"👥 Users: {len(docs['users'])}\n"
            f"🔗 Links: {len(docs['links'])}\n"
            f"📁 Files: {len(docs['files'])}\n"
            f"📅 Autoposter: {ap_ch} channel(s), {ap_posts} post(s)\n"
            f"🖼 Start image: {img}")
    if problems:
        text += "\n\n⚠️ <b>Action needed</b>\n" + "\n".join(f"• {p}" for p in problems)
        text += "\n\nAdd the bot as admin there, then everything works as before."
        if any(p.startswith("Database Channel") for p in problems):
            text += ("\n\nIf the Database Channel was deleted: create a new one, add the bot as admin, "
                     "set it under <b>Database Channel</b>, then forward the old files into it. "
                     "Links are rebuilt from their caption codes.")
    return text

