"""Offline flow test for CHANNEL-WISE Ads / Limit (stdlib only; telegram / motor / aiohttp are stubs, Mongo is in-memory).

Run:  python3 tests/test_channel_opts.py
Checks: apchopt clamps 0..10 and saves on the channel, post screen is read-only, links carry "chan", old posts get "chan"
at start-up, gate says "WATCH 2 ADS", the real api_claim runs the 2-ad session, files arrive after BOTH ads, the daily
limit is charged with the CHANNEL's cost (0 = free), live changes apply at once, links without "chan" = 1 ad / 1 count.
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


# ------------------------------------------------------------------ tiny in-memory Mongo
def _match(doc, flt):
    for k, cond in flt.items():
        v = doc.get(k)
        if isinstance(cond, dict):
            for op, x in cond.items():
                if op == "$lte" and not (v is not None and v <= x): return False
                if op == "$gte" and not (v is not None and v >= x): return False
                if op == "$ne" and v == x: return False
                if op == "$in" and v not in x: return False
                if op == "$exists" and (k in doc) != x: return False
        elif v != cond:
            return False
    return True


def _apply(doc, upd):
    for k, v in upd.get("$set", {}).items():
        doc[k] = v
    for k, v in upd.get("$inc", {}).items():
        doc[k] = doc.get(k, 0) + v
    for k, v in upd.get("$push", {}).items():
        doc.setdefault(k, []).extend(v["$each"] if isinstance(v, dict) else [v])


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()

    def sort(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def skip(self, *a, **k): return self
    async def to_list(self, *a, **k): return self.docs


class Col:
    def __init__(self):
        self.d = {}

    async def insert_one(self, doc):
        if doc["_id"] in self.d:
            raise DupKey()
        self.d[doc["_id"]] = copy.deepcopy(doc)

    async def find_one(self, flt=None, proj=None, **kw):
        flt = flt or {}
        for doc in self.d.values():
            if _match(doc, flt):
                return copy.deepcopy(doc)

    def find(self, flt=None, proj=None, **kw):
        return _Cursor([copy.deepcopy(x) for x in self.d.values() if _match(x, flt or {})])

    async def update_one(self, flt, upd, upsert=False):
        for doc in self.d.values():
            if _match(doc, flt):
                _apply(doc, upd)
                return MagicMock(modified_count=1)
        if upsert and "_id" in flt:
            doc = {"_id": flt["_id"]}
            _apply(doc, upd)
            self.d[doc["_id"]] = doc
        return MagicMock(modified_count=0)

    async def update_many(self, flt, upd):
        for doc in list(self.d.values()):
            if _match(doc, flt):
                _apply(doc, upd)

    async def find_one_and_update(self, flt, upd, **kw):
        for doc in self.d.values():
            if _match(doc, flt):
                _apply(doc, upd)
                return copy.deepcopy(doc)


import config  # noqa: E402
from database import db  # noqa: E402
from database.db import settings as S  # noqa: E402

for name in ("links_col", "ap_channels_col", "ap_posts_col", "ads_col", "usage_col", "users_col", "stats_col", "settings_col", "earn_col"):
    setattr(db, name, Col())
S._data = copy.deepcopy(db.DEFAULTS)
S._data.update(admins=[42], db_channel={"id": -100, "title": "Store"}, ads_enabled=True, daily_limit=10)
S.set = AsyncMock()

from handlers import admin, autoposter, start  # noqa: E402,F401
from utils import ads  # noqa: E402

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


UID, CID = 777, -100500
ads.zone = lambda: "13042"
ads.mini_app_ready = lambda: True
delivered = []


async def fake_deliver(bot, chat_id, link):
    delivered.append(link["_id"])
    return 1
start._deliver = fake_deliver
start._last_open.clear()

screens = []


async def fake_show(update, text, markup=None, **kw):
    screens.append((text, markup))
autoposter.show = fake_show


def btns(markup):
    return [b for r in markup.inline_keyboard for b in r]


def bot():
    b = MagicMock()
    b.username = "bot"
    b.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    b.delete_message = AsyncMock()
    return b


async def press(data):                       # "apchopt:<cid>:ads:1" -> run the real action
    await autoposter.a_apchopt(MagicMock(), MagicMock(), data.split(":")[1:])


async def open_link(b, token):
    start._last_open.clear()
    link = await db.get_link(token)
    await start.request_access(b, UID, link)


async def watch_all(b, token, n):
    """Real api_claim, once per ad; the 'ad was really seen' flags are what the Mini App would have set."""
    states = []
    for _ in range(n):
        s = await db.ads_col.find_one({"uid": UID, "status": "open"})
        await db.ads_col.update_one({"_id": s["_id"]}, {"$set": {"shown": True, "ad_seen": True}})

        async def fake_auth(request, _sid=s["_id"]):
            return {}, await db.ads_col.find_one({"_id": _sid})
        ads._auth = fake_auth
        req = MagicMock()
        req.app = {"bot": b}
        r = await ads.api_claim(req)
        import json
        states.append(json.loads(r.body) if hasattr(r, "body") and isinstance(r.body, (bytes, str)) else r)
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)                # let _deliver_after_ad finish
    return states


async def main():
    ads._reply = lambda **kw: kw             # plain dict instead of an aiohttp response

    await db.ap_add_channel(CID, "My Channel")
    print("1) channel settings (apchopt)")
    ch = await db.ap_get_channel(CID)
    check(db.chan_opts(ch) == (1, 1), "default = 1 ad, 1 count")
    await press(f"apchopt:{CID}:ads:1")
    check((await db.ap_get_channel(CID))["ads_n"] == 2, "ads +1 -> 2, saved on the channel doc")
    for _ in range(15):
        await press(f"apchopt:{CID}:ads:1")
    check((await db.ap_get_channel(CID))["ads_n"] == 10, "ads clamps at 10")
    for _ in range(15):
        await press(f"apchopt:{CID}:cost:-1")
    check((await db.ap_get_channel(CID))["cost"] == 0, "cost clamps at 0")
    await press(f"apchopt:{CID}:cost:0")
    check((await db.ap_get_channel(CID))["cost"] == 0, "step 0 only refreshes")
    await db.ap_set(CID, ads_n=2, cost=1)
    await press(f"apchopt:{CID}:ads:0")
    text, mk = screens[-1]
    labels = [b.text for b in btns(mk)]
    low = [x.lower() for x in labels]      # the panel UI upper-cases button labels (project convention)
    check("📺 ads: 2" in low and "👤 limit: 1" in low, "show_channel has the 'Ads: N' and 'Limit: N' rows")
    check(sum(1 for b in btns(mk) if b.text in ("➖", "➕")) == 4, "each row has ➖ and ➕")

    print("2) links carry the channel")
    await db.create_link([11], 1, "classic", token="CLS", chan=CID)
    await db.create_link([12], 1, "preview", token="PRV", no_ads=True, chan=CID)
    await db.create_link([13], 1, "plain admin upload", token="PLN")
    check((await db.get_link("CLS")).get("chan") == CID, "classic/video link has chan")
    check("chan" not in await db.get_link("PRV"), "free preview link has NO chan")
    check("chan" not in await db.get_link("PLN"), "normal admin link has no chan")

    print("3) old posts get chan at start-up")
    await db.create_link([14], 1, "old classic", token="OLD")
    await db.ap_add_post(CID, "OLD", None, 1, "old", "", "photo", 1, 1)
    await db.create_link([15], 1, "old pair video", token="OLDV")
    await db.ap_add_post(CID, "OLDP", None, 2, "old pair", "", "photo", 2, 1, ptype="pair", vtoken="OLDV")
    await db._tag_post_links()
    check((await db.get_link("OLD")).get("chan") == CID, "old classic link tagged")
    check((await db.get_link("OLDV")).get("chan") == CID, "old pair/batch video link tagged")

    print("4) post screen is read-only")
    await db.ap_add_post(CID, "CLS", None, 3, "classic", "", "photo", 1, 1)
    await autoposter.show_post(MagicMock(), bot(), "CLS")
    text, mk = screens[-1]
    check("📺 Ads: 2 · 👤 Limit: 1 (channel setting)" in text, "post shows the read-only channel line")
    check(not any("➖" in b.text or "➕" in b.text for b in btns(mk)), "post has no edit buttons")
    check(not hasattr(autoposter, "a_apopt") and "apopt" not in autoposter.ACTIONS if hasattr(autoposter, "ACTIONS") else not hasattr(autoposter, "a_apopt"),
          "old per-post action apopt is gone")

    print("5) FULL FLOW: channel ads=2, cost=1")
    b = bot()
    await open_link(b, "CLS")
    msg = b.send_message.call_args[0][1] if b.send_message.call_args else ""
    check("WATCH 2 ADS" in msg, "gate message says WATCH 2 ADS")
    check(delivered == [], "no file before the ads")
    check(await db.used_today(UID) == 0, "nothing charged before the ads")
    st = await watch_all(b, "CLS", 1)
    check(st[0].get("state") == "next" and delivered == [], "after ad 1: state 'next', still no file")
    st = await watch_all(b, "CLS", 1)
    check(st[0].get("state") == "done" and delivered == ["CLS"], "after ad 2: file delivered once")
    check(await db.used_today(UID) == 1, "daily limit charged 1 (channel cost)")

    print("6) LIVE: change channel -> same old link follows at once")
    await press(f"apchopt:{CID}:cost:1")      # cost 1 -> 2
    await press(f"apchopt:{CID}:ads:-1")      # ads 2 -> 1
    delivered.clear(); b = bot()
    await open_link(b, "OLD")                  # an OLD post (migrated link)
    check("WATCH AN AD" in b.send_message.call_args[0][1], "old post now asks 1 ad (live)")
    await watch_all(b, "OLD", 1)
    check(delivered == ["OLD"] and await db.used_today(UID) == 3, "old post charged the new cost 2 (1+2=3)")

    print("7) cost = 0 -> free, ads = 0 -> no gate")
    await db.ap_set(CID, ads_n=0, cost=0)
    delivered.clear(); b = bot()
    await open_link(b, "CLS")
    check(delivered == ["CLS"] and not b.send_message.call_args_list, "ads=0: file straight away, no gate")
    check(await db.used_today(UID) == 3, "cost=0: no count deducted")

    print("8) link without chan = old default (1 ad, 1 count)")
    await db.ap_set(CID, ads_n=5, cost=5)
    delivered.clear(); b = bot()
    await open_link(b, "PLN")
    check("WATCH AN AD" in b.send_message.call_args[0][1], "plain link: 1 ad (channel setting ignored)")
    await watch_all(b, "PLN", 1)
    check(delivered == ["PLN"] and await db.used_today(UID) == 4, "plain link: 1 count")

    print("9) free preview link never gated or charged")
    delivered.clear(); b = bot()
    await open_link(b, "PRV")
    check(delivered == ["PRV"] and await db.used_today(UID) == 4, "preview: no ad, no count")

    print("10) refund when nothing delivered")
    async def zero(bot_, chat, link): return 0
    start._deliver = zero
    await db.ap_set(CID, ads_n=0, cost=2)
    before = await db.used_today(UID)
    await open_link(bot(), "CLS")
    check(await db.used_today(UID) == before, "failed delivery refunds the channel cost")

asyncio.run(main())
print("\nFAILED:" if fails else "\nALL OK")
for f in fails:
    print(" -", f)
sys.exit(1 if fails else 0)
