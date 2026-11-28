"""
database/db.py
Async MongoDB layer (Motor) + an in-memory settings cache.

Collections
  settings        one document (_id="main") holding every panel setting
  users           _id=user id, banned / blocked flags
  links           _id=random token, ids=[DB-channel message ids], label, views
  files           one record per uploaded file (for the Stats screen)
  pending_deletes scheduled auto-deletes (survive bot restarts)
  ap_channels     Autoposter target channels/groups (timings, ON/OFF, loop mode, shuffled queue_state)
  ap_posts        Autoposter posts (thumbnail + share-link token + posted flag)
  ap_groups       Autoposter share groups: channels that post from ONE common pool (no repeats, 7-day gap)
"""
import copy
import datetime as dt
import random
import re
import secrets
import time
from zoneinfo import ZoneInfo

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import config

_client = AsyncIOMotorClient(
    config.MONGO_URI,
    serverSelectionTimeoutMS=15000,
    connectTimeoutMS=10000,
    socketTimeoutMS=30000,
    maxPoolSize=50,          # many users at once -> enough parallel connections
    minPoolSize=3,           # keep a few warm so the first user after idle is not slow
    maxIdleTimeMS=120000,
    retryWrites=True,
    retryReads=True,
)
db = _client[config.DB_NAME]
settings_col = db["settings"]
users_col = db["users"]
links_col = db["links"]
files_col = db["files"]
pending_col = db["pending_deletes"]
earn_col = db["earn_stats"]        # _id="YYYY-MM-DD" (UTC, same as Monetag): rev, imp, clk, valued
pb_col = db["postback_seen"]     # recent Monetag postbacks (dedupe, auto-expire)
ads_col = db["ad_sessions"]      # one-time Mini App ad sessions (auto-expire)
usage_col = db["daily_usage"]   # _id="<user_id>:<YYYY-MM-DD>", used=<links opened today>
ap_channels_col = db["ap_channels"]   # Autoposter targets: _id=chat id, slots, enabled, done
ap_posts_col = db["ap_posts"]         # Autoposter posts: _id=share-link token, chan=chat id, seq, posted
ap_groups_col = db["ap_groups"]       # Autoposter SHARE GROUPS: _id=group id, members=[chat ids], queue_state (one shared shuffled queue)
ap_batches_col = db["ap_batches"]     # Vault Pack jobs (post type 3): resumable imgbb / Telegraph / file-store work - utils/batch.py
vps_col = db["vps_usage"]             # _id="YYYY-MM-DD-HH" (IST): hourly VPS samples (cpu, ram, network) - utils/usage.py
stats_col = db["daily_stats"]         # _id="YYYY-MM-DD" (IST): views, gates, imp, clk, valued, rev, verified, fallback, blocked

# A user can open at most this many links per day (admins are exempt). One link = 1 count, all its files are sent.
# Day changes at midnight in this timezone.
DAILY_LIMIT = 10
LIMIT_TZ = ZoneInfo("Asia/Kolkata")

# Backup code that is written under every file in the Database Channel, e.g.  FSB:Ab3dE9xYz1Q2:0
# It is hidden from users on delivery and is used to rebuild links if the bot / channel is lost.
CODE_RE = re.compile(r"FSB:([A-Za-z0-9_-]{6,}):(\d+)")
CODE_TAIL_RE = re.compile(r"\s*<code>FSB:[A-Za-z0-9_-]{6,}:\d+</code>\s*$")

DEFAULT_CAPTION = (
    "👋 <b>HELLO {mention}</b>\n\n"
    "JOIN ALL CHANNELS BELOW, THEN TAP <b>VERIFY</b>."
)

