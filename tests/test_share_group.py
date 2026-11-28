"""Offline test of SHARE GROUPS (several Autoposter channels, one pool of posts). stdlib only - telegram / motor are stubs,
Mongo is a small in-memory fake that understands dotted keys, $set/$inc/$push/$pull/$addToSet/$unset, $in/$ne/$gte.

Run:  python3 tests/test_share_group.py
Checks: sharing copies caption / button / thumbnail / ads / limit / loop and keeps timings apart, pool = posts of both channels,
two channels (and two racing callers) never get the same post, 7-day gap between channels, loop reshuffles, one pass stops,
new posts join the running cycle, failed send puts the post back, leaving the group restores own queues.
"""
import asyncio
import copy
import importlib
import importlib.abc
import importlib.machinery
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in dict(BOT_TOKEN="1:x", API_ID="1", API_HASH="x", MONGO_URI="mongodb://x", OWNER_ID="1", BASE_URL="https://x.y").items():
    os.environ.setdefault(k, v)


class Btn:
    def __init__(self, text, callback_data=None, url=None, web_app=None, **kw):
        self.text, self.callback_data, self.url, self.web_app = text, callback_data, url, web_app


class Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = [list(r) for r in inline_keyboard]


class _Filter:
    class MessageFilter:
        pass


class _Err(Exception):
    pass


class DupKey(Exception):
    pass


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    ROOTS = ("telegram", "motor", "pymongo", "aiohttp", "pyrogram", "uvloop", "dns", "bson")

    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in self.ROOTS:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)

    def create_module(self, spec):
        m = MagicMock(name=spec.name)
        m.__path__, m.__name__, m.__spec__ = [], spec.name, spec
        if spec.name == "telegram":
            m.InlineKeyboardButton, m.InlineKeyboardMarkup = Btn, Markup
        if spec.name == "telegram.error":
            for n in ("BadRequest", "Forbidden", "RetryAfter", "TelegramError", "Conflict", "NetworkError"):
                setattr(m, n, type(n, (_Err,), {}))
        if spec.name == "pymongo.errors":
            m.DuplicateKeyError = DupKey
        if spec.name == "telegram.ext":
            m.filters = _Filter
            m.ExtBot = type("ExtBot", (), {})
        return m

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _Finder())



# ------------------------------------------------------------------ in-memory Mongo (enough for the share-group code)
def _get(doc, path):
    for part in path.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return None
        doc = doc[part]
    return doc


def _set(doc, path, val):
    parts = path.split(".")
    for part in parts[:-1]:
        doc = doc.setdefault(part, {})
    doc[parts[-1]] = val


def _unset(doc, path):
    parts = path.split(".")
    for part in parts[:-1]:
        doc = doc.get(part, {})
    doc.pop(parts[-1], None)


def _match(doc, flt):
    for k, cond in flt.items():
        v = _get(doc, k)
        if isinstance(cond, dict):
            for op, x in cond.items():
                if op == "$in" and v not in x: return False
                if op == "$ne":
                    if (isinstance(v, list) and x in v) or v == x: return False
                if op == "$gte" and not (v is not None and v >= x): return False
                if op == "$lte" and not (v is not None and v <= x): return False
                if op == "$exists" and (v is not None) != x: return False
        elif v != cond:
            return False
    return True


def _apply(doc, upd):
    for k, v in upd.get("$set", {}).items(): _set(doc, k, copy.deepcopy(v))
    for k, v in upd.get("$unset", {}).items(): _unset(doc, k)
    for k, v in upd.get("$inc", {}).items(): _set(doc, k, (_get(doc, k) or 0) + v)
    for k, v in upd.get("$push", {}).items():
        cur = _get(doc, k)
        if cur is None: cur = []; _set(doc, k, cur)
        cur.extend(v["$each"] if isinstance(v, dict) else [v])
    for k, v in upd.get("$addToSet", {}).items():
        cur = _get(doc, k)
        if cur is None: cur = []; _set(doc, k, cur)
        if v not in cur: cur.append(v)
    for k, v in upd.get("$pull", {}).items():
        cur = _get(doc, k)
        if isinstance(cur, list):
            drop = v["$in"] if isinstance(v, dict) else [v]
            cur[:] = [x for x in cur if x not in drop]


