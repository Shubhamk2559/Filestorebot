"""
database/db.py
Async MongoDB layer (Motor) + an in-memory settings cache.

Collections
  settings        one document (_id="main") holding every panel setting
  users           _id=user id, banned / blocked flags
  links           _id=random token, ids=[DB-channel message ids], label, views
  files           one record per uploaded file (for the Stats screen)
  pending_deletes scheduled auto-deletes (survive bot restarts)
  ap_channels     Autoposter target channels/groups (timings, ON/OFF)
  ap_posts        Autoposter posts (thumbnail + share-link token + posted flag)
"""
import asyncio
import copy
import datetime as dt
import re
import secrets
import time
from zoneinfo import ZoneInfo

from motor.motor_asyncio import AsyncIOMotorClient

import config

_client = AsyncIOMotorClient(config.MONGO_URI, serverSelectionTimeoutMS=15000)
db = _client[config.DB_NAME]
settings_col = db["settings"]
users_col = db["users"]
links_col = db["links"]
files_col = db["files"]
pending_col = db["pending_deletes"]
ads_col = db["ad_sessions"]      # one-time Mini App ad sessions (auto-expire)
usage_col = db["daily_usage"]   # _id="<user_id>:<YYYY-MM-DD>", used=<links opened today>
ap_channels_col = db["ap_channels"]   # Autoposter targets: _id=chat id, slots, enabled, done
ap_posts_col = db["ap_posts"]         # Autoposter posts: _id=share-link token, chan=chat id, seq, posted

# A user can open at most this many links per day (admins are exempt). One link = 1 count, all its files are sent.
# Day changes at midnight in this timezone.
DAILY_LIMIT = 10
LIMIT_TZ = ZoneInfo("Asia/Kolkata")

# Backup code that is written under every file in the Database Channel, e.g.  FSB:Ab3dE9xYz1Q2:0
# It is hidden from users on delivery and is used to rebuild links if the bot / channel is lost.
CODE_RE = re.compile(r"FSB:([A-Za-z0-9_-]{6,}):(\d+)")
CODE_TAIL_RE = re.compile(r"\s*<code>FSB:[A-Za-z0-9_-]{6,}:\d+</code>\s*$")

DEFAULT_CAPTION = (
    "👋 Hello {mention}!\n\n"
    "Please join all the channels below to access your files,\n"
    "then tap <b>Verify</b>."
)

DEFAULTS = {
    "admins": [],                 # extra admin user ids (OWNER_ID is always admin)
    "db_channel": None,           # {"id": int, "title": str}
    "fsub_enabled": True,
    "fsub_channels": [],          # [{"id": int, "title": str, "link": str}], order = display order
    "start_image": None,          # Telegram file_id
    "start_caption": DEFAULT_CAPTION,
    "custom_buttons": [],         # rows of [[text, url], ...]
    "autodelete": 0,              # minutes, 0 = off
    "delivery_mode": "forward",   # "forward" = send the files in chat, "stream" = Stream/Download web links
    "protect": False,             # protect_content on delivered files (forward mode only)
    "custom_caption": "",         # HTML, replaces original caption when set
    "remove_caption": False,      # strip original caption
    "shortener_enabled": False,
    "shortener_url": "",
    "shortener_key": "",
    "daily_limit": 10,            # links per user per day (admins exempt)
    # ---- Monetag rewarded ads (Telegram Mini App) - all configured from the Ads screen of the panel
    "ads_enabled": False,
    "ads_zone": "",               # Monetag main SDK zone id
    "ads_sdk_url": "https://libtl.com/sdk.js",   # SDK script URL exactly as in your Monetag snippet
    "ads_postback": True,         # server-to-server verification (Monetag postback) - strongest mode
    "ads_secret": "",             # secret used in the postback URL (auto-generated)
    "ads_block_adblock": True,    # refuse users running an ad blocker / ad-blocking DNS
    "ads_tries": 3,               # no-fill tries before files are given without an ad
    "ads_last_postback": 0,
    "ads_admin_test": False,      # show the ad gate to admins too (for testing)
}