DEFAULTS = {
    "admins": [],                 # extra admin user ids (OWNER_ID is always admin)
    "db_channel": None,           # {"id": int, "title": str}
    "fsub_enabled": True,
    "fsub_channels": [],          # [{"id": int, "title": str, "link": str}], order = display order
    "home_channel": None,         # {"id": int, "title": str, "link": str} - channel shown on the website main page
    "start_image": None,          # Telegram file_id
    "start_caption": DEFAULT_CAPTION,
    "custom_buttons": [],         # rows of [[text, url], ...]
    "autodelete": 0,              # minutes, 0 = off
    "protect": False,             # protect_content on forwarded files
    "custom_caption": "",         # HTML, replaces original caption when set
    "remove_caption": False,      # strip original caption
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
    # ---- Adsgram (2nd network) + rotation
    "ads_ag_enabled": False,      # Adsgram on/off
    "ads_ag_block": "",           # Adsgram blockId (Reward block, digits)
    "ads_ag_postback": False,     # verify Adsgram ads server-side with the Reward URL (needs Reward URL enabled by Adsgram)
    "ads_ag_share": 50,           # % of ads served by Adsgram, rest Monetag (rotation per user; 0 = Monetag only, 100 = Adsgram only)
    "ads_ag_last": 0,             # time of the last Adsgram reward callback
    "ads_nofill_free": False,     # give files without an ad when EVERY network has no ad (False = never, strongest)
    # ---- Support Bot (handlers/support.py): a second bot, one forum topic per user
    "support_token": "",          # token of the Support Bot (sent by the owner in the panel)
    "support_username": "",
    "support_enabled": False,
    "support_group": None,        # {"id": int, "title": str} - forum group where every user gets a topic
    "support_in_posts": True,     # add the "Video not playing? Support" button to autoposter channel posts
    "support_btn": "",            # custom button text ("" = default)
    # ---- Backup (utils/backup.py)
    "auto_backup": True,          # send a backup file to the owner every 24 h
    "bk_last": 0,                 # time of the last backup
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

    async def reset(self, keep=("admins", "db_channel", "ads_secret", "bk_last", "imgbb_key", "imgbb_keys", "telegraph_token",
                                                 "support_token", "support_username", "support_enabled", "support_group",
                                                 "support_in_posts", "support_btn", "home_channel")):
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
    await pb_col.create_index("expire_at", expireAfterSeconds=0)
    await vps_col.create_index("expire_at", expireAfterSeconds=0)   # VPS usage history is kept ~13 months
    await ads_col.create_index([("uid", 1), ("status", 1)])
    await ap_posts_col.create_index([("chan", 1), ("seq", 1)])
    await ap_posts_col.create_index([("chan", 1), ("posted", 1), ("seq", 1)])
    await ap_posts_col.create_index("batch_id")
    await ap_batches_col.create_index([("status", 1), ("next_try", 1)])
    await _tag_post_links()
    await settings.load()


async def _tag_post_links():
    """One-time (idempotent) upgrade: give the ad-gated link of every EXISTING Autoposter post its "chan",
    so the channel's Ads / Limit setting reaches old posts too. Errors are swallowed: never block start-up."""
    try:
        by_chan = {}
        async for p in ap_posts_col.find({}, {"chan": 1, "ptype": 1, "vtoken": 1}):
            gated = p.get("vtoken") if p.get("ptype") in ("pair", "batch") else p["_id"]
            if gated:
                by_chan.setdefault(p["chan"], set()).add(gated)
        for cid, tokens in by_chan.items():
            await links_col.update_many({"_id": {"$in": list(tokens)}, "chan": {"$exists": False}},
                                        {"$set": {"chan": cid}})
    except Exception:
        pass


# ---------------------------------------------------------------- users
async def add_user(user_id: int):
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"blocked": False}, "$setOnInsert": {"joined": time.time(), "banned": False}},
        upsert=True,
    )


async def touch_user(user_id: int) -> bool:
    """Register / refresh the user in ONE round trip. Returns True if the user is banned."""
    doc = await users_col.find_one_and_update(
        {"_id": user_id},
        {"$set": {"blocked": False}, "$setOnInsert": {"joined": time.time(), "banned": False}},
        upsert=True, projection={"banned": 1}, return_document=ReturnDocument.BEFORE,
    )
    return bool(doc and doc.get("banned"))


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
    """Point link <token> slot <idx> at its new message id (file was re-forwarded into a new channel / bot).
    Safe when many files of ONE link arrive at the same time: every file only touches its own array slot."""
    code = make_code(token, idx)
    await files_col.update_one(
        {"code": code},
        {"$set": {"msg_id": new_msg_id, "kind": kind, "name": name, "cap": cap, "by": by,
                  "date": time.time()}},
        upsert=True,
    )
    try:   # create the link if it does not exist (idempotent)
        await links_col.update_one(
            {"_id": token},
            {"$setOnInsert": {"ids": [], "creator": by, "label": "Restored link",
                              "created": time.time(), "views": 0}},
            upsert=True)
    except DuplicateKeyError:
        pass   # another file of the same link created it a moment ago
    # one atomic write per slot; missing slots before it become null (skipped on delivery until restored)
    await links_col.update_one({"_id": token}, {"$set": {f"ids.{idx}": new_msg_id}})


async def create_link(ids: list, creator: int, label: str, token: str = None, no_ads: bool = False,
                      chan: int = None) -> str:
    """no_ads=True: a free preview link - opened without the ad gate and without using the daily limit.
    chan = Autoposter channel id: the link then follows that channel's live Ads / Limit setting."""
    token = token or new_token()
    doc = {"_id": token, "ids": ids, "creator": creator, "label": label,
           "created": time.time(), "views": 0}
    if no_ads:
        doc["no_ads"] = True
    if chan is not None and not no_ads:
        doc["chan"] = chan
    await links_col.insert_one(doc)
    return token