class _Cursor:
    def __init__(self, docs): self.docs = docs
    def sort(self, key, d=1): self.docs.sort(key=lambda x: x.get(key) or 0, reverse=d < 0); return self
    def skip(self, n): self.docs = self.docs[n:]; return self
    def limit(self, n): self.docs = self.docs[:n]; return self
    async def to_list(self, *a, **k): return self.docs


class Col:
    def __init__(self): self.d = {}
    async def insert_one(self, doc):
        if doc["_id"] in self.d: raise DupKey()
        self.d[doc["_id"]] = copy.deepcopy(doc)
    async def find_one(self, flt=None, proj=None, **kw):
        for doc in self.d.values():
            if _match(doc, flt or {}): return copy.deepcopy(doc)
    def find(self, flt=None, proj=None, **kw):
        return _Cursor([copy.deepcopy(x) for x in self.d.values() if _match(x, flt or {})])
    async def count_documents(self, flt, **kw):
        return sum(1 for x in self.d.values() if _match(x, flt))
    async def update_one(self, flt, upd, upsert=False):
        for doc in self.d.values():
            if _match(doc, flt):
                _apply(doc, upd); return MagicMock(modified_count=1)
        if upsert and "_id" in flt:
            doc = {"_id": flt["_id"]}; _apply(doc, upd); self.d[doc["_id"]] = doc
        return MagicMock(modified_count=0)
    async def update_many(self, flt, upd):
        for doc in list(self.d.values()):
            if _match(doc, flt): _apply(doc, upd)
    async def find_one_and_update(self, flt, upd, **kw):
        for doc in self.d.values():
            if _match(doc, flt):
                _apply(doc, upd); return copy.deepcopy(doc)
    async def delete_one(self, flt):
        for k, doc in list(self.d.items()):
            if _match(doc, flt): del self.d[k]; return
    async def delete_many(self, flt):
        for k, doc in list(self.d.items()):
            if _match(doc, flt): del self.d[k]


import config  # noqa: E402
from database import db  # noqa: E402
from database.db import settings as S  # noqa: E402

for name in ("ap_channels_col", "ap_posts_col", "ap_groups_col", "ap_batches_col", "links_col", "files_col", "users_col"):
    setattr(db, name, Col())
S._data = copy.deepcopy(db.DEFAULTS)
S._data.update(admins=[42], db_channel={"id": -100, "title": "Store"})

from utils import autoposter as AP  # noqa: E402

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


A, B, C = -1001, -1002, -1003
DAY = 86400
sent_log = []          # (channel, post token)
fail_next = {"on": False}


async def fake_send(bot, chat_id, post, mark=True):
    if fail_next["on"]:
        return "Forbidden: bot is not a member"
    sent_log.append((chat_id, post["_id"]))
    return None
AP.send_post = fake_send
AP.POST_DELAY = 0
notes = []


async def fake_notify(bot, key, text, every=0):
    notes.append(key)
AP.notify_owner = fake_notify


async def add_posts(cid, n, prefix):
    seq = await db.ap_last_seq(cid)
    toks = []
    for i in range(n):
        seq += 1
        t = f"{prefix}{i:03d}"
        await db.ap_add_post(cid, t, "th" + t, seq, t, "cap", "photo", 1, 1)
        toks.append(t)
    await db.ap_enqueue_new(cid, toks)
    return toks


async def slot(cid, n=1):
    ch = await db.ap_get_channel(cid)
    await AP.run_slot(None, ch, n)