class SettingsCache:
    """Settings are read from memory (fast) and written through to MongoDB."""

    def __init__(self):
        self._data = copy.deepcopy(DEFAULTS)

    async def load(self):
        doc = await settings_col.find_one({"_id": "main"}) or {}
        doc.pop("_id", None)
        self._data = {**copy.deepcopy(DEFAULTS), **doc}

    def get(self, key):
        return self._data.get(key, copy.deepcopy(DEFAULTS.get(key)))

    async def set(self, key, value):
        self._data[key] = value
        await settings_col.update_one({"_id": "main"}, {"$set": {key: value}}, upsert=True)

    async def reset(self, keep=("admins", "db_channel", "ads_secret")):
        """Restore defaults but keep admins and the database channel."""
        kept = {k: self._data.get(k) for k in keep}
        fresh = copy.deepcopy(DEFAULTS)
        fresh.update(kept)
        self._data = fresh
        await settings_col.replace_one({"_id": "main"}, {"_id": "main", **fresh}, upsert=True)


settings = SettingsCache()


async def init_db():
    """Ping Mongo, create indexes, load the settings cache."""
    await db.command("ping")
    await links_col.create_index("created")
    await pending_col.create_index("delete_at")
    await files_col.create_index("msg_id")
    await files_col.create_index("code")
    await usage_col.create_index("expire_at", expireAfterSeconds=0)  # old counters clean themselves
    await ads_col.create_index("expire_at", expireAfterSeconds=0)
    await ads_col.create_index([("uid", 1), ("status", 1)])
    await ap_posts_col.create_index([("chan", 1), ("seq", 1)])
    await ap_posts_col.create_index([("chan", 1), ("posted", 1), ("seq", 1)])
    await settings.load()


# ---------------------------------------------------------------- users
async def add_user(user_id: int):
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"blocked": False}, "$setOnInsert": {"joined": time.time(), "banned": False}},
        upsert=True,
    )


async def is_banned(user_id: int) -> bool:
    doc = await users_col.find_one({"_id": user_id}, {"banned": 1})
    return bool(doc and doc.get("banned"))


async def set_banned(user_id: int, flag: bool):
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"banned": flag}, "$setOnInsert": {"joined": time.time(), "blocked": False}},
        upsert=True,
    )


async def set_blocked(user_id: int, flag: bool):
    await users_col.update_one({"_id": user_id}, {"$set": {"blocked": flag}})


_BROADCAST_FILTER = {"banned": {"$ne": True}, "blocked": {"$ne": True}}


async def iter_broadcast_ids():
    async for doc in users_col.find(_BROADCAST_FILTER, {"_id": 1}):
        yield doc["_id"]


async def count_broadcastable() -> int:
    return await users_col.count_documents(_BROADCAST_FILTER)


async def stats() -> dict:
    return {
        "users": await users_col.count_documents({}),
        "files": await files_col.count_documents({}),
        "links": await links_col.count_documents({}),
        "banned": await users_col.count_documents({"banned": True}),
        "blocked": await users_col.count_documents({"blocked": True}),
    }


async def mongo_status() -> dict:
    """Health, speed and storage of the MongoDB database (for the Stats screen)."""
    out = {"ok": False, "error": "", "ping_ms": None, "read_ms": None, "limit_mb": config.MONGO_LIMIT_MB}
    try:
        t = time.perf_counter()
        await db.command("ping")
        out["ping_ms"] = (time.perf_counter() - t) * 1000
        t = time.perf_counter()
        await settings_col.find_one({"_id": "main"})
        out["read_ms"] = (time.perf_counter() - t) * 1000
        st = await db.command("dbStats", scale=1)
        out.update(ok=True, objects=st.get("objects", 0), data=st.get("dataSize", 0),
                   storage=st.get("storageSize", 0), index=st.get("indexSize", 0),
                   collections=st.get("collections", 0))
        out["used"] = out["storage"] + out["index"]          # what Atlas counts against the quota
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:200]
        return out
    try:
        out["conn"] = (await db.client.admin.command("serverStatus")).get("connections")
    except Exception:
        out["conn"] = None                                   # free tier does not allow serverStatus
    cols = []
    for name in sorted(await db.list_collection_names()):
        try:
            c = await db.command("collStats", name)
            cols.append((name, c.get("count", 0), c.get("storageSize", 0) + c.get("totalIndexSize", 0)))
        except Exception:
            pass
    out["cols"] = sorted(cols, key=lambda x: -x[2])
    return out


# ---------------------------------------------------------------- files & links
def new_token() -> str:
    return secrets.token_urlsafe(9)  # 12 random URL-safe chars, unguessable


def make_code(token: str, idx: int) -> str:
    return f"FSB:{token}:{idx}"


def parse_code(text):
    """(token, idx) from a caption that contains a backup code, else None."""
    m = CODE_RE.search(text or "")
    return (m.group(1), int(m.group(2))) if m else None