async def get_link(token: str):
    return await links_col.find_one({"_id": token})


LINK_CACHE: dict = {}   # token -> (time, link doc); utils/web.py reads it, purge_link() clears it


async def delete_link(token: str):
    """Revoke ONE link (plain). Use purge_link() to remove it from the whole database."""
    await links_col.delete_one({"_id": token})
    LINK_CACHE.pop(token, None)


async def purge_link(token: str, posts: bool = True, _seen=None) -> list:
    """Delete a link COMPLETELY from MongoDB and return the Database-Channel message ids that belonged only to it
    (the caller deletes those messages from the channel).

    Removes: the link itself, its file records (FSB:<token>:n backup codes - so a restore can never bring it back),
    every user's saved progress for it, the Autoposter posts that point to it (+ their thumbnail copies and their
    place in the shuffled queue), and finished Vault Pack jobs that created it.
    Range links (no backup codes) only point to messages that other links may share, so those channel
    messages are never touched.
    posts=False: leave the Autoposter posts alone (the caller already removed them)."""
    seen = _seen if _seen is not None else set()
    if token in seen:
        return []
    seen.add(token)
    msg_ids: list = []

    # the link + its file records
    await links_col.delete_one({"_id": token})
    LINK_CACHE.pop(token, None)
    own = re.compile(rf"^FSB:{re.escape(token)}:\d+$")
    async for d in files_col.find({"code": own}, {"msg_id": 1}):
        msg_ids.append(d["msg_id"])
    await files_col.delete_many({"code": own})

    # saved per-user progress of this link
    await users_col.update_many({f"prog.{token}": {"$exists": True}}, {"$unset": {f"prog.{token}": ""}})

    # Vault Pack jobs that made this link
    await ap_batches_col.delete_many({"vtoken": token})

    if posts:
        found = await ap_posts_col.find({"$or": [{"_id": token}, {"vtoken": token}]}).to_list(None)
        if found:
            msg_ids += await _drop_posts(found, seen)
    return msg_ids


async def _drop_posts(found: list, seen: set) -> list:
    """Delete Autoposter posts + thumbnail copies + queue entries; purge their other links when nobody else uses them."""
    msg_ids: list = []
    ids = [p["_id"] for p in found]
    await ap_posts_col.delete_many({"_id": {"$in": ids}})
    for cid in {p["chan"] for p in found}:
        await ap_channels_col.update_one({"_id": cid}, {"$pull": {"queue_state.queue": {"$in": ids}}})
    await ap_groups_col.update_many({}, {"$pull": {"queue_state.queue": {"$in": ids}}})
    for p in found:
        if p.get("thumb_token"):
            code = make_code(p["thumb_token"], 0)
            async for d in files_col.find({"code": code}, {"msg_id": 1}):
                msg_ids.append(d["msg_id"])
            await files_col.delete_many({"code": code})
        for t in post_tokens(p):                       # the post's links (Duo Reel: image + video; Vault Pack: video)
            if t in seen:
                continue
            if await ap_posts_col.count_documents({"vtoken": t}, limit=1):   # another post still uses it
                continue
            msg_ids += await purge_link(t, posts=False, _seen=seen)
    return msg_ids


async def purge_post(token: str, with_link: bool = False) -> list:
    """Delete ONE Autoposter post. with_link=True also removes its links from the database entirely."""
    p = await ap_posts_col.find_one({"_id": token})
    if not p:
        return await purge_link(token, posts=False) if with_link else []
    seen: set = set() if with_link else set(post_tokens(p))     # seen = links we must NOT touch
    return await _drop_posts([p], seen)


async def inc_views(token: str):
    await links_col.update_one({"_id": token}, {"$inc": {"views": 1}})
    await stat_inc(views=1)


async def stat_inc(**fields):
    """Add to today's counters (IST day). Analytics must never break the bot, so errors are swallowed."""
    try:
        await stats_col.update_one({"_id": _today()}, {"$inc": fields}, upsert=True)
    except Exception:
        pass


async def earn_inc(**fields):
    """Add to today's EARNING counters. Day = UTC, because Monetag's dashboard changes day at 00:00 UTC."""
    try:
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        await earn_col.update_one({"_id": day}, {"$inc": fields}, upsert=True)
    except Exception:
        pass


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


OPT_MAX = 10


def _opt(value, default: int = 1) -> int:
    """Clamp an Ads / Limit setting to 0..OPT_MAX (anything odd -> default)."""
    try:
        return min(OPT_MAX, max(0, int(value)))
    except (TypeError, ValueError):
        return default