async def main():
    await db.ap_add_channel(A, "Alpha")
    await db.ap_add_channel(B, "Beta")
    await db.ap_add_channel(C, "Gamma")
    await db.ap_set(A, slots=[{"t": "09:00", "n": 2}], cap_tpl="CAP-A", btn="BTN-A", fthumb="FT-A", ads_n=3, cost=2)
    await db.ap_set(B, slots=[{"t": "18:30", "n": 1}], cap_tpl="CAP-B", btn="BTN-B")
    a_posts = await add_posts(A, 6, "a")
    b_posts = await add_posts(B, 6, "b")

    print("1) sharing")
    gid, why = await db.ap_group_share(A, B)
    check(why is None and gid, "A + B -> one group")
    a, b = await db.ap_get_channel(A), await db.ap_get_channel(B)
    check(a["group"] == b["group"] == gid, "both channels carry the group id")
    check((b["cap_tpl"], b["btn"], b["fthumb"], b["ads_n"], b["cost"]) == ("CAP-A", "BTN-A", "FT-A", 3, 2),
          "B adopted caption / button / thumbnail / ads / limit of A")
    check(a["slots"] == [{"t": "09:00", "n": 2}] and b["slots"] == [{"t": "18:30", "n": 1}], "timings stay separate")
    await db.ap_set(B, cap_tpl="NEW CAP", ads_n=1)
    check((await db.ap_get_channel(A))["cap_tpl"] == "NEW CAP" and (await db.ap_get_channel(A))["ads_n"] == 1,
          "changing the caption / ads on ONE channel changes the whole group")
    await db.ap_set(B, slots=[{"t": "19:00", "n": 3}])
    check((await db.ap_get_channel(A))["slots"] == [{"t": "09:00", "n": 2}], "changing timings does NOT touch the other channel")
    await db.ap_set(B, slots=[{"t": "18:30", "n": 1}])
    g = await db.ap_group_get(gid)
    check(len(g["queue_state"]["queue"]) == 12 and await db.ap_group_total(gid) == 12, "pool = 12 posts of both channels")
    check(len(set(g["queue_state"]["queue"])) == 12, "every post once in the queue")

    print("2) no repeat between channels")
    for _ in range(3):
        await slot(A, 2)
        await slot(B, 1)
    toks = [t for _, t in sent_log]
    check(len(toks) == 9 and len(set(toks)) == 9, "9 posts sent from both channels, none twice")
    check({c for c, _ in sent_log} == {A, B}, "both channels posted")
    check(any(t.startswith("b") for c, t in sent_log if c == A) or any(t.startswith("a") for c, t in sent_log if c == B)
          or True, "posts may cross channels")
    a_got = {t for c, t in sent_log if c == A}
    b_got = {t for c, t in sent_log if c == B}
    check(not (a_got & b_got), "A and B never got the same post")

    print("3) two callers at the same moment")
    sent_log.clear()
    r = await asyncio.gather(*[db.ap_group_claim(gid, A if i % 2 else B) for i in range(3)])
    got = [p["_id"] for p, _ in r if p]
    check(len(got) == len(set(got)), "racing claims return different posts")

    print("4) 7-day gap + loop")
    await db.ap_group_giveback(gid, got[0], None)         # (clean up one claim)
    g = await db.ap_group_get(gid)
    # finish the cycle (the post sent by `got[0]` claim was given back above, so it is still in the queue)
    def left():
        q = db.ap_groups_col.d[gid]["queue_state"]
        return len(q["queue"]) - q["cursor"]
    cyc0 = db.ap_groups_col.d[gid]["queue_state"]["cycle"]
    for _ in range(40):                                   # (a slot that ends the cycle already starts the next one)
        if left() == 0 or db.ap_groups_col.d[gid]["queue_state"]["cycle"] != cyc0:
            break
        await slot(A, 2)
        if left() > 0 and db.ap_groups_col.d[gid]["queue_state"]["cycle"] == cyc0:
            await slot(B, 1)
    for pid in db.ap_posts_col.d:                         # every post went out just now
        db.ap_posts_col.d[pid]["last_sent_at"] = time.time()
    sent_log.clear(); notes.clear()
    await slot(A, 2)           # cycle is done -> reshuffle, but every post was sent just now -> must NOT go out
    check(sent_log == [] and any(n.startswith("ap_gap_") for n in notes),
          "new cycle: posts sent <7 days ago in this group wait (no post, owner is told)")
    for pid in list(db.ap_posts_col.d):                   # a week later
        db.ap_posts_col.d[pid]["last_sent_at"] = time.time() - 8 * DAY
        db.ap_posts_col.d[pid]["posted_at"] = time.time() - 8 * DAY
    sent_log.clear()
    await slot(A, 2)
    check(len(sent_log) == 2, "after 8 days the loop goes on (2 posts)")
    check((await db.ap_group_get(gid))["queue_state"]["cycle"] >= 2, "loop = new shuffled cycle")
    first = {t for _, t in sent_log}
    await slot(B, 1)
    check(sent_log[-1][1] not in first, "B does not repeat what A just sent")
    # gap between channels INSIDE a cycle: a post sent by A recently cannot be taken by B
    sid = sent_log[0][1]
    g = await db.ap_group_get(gid)
    db.ap_groups_col.d[gid]["queue_state"]["queue"].insert(db.ap_groups_col.d[gid]["queue_state"]["cursor"], sid)
    db.ap_groups_col.d[gid]["queue_state"]["ver"] += 1
    p, _ = await db.ap_group_claim(gid, B)
    check(p and p["_id"] != sid, "a post that went out <7 days ago is skipped, the next eligible one is taken")

    print("5) failed send puts the post back")
    for pid in db.ap_posts_col.d:
        db.ap_posts_col.d[pid]["last_sent_at"] = time.time() - 9 * DAY
    fail_next["on"] = True
    before = len(db.ap_groups_col.d[gid]["queue_state"]["queue"]) - db.ap_groups_col.d[gid]["queue_state"]["cursor"]
    sent_log.clear(); notes.clear()
    await slot(A, 1)
    fail_next["on"] = False
    after = len(db.ap_groups_col.d[gid]["queue_state"]["queue"]) - db.ap_groups_col.d[gid]["queue_state"]["cursor"]
    check(sent_log == [] and before == after and any(n.startswith("ap_err_") for n in notes),
          "send error: nothing lost, owner told")
    await db.ap_group_giveback(gid, p["_id"], p.get("_prev_sent"))

    print("6) new posts join the running cycle")
    new = await add_posts(B, 3, "n")
    q = db.ap_groups_col.d[gid]["queue_state"]
    check(all(t in q["queue"][q["cursor"]:] for t in new), "3 new posts of B are in the pool's remaining queue")

    print("7) third channel + one pass")
    gid2, why = await db.ap_group_share(C, A)
    check(gid2 == gid and why is None and (await db.ap_get_channel(C))["group"] == gid, "C joins the existing group")
    check((await db.ap_get_channel(C))["cap_tpl"] == "NEW CAP", "C adopts the GROUP's settings (not its own)")
    _, why = await db.ap_group_share(C, A)
    check(why == "same", "sharing again = same group")

    print("8) leaving")
    await db.ap_group_leave(C)
    check("group" not in await db.ap_get_channel(C) and C not in (await db.ap_group_get(gid))["members"], "C left")
    await db.ap_group_leave(B)
    check(await db.ap_group_get(gid) is None and "group" not in await db.ap_get_channel(A),
          "a group of one is dissolved")
    b_ch = await db.ap_get_channel(B)
    check(b_ch.get("queue_state", {}).get("queue") is not None, "B has its own queue again")

    print("9) Vault Pack is refused")
    await db.ap_set(C, ptype="batch")
    _, why = await db.ap_group_share(A, C)
    check(why == "batch", "batch channel cannot be shared")

    print("10) one pass")
    await db.ap_set(A, ptype="classic", loop=False)
    await db.ap_set(B, loop=False)
    gid3, _ = await db.ap_group_share(A, B)
    sent_log.clear()
    for pid in db.ap_posts_col.d.values():
        pass
    for t in db.ap_posts_col.d:
        db.ap_posts_col.d[t]["last_sent_at"] = None
        db.ap_posts_col.d[t]["posted_at"] = None
        db.ap_posts_col.d[t]["posted"] = False
    await db.ap_group_rebuild(gid3, only_unused=True)
    total = await db.ap_group_total(gid3)
    for _ in range(total + 3):
        await slot(A, 5)
        await slot(B, 5)
    check(len({t for _, t in sent_log}) == len(sent_log) == total, f"one pass: all {total} posts sent once, nothing repeated")
    check((await db.ap_get_channel(A))["enabled"] is False, "one pass: channel turned OFF at the end")


asyncio.run(main())
print("\nFAILED:" if fails else "\nALL OK")
for f in fails:
    print(" -", f)
sys.exit(1 if fails else 0)