async def add_file(msg_id: int, kind: str, name: str, added_by: int, code=None, cap=None):
    """cap = the ORIGINAL caption (HTML) without the backup code - that is what users get."""
    doc = {"msg_id": msg_id, "kind": kind, "name": name, "by": added_by, "date": time.time()}
    if code:
        doc["code"] = code
        doc["cap"] = cap or ""
    await files_col.insert_one(doc)


async def caption_overrides(ids: list) -> dict:
    """{msg_id: original caption} for files that carry a backup code (so the code never reaches users)."""
    out = {}
    async for d in files_col.find({"msg_id": {"$in": ids}, "code": {"$exists": True}},
                                  {"msg_id": 1, "cap": 1}):
        out[d["msg_id"]] = d.get("cap") or ""
    return out


async def restore_file(token: str, idx: int, new_msg_id: int, kind: str, name: str, cap: str, by: int):
    """Point link <token> slot <idx> at its new message id (file was re-forwarded into a new channel / bot)."""
    code = make_code(token, idx)
    await files_col.update_one(
        {"code": code},
        {"$set": {"msg_id": new_msg_id, "kind": kind, "name": name, "cap": cap, "by": by,
                  "date": time.time()}},
        upsert=True,
    )
    link = await links_col.find_one({"_id": token}, {"ids": 1})
    if not link:
        await links_col.insert_one({"_id": token, "ids": [], "creator": by,
                                    "label": "Restored link", "created": time.time(), "views": 0})
        ids = []
    else:
        ids = link["ids"]
    if len(ids) <= idx:
        ids = ids + [None] * (idx + 1 - len(ids))
    ids[idx] = new_msg_id
    await links_col.update_one({"_id": token}, {"$set": {"ids": ids}})


async def create_link(ids: list, creator: int, label: str, token: str = None) -> str:
    token = token or new_token()
    await links_col.insert_one(
        {"_id": token, "ids": ids, "creator": creator, "label": label,
         "created": time.time(), "views": 0}
    )
    return token


async def get_link(token: str):
    return await links_col.find_one({"_id": token})


async def delete_link(token: str):
    await links_col.delete_one({"_id": token})


async def inc_views(token: str):
    await links_col.update_one({"_id": token}, {"$inc": {"views": 1}})


async def list_links(skip: int, limit: int, creator=None):
    query = {"creator": creator} if creator is not None else {}
    cursor = links_col.find(query).sort("created", -1).skip(skip).limit(limit)
    return await cursor.to_list(limit)


async def count_links(creator=None) -> int:
    return await links_col.count_documents({"creator": creator} if creator is not None else {})


# ---------------------------------------------------------------- auto-delete queue
async def schedule_delete(chat_id: int, ids: list, minutes: int):
    when = time.time() + minutes * 60
    for i in range(0, len(ids), 100):  # Telegram deletes max 100 per call
        await pending_col.insert_one({"chat_id": chat_id, "ids": ids[i:i + 100], "delete_at": when})


async def due_deletes(limit: int = 100):
    cursor = pending_col.find({"delete_at": {"$lte": time.time()}}).limit(limit)
    return await cursor.to_list(limit)


async def remove_pending(doc_id):
    await pending_col.delete_one({"_id": doc_id})


# ---------------------------------------------------------------- daily limit (per user)
_quota_locks: dict = {}


def _today() -> str:
    return dt.datetime.now(LIMIT_TZ).strftime("%Y-%m-%d")