def chan_opts(ch) -> tuple:
    """(ads_n, cost) of an Autoposter channel document. Defaults: 1 ad, 1 count."""
    ch = ch or {}
    return _opt(ch.get("ads_n", 1)), _opt(ch.get("cost", 1))


async def link_opts(link: dict) -> tuple:
    """(ads_n, cost) for a link, read LIVE from its Autoposter channel (link["chan"]), so a channel setting
    change applies at once to every old and new post of that channel. Links without "chan"
    (normal admin uploads) or whose channel is gone: 1 ad, 1 count."""
    cid = (link or {}).get("chan")
    if cid is None:
        return 1, 1
    return chan_opts(await ap_channels_col.find_one({"_id": cid}, {"ads_n": 1, "cost": 1}))


async def link_cost(link: dict) -> int:
    """How many of the user's daily-limit counts opening this link uses (0 = free)."""
    return (await link_opts(link))[1]


async def link_ads(link: dict) -> int:
    """How many ads must be watched before the files arrive (0 = no ad)."""
    return (await link_opts(link))[0]


async def claim_quota(user_id: int, n: int = 1):
    """Atomically take n counts from today's allowance (no in-process locks, safe even with several workers).
    Returns (granted n or 0, used_after)."""
    limit = limit_value()
    if limit <= 0 or n > limit:
        return 0, 0
    key = f"{user_id}:{_today()}"
    expire = _next_midnight() + dt.timedelta(days=1)
    for _ in range(3):
        doc = await usage_col.find_one_and_update(
            {"_id": key, "used": {"$lte": limit - n}},
            {"$inc": {"used": n}, "$set": {"expire_at": expire}},
            projection={"used": 1}, return_document=ReturnDocument.AFTER)
        if doc:
            return n, int(doc["used"])
        try:
            await usage_col.insert_one({"_id": key, "used": n, "expire_at": expire})
            return n, n
        except DuplicateKeyError:
            cur = await usage_col.find_one({"_id": key}, {"used": 1})
            if cur and int(cur.get("used", 0)) > limit - n:
                return 0, int(cur["used"])
            # else: we raced with the very first insert of the day -> try the update again
    return 0, limit


async def reserve_quota(user_id: int, wanted: int = 1) -> int:
    """Kept for compatibility."""
    granted, _ = await claim_quota(user_id, wanted)
    return granted


async def refund_quota(user_id: int, n: int):
    """Give back a link count that was reserved but could not be delivered."""
    if n > 0:
        await usage_col.update_one({"_id": f"{user_id}:{_today()}", "used": {"$gte": n}}, {"$inc": {"used": -n}})


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
def _blank_queue() -> dict:
    return {"cycle": 0, "cursor": 0, "queue": [], "shuffle_status": "idle", "last_batch_size": 0,
            "last_posted_at": None, "last_reshuffled_at": None, "last_post_id": None}


async def ap_add_channel(cid: int, title: str):
    await ap_channels_col.update_one(
        {"_id": cid},
        {"$set": {"title": title},
         "$setOnInsert": {"slots": [], "enabled": True, "done": [], "created": time.time(),
                          "loop": True, "queue_state": _blank_queue()}},
        upsert=True,
    )


async def ap_get_channel(cid: int):
    return await ap_channels_col.find_one({"_id": cid})


async def ap_list_channels():
    return await ap_channels_col.find({}).sort("created", 1).to_list(200)


# Settings that are the SAME for every channel of a share group (only the timings / ON-OFF stay per channel).
GROUP_SHARED = ("cap_tpl", "btn", "btn_img", "btn_vid", "fthumb", "ads_n", "cost", "loop")
GROUP_GAP_DAYS = 7          # a post that went to one channel of a group may not go to ANY channel of it again within this


async def ap_set(cid: int, **fields):
    await ap_channels_col.update_one({"_id": cid}, {"$set": fields})
    shared = {k: v for k, v in fields.items() if k in GROUP_SHARED}
    if shared:                                           # caption / button / thumbnail / ads / limit / loop: whole group
        gid = ((await ap_channels_col.find_one({"_id": cid}, {"group": 1})) or {}).get("group")
        if gid:
            await ap_channels_col.update_many({"group": gid}, {"$set": shared})


async def ap_remove_channel(cid: int):
    await ap_group_leave(cid)
    await ap_channels_col.delete_one({"_id": cid})
    await ap_posts_col.delete_many({"chan": cid})
    await ap_batches_col.delete_many({"chan": cid})


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


async def ap_add_post(cid: int, token: str, thumb_token, seq: int, title: str, cap: str,
                      kind: str, n_files: int, by: int, **extra):
    """token = the share-link token of the files; thumb_token = backup code token of the thumbnail copy in the DB channel.
    Duo Reel posts (ptype="pair"): token = the free IMAGE link, extra vtoken = the ad-gated VIDEO link,
    thumb_token = None (the post shows the channel's fixed thumbnail, or the image itself)."""
    await ap_posts_col.insert_one({
        "_id": token, "chan": cid, "seq": seq, "thumb_token": thumb_token, "title": title,
        "cap": cap, "kind": kind, "n_files": n_files, "by": by,
        "posted": False, "posted_at": None, "created": time.time(), **extra,
    })


def post_tokens(post: dict) -> list:
    """Every share-link token that belongs to an Autoposter post (1 for classic, 2 for Duo Reel)."""
    return [post["_id"]] + ([post["vtoken"]] if post.get("vtoken") else [])


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


async def ap_wipe(cid: int, only_posted: bool = False, links: bool = False):
    """Delete every post of a channel (optionally only the posted ones, optionally their share links too).
    Returns (number of posts, Database-Channel message ids to delete)."""
    q = {"chan": cid, **({"posted": True} if only_posted else {})}
    docs = await ap_posts_col.find(q).to_list(None)
    if not docs:
        return 0, []
    msg_ids: list = []
    if links:
        seen: set = set()
        msg_ids = await _drop_posts(docs, seen)
    else:
        await ap_posts_col.delete_many({"_id": {"$in": [d["_id"] for d in docs]}})
        for d in docs:                                  # thumbnail copies belong to the post only
            if d.get("thumb_token"):
                code = make_code(d["thumb_token"], 0)
                async for f in files_col.find({"code": code}, {"msg_id": 1}):
                    msg_ids.append(f["msg_id"])
                await files_col.delete_many({"code": code})
    await ap_channels_col.update_one({"_id": cid}, {"$set": {"queue_state": _blank_queue()}})
    return len(docs), msg_ids


async def ap_reset(cid: int):
    await ap_posts_col.update_many({"chan": cid}, {"$set": {"posted": False, "posted_at": None}})


# ---- shuffled queue (cycle = every post once in random order; cursor = how far the cycle has got)
async def ap_rebuild_cycle(cid: int, only_unused: bool = False):
    """Full rebuild = every post, all marked pending, freshly shuffled (new cycle).
    only_unused = only the posts not yet posted (used when one-pass mode / first start of an old channel)."""
    ch = await ap_channels_col.find_one({"_id": cid})
    if not ch:
        return None
    qs = ch.get("queue_state") or {}
    flt = {"chan": cid, **({"posted": False} if only_unused else {})}
    ids = [d["_id"] for d in await ap_posts_col.find(flt, {"_id": 1}).to_list(None)]
    random.shuffle(ids)
    last = qs.get("last_post_id")
    if len(ids) > 1 and ids[0] == last:      # never open a cycle with the post that closed the last one
        j = random.randrange(1, len(ids))
        ids[0], ids[j] = ids[j], ids[0]
    if not only_unused:
        await ap_posts_col.update_many({"chan": cid}, {"$set": {"posted": False, "posted_at": None}})
    prev = int(qs.get("cycle", 0))
    cycle = prev + 1 if ids and not (only_unused and prev > 0) else prev
    new_qs = {"cycle": cycle, "cursor": 0, "queue": ids, "shuffle_status": "ready" if ids else "empty",
              "last_batch_size": 0, "last_posted_at": qs.get("last_posted_at"),
              "last_reshuffled_at": time.time(), "last_post_id": last}
    return await ap_channels_col.find_one_and_update(
        {"_id": cid}, {"$set": {"queue_state": new_qs}}, return_document=ReturnDocument.AFTER)


async def ap_enqueue_new(cid: int, tokens: list):
    """New posts are mixed (shuffled) into the part of the running cycle that is still to be posted."""
    if not tokens:
        return
    ch = await ap_channels_col.find_one({"_id": cid})
    if (ch or {}).get("group"):                   # channel of a share group: the group's pool queue
        await ap_group_enqueue_new(ch["group"], tokens)
        return
    qs = (ch or {}).get("queue_state") or {}
    queue, cursor = list(qs.get("queue") or []), int(qs.get("cursor", 0))
    if not queue or cursor >= len(queue):
        return                                # no running cycle: the next rebuild includes them
    rest = queue[cursor:] + [str(t) for t in tokens]
    random.shuffle(rest)
    await ap_channels_col.update_one(
        {"_id": cid}, {"$set": {"queue_state.queue": queue[:cursor] + rest, "queue_state.shuffle_status": "ready"}})