def _next_midnight() -> dt.datetime:
    now = dt.datetime.now(LIMIT_TZ)
    return (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def limit_value() -> int:
    try:
        return max(0, int(settings.get("daily_limit")))
    except (TypeError, ValueError):
        return DAILY_LIMIT


async def reserve_quota(user_id: int, wanted: int) -> int:
    """Atomically take up to `wanted` links from today's allowance. Returns how many were granted (0 = used up)."""
    lock = _quota_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        key = f"{user_id}:{_today()}"
        doc = await usage_col.find_one({"_id": key}, {"used": 1})
        used = (doc or {}).get("used", 0)
        granted = max(0, min(wanted, limit_value() - used))
        if granted:
            await usage_col.update_one(
                {"_id": key},
                {"$inc": {"used": granted}, "$set": {"expire_at": _next_midnight() + dt.timedelta(days=1)}},
                upsert=True,
            )
        return granted


async def refund_quota(user_id: int, n: int):
    """Give back a link count that was reserved but could not be delivered."""
    if n > 0:
        await usage_col.update_one({"_id": f"{user_id}:{_today()}"}, {"$inc": {"used": -n}})


# ---------------------------------------------------------------- per-link progress (for links bigger than the daily limit)
async def get_progress(user_id: int, token: str) -> int:
    doc = await users_col.find_one({"_id": user_id}, {"prog": 1})
    return int(((doc or {}).get("prog") or {}).get(token, 0))


async def set_progress(user_id: int, token: str, n: int):
    """n = how many files of this link the user already got; 0 clears it (link finished)."""
    op = {"$set": {f"prog.{token}": n}} if n else {"$unset": {f"prog.{token}": ""}}
    await users_col.update_one({"_id": user_id}, op, upsert=True)


async def used_today(user_id: int) -> int:
    doc = await usage_col.find_one({"_id": f"{user_id}:{_today()}"}, {"used": 1})
    return (doc or {}).get("used", 0)


# ---------------------------------------------------------------- autoposter
async def ap_add_channel(cid: int, title: str):
    await ap_channels_col.update_one(
        {"_id": cid},
        {"$set": {"title": title},
         "$setOnInsert": {"slots": [], "enabled": True, "done": [], "created": time.time()}},
        upsert=True,
    )


async def ap_get_channel(cid: int):
    return await ap_channels_col.find_one({"_id": cid})


async def ap_list_channels():
    return await ap_channels_col.find({}).sort("created", 1).to_list(200)


async def ap_set(cid: int, **fields):
    await ap_channels_col.update_one({"_id": cid}, {"$set": fields})


async def ap_remove_channel(cid: int):
    await ap_channels_col.delete_one({"_id": cid})
    await ap_posts_col.delete_many({"chan": cid})


async def ap_claim_slot(cid: int, key: str) -> bool:
    """Atomically mark '<date> <HH:MM>' as run for this channel. True only for the caller that got it."""
    r = await ap_channels_col.update_one(
        {"_id": cid, "done": {"$ne": key}},
        {"$push": {"done": {"$each": [key], "$slice": -60}}},
    )
    return r.modified_count == 1


async def ap_last_seq(cid: int) -> int:
    doc = await ap_posts_col.find_one({"chan": cid}, {"seq": 1}, sort=[("seq", -1)])
    return int((doc or {}).get("seq", 0))


async def ap_add_post(cid: int, token: str, thumb_token: str, seq: int, title: str, cap: str,
                      kind: str, n_files: int, by: int):
    """token = the share-link token of the files; thumb_token = backup code token of the thumbnail copy in the DB channel."""
    await ap_posts_col.insert_one({
        "_id": token, "chan": cid, "seq": seq, "thumb_token": thumb_token, "title": title,
        "cap": cap, "kind": kind, "n_files": n_files, "by": by,
        "posted": False, "posted_at": None, "created": time.time(),
    })


async def ap_get_post(token: str):
    return await ap_posts_col.find_one({"_id": token})


async def ap_list_posts(cid: int, skip: int, limit: int):
    return await ap_posts_col.find({"chan": cid}).sort("seq", 1).skip(skip).limit(limit).to_list(limit)


async def ap_counts(cid: int):
    total = await ap_posts_col.count_documents({"chan": cid})
    posted = await ap_posts_col.count_documents({"chan": cid, "posted": True})
    return total, posted


async def ap_pending(cid: int, limit: int):
    return await ap_posts_col.find({"chan": cid, "posted": False}).sort("seq", 1).limit(limit).to_list(limit)


async def ap_pending_count(cid: int) -> int:
    return await ap_posts_col.count_documents({"chan": cid, "posted": False})


async def ap_mark_posted(token: str):
    await ap_posts_col.update_one({"_id": token}, {"$set": {"posted": True, "posted_at": time.time()}})


async def ap_delete_post(token: str):
    await ap_posts_col.delete_one({"_id": token})


async def ap_wipe(cid: int, only_posted: bool = False, links: bool = False) -> int:
    """Delete every post of a channel (optionally only the posted ones, optionally their share links too)."""
    q = {"chan": cid, **({"posted": True} if only_posted else {})}
    tokens = [d["_id"] for d in await ap_posts_col.find(q, {"_id": 1}).to_list(None)]
    if tokens:
        await ap_posts_col.delete_many({"_id": {"$in": tokens}})
        if links:
            await links_col.delete_many({"_id": {"$in": tokens}})
    return len(tokens)


async def ap_reset(cid: int):
    await ap_posts_col.update_many({"chan": cid}, {"$set": {"posted": False, "posted_at": None}})


async def thumb_msg_id(code: str):
    """Current DB-channel message id of a thumbnail (looked up by backup code, so it survives a channel restore)."""
    doc = await files_col.find_one({"code": code}, {"msg_id": 1})
    return (doc or {}).get("msg_id")