async def ap_advance(cid: int, token: str):
    """A post of the queue was published: mark it posted and move the cursor."""
    now = time.time()
    await ap_posts_col.update_one({"_id": token}, {"$set": {"posted": True, "posted_at": now}})
    await ap_channels_col.update_one(
        {"_id": cid},
        {"$inc": {"queue_state.cursor": 1},
         "$set": {"queue_state.last_posted_at": now, "queue_state.last_post_id": token}})


async def ap_skip_cursor(cid: int):
    await ap_channels_col.update_one({"_id": cid}, {"$inc": {"queue_state.cursor": 1}})


async def ap_finalize(cid: int, sent: int, exhausted: bool):
    await ap_channels_col.update_one(
        {"_id": cid},
        {"$set": {"queue_state.last_batch_size": sent,
                  "queue_state.shuffle_status": "cycle complete • awaiting next shuffle" if exhausted else "in progress"}})


async def ap_upcoming(cid: int, limit: int = 10) -> list:
    """The next not-yet-posted posts, in queue order (for the Queue screen)."""
    ch = await ap_channels_col.find_one({"_id": cid})
    qs = (ch or {}).get("queue_state") or {}
    queue, cursor = qs.get("queue") or [], int(qs.get("cursor", 0))
    window = queue[cursor:cursor + limit * 4]
    if not window:
        return []
    docs = {d["_id"]: d for d in await ap_posts_col.find({"_id": {"$in": window}, "chan": cid}).to_list(None)}
    return [docs[t] for t in window if t in docs and not docs[t].get("posted")][:limit]


# ---- SHARE GROUPS: several channels, ONE pool of posts, each channel keeps its own timings.
# Pool = every (non-Vault-Pack) post whose chan is a member. One shared shuffled queue; a slot of ANY member takes the next
# post with an atomic, version-checked claim -> two channels (or two servers) can never take the same post.
def _gblank() -> dict:
    return {"cycle": 0, "cursor": 0, "queue": [], "ver": 0, "last_reshuffled_at": None, "last_post_id": None}


def _pool_filter(members) -> dict:
    return {"chan": {"$in": list(members)}, "ptype": {"$ne": "batch"}}


async def ap_group_get(gid: str):
    return await ap_groups_col.find_one({"_id": gid})


async def ap_group_members(gid: str) -> list:
    g = await ap_groups_col.find_one({"_id": gid})
    return await ap_channels_col.find({"_id": {"$in": (g or {}).get("members", [])}}).to_list(None)


async def ap_group_total(gid: str) -> int:
    g = await ap_groups_col.find_one({"_id": gid})
    return await ap_posts_col.count_documents(_pool_filter((g or {}).get("members", [])))


async def ap_group_share(cid: int, other: int):
    """Put channel `cid` and channel `other` in one share group. Returns (gid, why): why is None on success,
    else 'missing' / 'batch' / 'two_groups' / 'same'.
    Settings: the channel that is ALREADY in a group keeps the group's settings (the other one adopts them);
    if neither is in a group, the channel the admin opened (`cid`) gives its settings to `other`."""
    a = await ap_channels_col.find_one({"_id": cid})
    b = await ap_channels_col.find_one({"_id": other})
    if not a or not b or cid == other:
        return None, "missing"
    if a.get("ptype") == "batch" or b.get("ptype") == "batch":
        return None, "batch"
    ga, gb = a.get("group"), b.get("group")
    if ga and gb:
        return (ga, "same") if ga == gb else (None, "two_groups")
    if ga:
        gid, src, new = ga, a, b
    elif gb:
        gid, src, new = gb, b, a
    else:
        gid, src, new = "g" + secrets.token_hex(3), a, b
        await ap_groups_col.insert_one({"_id": gid, "members": [src["_id"]], "gap_days": GROUP_GAP_DAYS,
                                        "created": time.time(), "queue_state": _gblank()})
        await ap_channels_col.update_one({"_id": src["_id"]}, {"$set": {"group": gid}})
    await ap_groups_col.update_one({"_id": gid}, {"$addToSet": {"members": new["_id"]}})
    adopt = {k: src[k] for k in GROUP_SHARED if k in src}
    await ap_channels_col.update_one({"_id": new["_id"]}, {"$set": {"group": gid, **adopt}})
    g = await ap_groups_col.find_one({"_id": gid})
    qs = g.get("queue_state") or {}
    if qs.get("queue") and int(qs.get("cursor", 0)) < len(qs["queue"]):       # running cycle: mix the new channel's posts in
        toks = [d["_id"] for d in await ap_posts_col.find(
            {"chan": new["_id"], "posted": False, "ptype": {"$ne": "batch"}}, {"_id": 1}).to_list(None)]
        await ap_group_enqueue_new(gid, toks)
    else:
        await ap_group_rebuild(gid, only_unused=True)
    return gid, None


async def ap_group_leave(cid: int):
    """Take a channel out of its group. Its posts stay with it; both sides get their own queue back."""
    ch = await ap_channels_col.find_one({"_id": cid})
    gid = (ch or {}).get("group")
    if not gid:
        return
    await ap_channels_col.update_one({"_id": cid}, {"$unset": {"group": ""}})
    g = await ap_groups_col.find_one_and_update({"_id": gid}, {"$pull": {"members": cid}},
                                                return_document=ReturnDocument.AFTER)
    left = list((g or {}).get("members", []))
    await ap_rebuild_cycle(cid, only_unused=True)
    if len(left) <= 1:                                   # a group of one is no group
        for m in left:
            await ap_channels_col.update_one({"_id": m}, {"$unset": {"group": ""}})
            await ap_rebuild_cycle(m, only_unused=True)
        await ap_groups_col.delete_one({"_id": gid})


async def ap_group_rebuild(gid: str, only_unused: bool = False, expect_cycle=None):
    """New shuffled cycle of the whole pool. only_unused = just the not-yet-posted posts.
    expect_cycle: only rebuild if the group is still in that cycle (two channels finding the cycle done at once)."""
    g = await ap_groups_col.find_one({"_id": gid})
    if not g:
        return None
    qs = g.get("queue_state") or _gblank()
    if expect_cycle is not None and int(qs.get("cycle", 0)) != expect_cycle:
        return g
    flt = _pool_filter(g.get("members", []))
    ids = [d["_id"] for d in await ap_posts_col.find({**flt, **({"posted": False} if only_unused else {})},
                                                      {"_id": 1}).to_list(None)]
    random.shuffle(ids)
    last = qs.get("last_post_id")
    if len(ids) > 1 and ids[0] == last:
        j = random.randrange(1, len(ids))
        ids[0], ids[j] = ids[j], ids[0]
    ver, prev = int(qs.get("ver", 0)), int(qs.get("cycle", 0))
    new_qs = {"cycle": prev + 1 if ids and not (only_unused and prev > 0) else prev, "cursor": 0, "queue": ids,
              "ver": ver + 1, "last_reshuffled_at": time.time(), "last_post_id": last}
    r = await ap_groups_col.update_one({"_id": gid, "queue_state.ver": ver}, {"$set": {"queue_state": new_qs}})
    if r.modified_count == 1 and not only_unused:
        await ap_posts_col.update_many(flt, {"$set": {"posted": False, "posted_at": None}})
    return await ap_groups_col.find_one({"_id": gid})


async def ap_group_enqueue_new(gid: str, tokens: list):
    """New posts are mixed into the part of the running cycle that is still to be posted."""
    for _ in range(5):
        g = await ap_groups_col.find_one({"_id": gid})
        qs = (g or {}).get("queue_state") or {}
        queue, cursor, ver = list(qs.get("queue") or []), int(qs.get("cursor", 0)), int(qs.get("ver", 0))
        if not g or not queue or cursor >= len(queue):
            return                                       # no running cycle: the next rebuild includes them
        new = {str(t) for t in tokens}
        rest = [t for t in queue[cursor:] if t not in new] + sorted(new)
        random.shuffle(rest)
        r = await ap_groups_col.update_one(
            {"_id": gid, "queue_state.ver": ver},
            {"$set": {"queue_state.queue": queue[:cursor] + rest, "queue_state.ver": ver + 1}})
        if r.modified_count == 1:
            return


async def ap_group_claim(gid: str, cid: int):
    """Take the next post of the pool for channel `cid`. Returns (post, why): post None -> why is
    'empty' (cycle finished), 'gap' (every post left was sent less than gap_days ago) or 'nogroup'.
    The claim is a compare-and-swap on the queue version: only ONE caller gets a given post."""
    for _ in range(8):
        g = await ap_groups_col.find_one({"_id": gid})
        if not g or cid not in g.get("members", []):
            return None, "nogroup"
        qs = g.get("queue_state") or {}
        queue, cursor, ver = list(qs.get("queue") or []), int(qs.get("cursor", 0)), int(qs.get("ver", 0))
        if cursor >= len(queue):
            return None, "empty"
        gap = float(g.get("gap_days", GROUP_GAP_DAYS)) * 86400
        now, t0 = time.time(), float(qs.get("last_reshuffled_at") or 0)
        window = queue[cursor:cursor + 2000]
        docs = {d["_id"]: d for d in await ap_posts_col.find(
            {"_id": {"$in": window}, **_pool_filter(g.get("members", []))}).to_list(None)}
        waiting, chosen, idx = [], None, 0
        for i, t in enumerate(window):
            d = docs.get(t)
            if not d:
                continue                                  # deleted / its channel left the group -> dropped
            if d.get("posted") and float(d.get("posted_at") or 0) >= t0:
                continue                                  # already posted in this cycle (e.g. Post now) -> dropped
            if now - max(float(d.get("last_sent_at") or 0), float(d.get("posted_at") or 0)) < gap:
                waiting.append(t)                         # went out less than gap_days ago -> try again later
                continue
            chosen, idx = d, i
            break
        if chosen is None:
            if waiting:
                return None, "gap"
            await ap_groups_col.update_one({"_id": gid, "queue_state.ver": ver},
                                           {"$set": {"queue_state.cursor": len(queue), "queue_state.ver": ver + 1}})
            continue
        new_queue = queue[:cursor] + [chosen["_id"]] + waiting + queue[cursor + idx + 1:]
        r = await ap_groups_col.update_one(
            {"_id": gid, "queue_state.ver": ver},
            {"$set": {"queue_state.queue": new_queue, "queue_state.cursor": cursor + 1, "queue_state.ver": ver + 1}})
        if r.modified_count != 1:
            continue                                      # somebody else claimed first -> look again
        chosen["_prev_sent"] = chosen.get("last_sent_at")
        await ap_posts_col.update_one({"_id": chosen["_id"]}, {"$set": {"last_sent_at": now, "last_sent_chan": cid}})
        return chosen, None
    return None, "busy"


async def ap_group_done(gid: str, cid: int, token: str):
    """The claimed post was published."""
    now = time.time()
    await ap_posts_col.update_one({"_id": token}, {"$set": {"posted": True, "posted_at": now}})
    await ap_channels_col.update_one({"_id": cid}, {"$set": {"queue_state.last_posted_at": now,
                                                              "queue_state.last_post_id": token}})
    await ap_groups_col.update_one({"_id": gid}, {"$set": {"queue_state.last_post_id": token}})


async def ap_group_giveback(gid: str, token: str, prev_sent, requeue: bool = True):
    """The claimed post could not be published: forget the claim; requeue = put it back at the end of the queue
    (the claimed entry sits BEHIND the cursor, so it is added again after it)."""
    await ap_posts_col.update_one({"_id": token}, {"$set": {"last_sent_at": prev_sent}})
    if not requeue:
        return
    for _ in range(8):
        g = await ap_groups_col.find_one({"_id": gid})
        qs = (g or {}).get("queue_state") or {}
        queue, cursor, ver = list(qs.get("queue") or []), int(qs.get("cursor", 0)), int(qs.get("ver", 0))
        if not g or token in queue[cursor:]:
            return                                       # group gone, or already waiting in the queue
        r = await ap_groups_col.update_one(
            {"_id": gid, "queue_state.ver": ver},
            {"$set": {"queue_state.queue": queue[:cursor] + queue[cursor:] + [token], "queue_state.ver": ver + 1}})
        if r.modified_count == 1:
            return


async def ap_group_upcoming(gid: str, limit: int = 10) -> list:
    g = await ap_groups_col.find_one({"_id": gid})
    qs = (g or {}).get("queue_state") or {}
    queue, cursor = qs.get("queue") or [], int(qs.get("cursor", 0))
    window = queue[cursor:cursor + limit * 4]
    if not window:
        return []
    docs = {d["_id"]: d for d in await ap_posts_col.find(
        {"_id": {"$in": window}, **_pool_filter((g or {}).get("members", []))}).to_list(None)}
    return [docs[t] for t in window if t in docs][:limit]


async def ap_pool_counts(cid: int):
    """(total, posted) of what this channel can post: its own posts, or the whole pool when it is in a group."""
    ch = await ap_channels_col.find_one({"_id": cid}, {"group": 1})
    if not (ch or {}).get("group"):
        return await ap_counts(cid)
    g = await ap_groups_col.find_one({"_id": ch["group"]})
    flt = _pool_filter((g or {}).get("members", []))
    return (await ap_posts_col.count_documents(flt),
            await ap_posts_col.count_documents({**flt, "posted": True}))


async def ap_pool_list(cid: int, skip: int, limit: int):
    ch = await ap_channels_col.find_one({"_id": cid}, {"group": 1})
    if not (ch or {}).get("group"):
        return await ap_list_posts(cid, skip, limit)
    g = await ap_groups_col.find_one({"_id": ch["group"]})
    return await ap_posts_col.find(_pool_filter((g or {}).get("members", []))).sort("seq", 1).skip(skip).limit(limit).to_list(limit)



async def thumb_msg_id(code: str):
    """Current DB-channel message id of a thumbnail (looked up by backup code, so it survives a channel restore)."""
    doc = await files_col.find_one({"code": code}, {"msg_id": 1})
    return (doc or {}).get("msg_id")
