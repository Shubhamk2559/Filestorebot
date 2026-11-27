"""Offline end-to-end test of Local Database Backup / Recover (stdlib only).

Run:  python3 tests/test_localbackup.py
Telegram, MongoDB and Pyrogram are replaced by small in-memory fakes. Covers: planning / splitting, streamed zip parts,
verification, delivery + deletion, resume bookkeeping, recover into a DIFFERENT channel (ids collide on purpose),
idempotent re-run, a corrupted file, captions / kinds / link order / links resolving.
"""
import asyncio
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types
import zipfile
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="lbtest_")
for k, v in dict(BOT_TOKEN="1:x", API_ID="1", API_HASH="x", MONGO_URI="mongodb://x", OWNER_ID="1",
                 BASE_URL="https://x.y", BK_TMP_DIR=f"{TMP}/tmp", RESTORE_DIR=f"{TMP}/restore").items():
    os.environ[k] = v

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("  FAIL", msg)


# ------------------------------------------------------------------ module stubs
def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class TgErr(Exception):
    pass


class Btn:
    def __init__(self, text, callback_data=None, url=None, **kw):
        self.text, self.callback_data, self.url = text, callback_data, url


class Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = [list(r) for r in inline_keyboard]


class UpdateOne:
    def __init__(self, flt, update, upsert=False):
        self.flt, self.update, self.upsert = flt, update, upsert


mod("telegram", InlineKeyboardButton=Btn, InlineKeyboardMarkup=Markup, LinkPreviewOptions=MagicMock(), WebAppInfo=MagicMock())
mod("telegram.error", TelegramError=TgErr, BadRequest=type("BadRequest", (TgErr,), {}),
    RetryAfter=type("RetryAfter", (TgErr,), {}), Forbidden=type("Forbidden", (TgErr,), {}))
mod("telegram.constants", ParseMode=MagicMock())
mod("telegram.ext", filters=MagicMock())
mod("motor", ); mod("motor.motor_asyncio", AsyncIOMotorClient=MagicMock())
mod("pymongo", UpdateOne=UpdateOne, ReturnDocument=MagicMock())
mod("pymongo.errors", DuplicateKeyError=type("DuplicateKeyError", (Exception,), {}),
    BulkWriteError=type("BulkWriteError", (Exception,), {}))
bson = mod("bson")
bson.json_util = mod("bson.json_util", dumps=lambda o, json_options=None: json.dumps(o, default=str),
                     loads=lambda s, json_options=None: json.loads(s), CANONICAL_JSON_OPTIONS=None)
mod("dotenv", load_dotenv=lambda: None)
mod("aiohttp", web=MagicMock())
sys.modules["aiohttp.web"] = sys.modules["aiohttp"].web
mod("pyrogram", enums=types.SimpleNamespace(ParseMode=types.SimpleNamespace(HTML="html")))


# ------------------------------------------------------------------ fake MongoDB
def _match(doc, flt):
    for k, want in flt.items():
        have = doc.get(k)
        if isinstance(want, dict):
            for op, v in want.items():
                if op == "$in" and have not in v:
                    return False
                if op == "$gte" and not (have is not None and have >= v):
                    return False
                if op == "$lt" and not (have is not None and have < v):
                    return False
                if op == "$ne" and have == v:
                    return False
                if op == "$exists" and ((k in doc) != v):
                    return False
        elif have != want:
            return False
    return True


def _setpath(doc, path, val):
    parts = path.split(".")
    cur = doc
    for i, p in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if isinstance(cur, list):
            p = int(p)
            while len(cur) <= p:
                cur.append(None)
            if cur[p] is None:
                cur[p] = [] if nxt.isdigit() else {}
            cur = cur[p]
        else:
            cur = cur.setdefault(p, [] if nxt.isdigit() else {})
    last = parts[-1]
    if isinstance(cur, list):
        last = int(last)
        while len(cur) <= last:
            cur.append(None)
        cur[last] = val
    else:
        cur[last] = val


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, d=1):
        self.docs.sort(key=lambda x: x.get(key), reverse=d < 0)
        return self

    async def to_list(self, n=None):
        return [copy.deepcopy(d) for d in self.docs]

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield copy.deepcopy(d)
        return gen()


class Col:
    def __init__(self):
        self.docs = {}
        self.n = 0

    def find(self, flt=None, proj=None):
        return Cursor([d for d in self.docs.values() if _match(d, flt or {})])

    async def find_one(self, flt, proj=None):
        for d in self.docs.values():
            if _match(d, flt):
                return copy.deepcopy(d)

    async def insert_one(self, d):
        self.docs[d["_id"]] = copy.deepcopy(d)

    async def insert_many(self, ds):
        for d in ds:
            await self.insert_one(d)

    async def replace_one(self, flt, d, upsert=False):
        old = await self.find_one(flt)
        if old:
            self.docs.pop(old["_id"])
        if old or upsert:
            self.docs[d["_id"]] = copy.deepcopy(d)

    async def update_one(self, flt, upd, upsert=False):
        if isinstance(upd, list):
            raise TypeError("pipeline updates unsupported in the fake")
        for d in self.docs.values():
            if _match(d, flt):
                self._apply(d, upd, False)
                return types.SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            d = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            if "_id" not in d:
                self.n += 1
                d["_id"] = f"auto{self.n}"
            self._apply(d, upd, True)
            self.docs[d["_id"]] = d
        return types.SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, flt, upd):
        if isinstance(upd, list):
            raise TypeError("pipeline updates unsupported in the fake")
        for d in self.docs.values():
            if _match(d, flt):
                self._apply(d, upd, False)

    def _apply(self, d, upd, inserting):
        for k, v in copy.deepcopy(upd.get("$set", {})).items():
            _setpath(d, k, v)
        if inserting:
            for k, v in copy.deepcopy(upd.get("$setOnInsert", {})).items():
                d[k] = v
        for k, v in upd.get("$inc", {}).items():
            d[k] = d.get(k, 0) + v

    async def delete_one(self, flt):
        for d in list(self.docs.values()):
            if _match(d, flt):
                self.docs.pop(d["_id"])
                return

    async def delete_many(self, flt):
        for d in list(self.docs.values()):
            if _match(d, flt):
                self.docs.pop(d["_id"])

    async def count_documents(self, flt, limit=0):
        return sum(1 for d in self.docs.values() if _match(d, flt))

    async def create_index(self, *a, **k):
        pass

    async def bulk_write(self, ops, ordered=False):
        for op in ops:
            await self.update_one(op.flt, op.update, op.upsert)


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, n):
        return self.cols.setdefault(n, Col())


import config  # noqa: E402
from database import db  # noqa: E402
from database.db import settings as S  # noqa: E402

FDB = FakeDB()
db.db = FDB
for attr, name in [("settings_col", "settings"), ("users_col", "users"), ("links_col", "links"), ("files_col", "files")]:
    setattr(db, attr, FDB[name])
S._data = copy.deepcopy(db.DEFAULTS)


async def _set(k, v):
    S._data[k] = v


S.set = _set
S.load = AsyncMock()

from utils import lbcore as C  # noqa: E402
from utils import lbbackup, lbrestore, lbpaths, lbweb  # noqa: E402
from utils.lbclient import lbclient  # noqa: E402


# ------------------------------------------------------------------ fake Telegram
class Str(str):
    @property
    def html(self):
        return str(self)


def mk_msg(mid, kind, data, caption="", name=None, **extra):
    obj = types.SimpleNamespace(file_size=len(data), file_name=name, mime_type="application/octet-stream",
                                duration=extra.get("duration"), width=extra.get("width"), height=extra.get("height"),
                                performer=None, title=None, length=None, supports_streaming=None,
                                thumbs=[types.SimpleNamespace(file_id=f"th{mid}", width=90)] if extra.get("thumb") else [])
    m = types.SimpleNamespace(id=mid, empty=False, media=types.SimpleNamespace(value=kind), caption=Str(caption) if caption else None,
                              date=None, data=data, thumb=extra.get("thumb"))
    setattr(m, kind, obj)
    return m


class FakeClient:
    """Pyrogram-like: a channel of stored messages + uploads into a (new) channel."""

    def __init__(self, store):
        self.store = store                      # {mid: message}
        self.sent = []                          # uploads into channels
        self.owner_docs = []                    # zip parts "sent to the owner"
        self.next_id = 1
        self.fail_owner_uploads = 0

    async def get_messages(self, chat, ids):
        if isinstance(ids, int):
            ids = [ids]
        return [self.store.get(i) or types.SimpleNamespace(id=i, empty=True) for i in ids]

    async def stream_media(self, msg, limit=0, offset=0):
        data = msg.data
        if limit:
            yield data[offset * C.MiB:(offset + limit) * C.MiB]
        else:
            for i in range(0, len(data), C.MiB):
                yield data[i:i + C.MiB]

    async def download_media(self, fid, in_memory=False):
        mid = int(fid[2:])
        t = self.store[mid].thumb
        return io.BytesIO(t) if t else None

    def _up(self, kind, chat_id, path, **kw):
        data = open(path, "rb").read()
        mid = self.next_id
        self.next_id += 1
        self.sent.append(dict(kind=kind, chat=chat_id, id=mid, data=data, **kw))
        return types.SimpleNamespace(id=mid, document=types.SimpleNamespace(file_size=len(data)))

    async def send_document(self, chat_id, document, thumb=None, caption=None, parse_mode=None, file_name=None,
                            force_document=None, progress=None):
        if chat_id == config.OWNER_ID:
            if self.fail_owner_uploads > 0:
                self.fail_owner_uploads -= 1
                raise IOError("simulated network failure")
            dst = os.path.join(TMP, "owner_inbox", file_name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(document, dst)
            self.owner_docs.append(file_name)
            return types.SimpleNamespace(id=1, document=types.SimpleNamespace(file_size=os.path.getsize(document)))
        return self._up("document", chat_id, document, caption=caption, file_name=file_name, thumb=thumb)

    async def send_video(self, chat_id, video, caption=None, parse_mode=None, duration=None, width=None, height=None,
                         thumb=None, file_name=None, supports_streaming=None, progress=None):
        return self._up("video", chat_id, video, caption=caption, duration=duration, thumb=thumb, file_name=file_name)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, progress=None):
        return self._up("photo", chat_id, photo, caption=caption)

    async def send_video_note(self, chat_id, video_note, duration=None, length=None, thumb=None, progress=None):
        return self._up("video_note", chat_id, video_note, caption=None, duration=duration)


def fake_bot():
    bot = MagicMock()
    bot.username = "testbot"
    bot.get_me = AsyncMock(return_value=types.SimpleNamespace(username="testbot", id=7))
    bot.send_message = AsyncMock(return_value=types.SimpleNamespace(chat_id=1, message_id=5))
    bot.edit_message_text = AsyncMock()
    bot.send_document = AsyncMock()
    return bot


# ------------------------------------------------------------------ seed data
BIG = os.urandom(900 * 1024)           # ~0.9 MB per file -> with a 2 MiB part limit: ~1-2 files per part
OLD_CHAN, NEW_CHAN = -1001, -1002
TOK, RNG, T3 = "AAAAAAAAAAAA", "RANGE0000001", "VNOTE0000001"
CAP1 = '<b>Bold</b> &amp; <i>it</i> <a href="https://e.org">l</a>'


def seed():
    store = {
        11: mk_msg(11, "video", BIG + b"v", CAP1 + "\n\n<code>FSB:AAAAAAAAAAAA:0</code>", name="movie.mp4", duration=61, thumb=b"JPGTHUMB"),
        12: mk_msg(12, "document", BIG + b"d", "doc cap\n\n<code>FSB:AAAAAAAAAAAA:1</code>", name="paper.pdf"),
        13: mk_msg(13, "photo", b"P" * 5000, "pic\n\n<code>FSB:AAAAAAAAAAAA:2</code>"),
        14: mk_msg(14, "document", BIG + b"r", "range only caption", name="range.bin"),     # no backup code
        15: mk_msg(15, "video_note", b"N" * 3000),
        16: mk_msg(16, "photo", b"T" * 2000, "<code>FSB:THUMBTOKEN01:0</code>"),
    }
    docs = {
        "links": [{"_id": TOK, "ids": [11, 12, 13], "creator": 5, "label": "Batch (3 files)", "created": 1.0, "views": 42},
                  {"_id": RNG, "ids": [12, 14, 17], "creator": 5, "label": "Range 12-17", "created": 2.0, "views": 7, "chan": 99},
                  {"_id": T3, "ids": [15], "creator": 5, "label": "note", "created": 3.0, "views": 0, "no_ads": True}],
        "files": [{"_id": "f1", "msg_id": 11, "kind": "video", "name": "movie.mp4", "by": 5, "date": 10.0,
                   "code": "FSB:AAAAAAAAAAAA:0", "cap": CAP1},
                  {"_id": "f2", "msg_id": 12, "kind": "document", "name": "paper.pdf", "by": 5, "date": 11.0,
                   "code": "FSB:AAAAAAAAAAAA:1", "cap": "doc cap"},
                  {"_id": "f3", "msg_id": 13, "kind": "photo", "name": "pic", "by": 5, "date": 12.0,
                   "code": "FSB:AAAAAAAAAAAA:2", "cap": "pic"},
                  {"_id": "f4", "msg_id": 15, "kind": "video_note", "name": "video_note", "by": 5, "date": 13.0},
                  {"_id": "f5", "msg_id": 16, "kind": "photo", "name": "thumb", "by": 5, "date": 14.0,
                   "code": "FSB:THUMBTOKEN01:0", "cap": ""}],
        "users": [{"_id": 100, "joined": 1.0, "banned": False}, {"_id": 101, "joined": 2.0, "banned": True}],
        "settings": [{"_id": "main", "protect": True, "db_channel": {"id": OLD_CHAN, "title": "old"}}],
        "ap_channels": [{"_id": -777, "title": "AP", "slots": []}],
        "ap_posts": [{"_id": "POST00000001", "chan": -777, "seq": 1}],
    }
    for name, items in docs.items():
        FDB[name].docs = {d["_id"]: copy.deepcopy(d) for d in items}
    return store


async def wait(runner_):
    if runner_.task:
        await asyncio.wait_for(asyncio.shield(runner_.task), float(os.getenv("T_WAIT","60")))


# ------------------------------------------------------------------ pure unit tests
def unit():
    check(C.plan_parts([], 100) == [(0, 0)], "empty plan -> one (mongo-only) part")
    plan = C.plan_parts([C.BASE_OVERHEAD // 2] * 5, C.BASE_OVERHEAD + C.BASE_OVERHEAD // 2 * 2)
    check(plan == [(0, 2), (2, 4), (4, 5)], f"plan split {plan}")
    huge = C.plan_parts([10, 10 * C.GiB, 10], C.GiB)
    check(huge == [(0, 1), (1, 2), (2, 3)], f"oversize file gets its own part {huge}")
    first = C.plan_parts([600 * C.MiB, 100], C.GiB, first_reserve=700 * C.MiB)
    check(first[0] == (0, 0), f"part 1 may hold only the DB export {first}")
    check(C.parse_part_name("dbbackup_20260101_part012.zip") == ("20260101", 12), "part name")
    check(C.part_name("20260101", 1) == "dbbackup_20260101_part001.zip", "part_name")
    cap = C.caption_with_code("<b>x</b>", "FSB:T:0")
    check(cap == "<b>x</b>\n\n<code>FSB:T:0</code>", "caption + code")
    long = C.caption_with_code("a" * 1100, "FSB:TTTTTT:0")
    check(len(long) < 1100 and long.endswith("<code>FSB:TTTTTT:0</code>"), "long caption falls back to trimmed plain text")
    check(C.human(1536 * C.MiB) == "1.5 GB", "human")
    p = os.path.join(TMP, "u.zip")
    w = C.PartWriter(p)
    with w.entry("media/a/0.bin", 5) as e:
        e.write(b"he"), e.write(b"llo")
    w.finish({"format": C.FORMAT, "files": [{"path": "media/a/0.bin", "size": 5}]})
    ok, why, m = C.verify_zip(p)
    check(ok, f"verify_zip {why}")
    ok2, why2 = C.extract_entry(zipfile.ZipFile(p), "media/a/0.bin", os.path.join(TMP, "x"), "bad", 5)
    check(not ok2 and "sha256" in why2 and not os.path.exists(os.path.join(TMP, "x")), "extract detects wrong sha and deletes")


# ------------------------------------------------------------------ the scenario
async def scenario():
    store = seed()
    client = FakeClient(store)
    old_client_dir = None

    async def acquire():
        return client

    lbclient.acquire, lbclient.release = acquire, AsyncMock()
    lbbackup.part_limit = lambda: 2 * C.MiB
    lbrestore.PACE = 0
    S._data["db_channel"] = {"id": OLD_CHAN, "title": "old"}
    bot = fake_bot()
    R = lbbackup.runner
    await lbweb.init()

    # ---------------- BACKUP
    print("backup…")
    await R.start_scan(bot, 1, 2)
    await wait(R)
    j = R.job
    check(j["status"] == "ready", f"scan status {j['status']} {j.get('error')}")
    check(j["total_files"] == 6, f"planned files {j['total_files']} (expected 6: 5 slots + range-only; 17 is deleted)")
    check(j["skipped_n"] == 1 and "deleted" in j["skipped"][0]["reason"], f"skipped {j['skipped']}")
    check(j["total_parts"] >= 3, f"parts {j['total_parts']}")
    await R.start(bot)
    await wait(R)
    j = R.job
    check(j["status"] == "done", f"backup status {j['status']} {j.get('error')}")
    check(len(j["parts"]) == j["total_parts"] and all(p["delivered"] for p in j["parts"]), "every part delivered")
    check(os.listdir(lbpaths.tmp_dir()) == [], f"temp folder not empty after backup: {os.listdir(lbpaths.tmp_dir())}")
    inbox = os.path.join(TMP, "owner_inbox")
    check(sorted(client.owner_docs) == [p["name"] for p in j["parts"]], f"owner got {client.owner_docs}")
    for p in j["parts"]:
        path = os.path.join(inbox, p["name"])
        check(C.sha256_file(path) == p["sha256"], f"sha of {p['name']}")
        ok, why, m = C.verify_zip(path)
        check(ok, f"{p['name']} verify: {why}")
        check(os.path.getsize(path) <= 2 * C.MiB + 2 * C.MiB, f"{p['name']} size {os.path.getsize(path)}")
        check(m["part"] == int(p["name"][-7:-4]) and m["total_parts"] == j["total_parts"], "manifest numbering")
    m1 = json.loads(zipfile.ZipFile(os.path.join(inbox, j["parts"][0]["name"])).read("manifest.json"))
    check(m1["mongo"] and "mongo_export.jsonl.gz" in zipfile.ZipFile(os.path.join(inbox, j["parts"][0]["name"])).namelist(), "part 1 holds the Mongo export")
    allfiles = {}
    for p in j["parts"]:
        for f in json.loads(zipfile.ZipFile(os.path.join(inbox, p["name"])).read("manifest.json"))["files"]:
            check(f["key"] not in allfiles, f"file {f['key']} stored twice")
            allfiles[f["key"]] = f
    check(set(allfiles) == {"AAAAAAAAAAAA:0", "AAAAAAAAAAAA:1", "AAAAAAAAAAAA:2", "RANGE0000001:1", "VNOTE0000001:0", "THUMBTOKEN01:0"},
          f"keys {sorted(allfiles)}")
    v = allfiles["AAAAAAAAAAAA:0"]
    check(v["caption_html"] == CAP1 and v["code"] == "FSB:AAAAAAAAAAAA:0" and v["kind"] == "video" and v["duration"] == 61
          and v["thumb"] and v["name"] == "movie.mp4" and v["refs"] == [["AAAAAAAAAAAA", 0]], f"video entry {v}")
    check(allfiles["AAAAAAAAAAAA:1"]["refs"] == [["AAAAAAAAAAAA", 1], ["RANGE0000001", 0]], "shared message keeps both link slots")
    check(allfiles["VNOTE0000001:0"]["kind"] == "video_note", "video note kind")
    idx = json.loads(bot.send_document.call_args.kwargs["document"].read()) if False else None  # index goes to the bot API

    # ---------------- RECOVER into a NEW channel (ids restart at 1 -> collide with old ones)
    print("recover…")
    rdir = lbpaths.restore_dir()
    for p in j["parts"]:
        shutil.copy(os.path.join(inbox, p["name"]), rdir)
    # index file as the bot would have written it
    open(os.path.join(rdir, C.INDEX_NAME), "w").write(json.dumps(C.build_index(j, j["parts"], True)))
    info = lrinfo = lbrestore.analyze(rdir)
    check(info["set_id"] == j["set_id"] and info["missing"] == [], f"analyze {info['set_id']} missing {info['missing']}")
    # simulate a lost DB-channel: new channel, stale ids in MongoDB that equal NEW message ids
    S._data["db_channel"] = {"id": NEW_CHAN, "title": "new"}
    client.store = {}
    c2 = client
    names = [r["name"] for r in info["parts"]]
    # part 2 is withheld first (parts can arrive in any order / later)
    held = os.path.join(TMP, "held")
    os.makedirs(held)
    shutil.move(os.path.join(rdir, names[1]), held)
    check(lbrestore.analyze(rdir)["missing"] == [2], "missing part is detected")
    rr = lbrestore.restorer
    await rr.start(bot, 1, 3, [n for n in names if n != names[1]])
    await wait(rr)
    check(rr.job["status"] == "done", f"restore1 {rr.job['status']} {rr.job.get('error')}")
    first_sends = len(client.sent)
    check(first_sends >= 1, "files were uploaded")
    check(all(s["chat"] == NEW_CHAN for s in client.sent), "uploads go to the NEW channel")
    # stale-id protection: links not restored yet must be empty (no old ids that collide with new messages)
    l = FDB["links"].docs[TOK]["ids"]
    check(all(x is None or x in {s["id"] for s in client.sent} for x in l), f"link ids {l} must be None or new ids")

    # the rest arrives later
    shutil.move(os.path.join(held, names[1]), rdir)
    await rr.start(bot, 1, 3, names)
    await wait(rr)
    check(rr.job["status"] == "done", f"restore2 {rr.job['status']} {rr.job.get('error')}")
    check(rr.counts["failed"] == 0, f"failed {rr.counts}")
    new_by_id = {s["id"]: s for s in client.sent}
    check(len(client.sent) == 6, f"6 uploads in total, got {len(client.sent)} (no duplicates)")
    L = FDB["links"].docs
    t_ids = L[TOK]["ids"]
    check(len(t_ids) == 3 and all(t_ids), f"token link restored {t_ids}")
    s0 = new_by_id[t_ids[0]]
    check(s0["kind"] == "video" and s0["data"] == BIG + b"v" and s0["file_name"] == "movie.mp4" and s0["duration"] == 61, "video restored as video")
    check(s0["caption"] == CAP1 + "\n\n<code>FSB:AAAAAAAAAAAA:0</code>", f"caption {s0['caption']!r}")
    check(s0["thumb"] is not None, "thumbnail passed")
    check(new_by_id[t_ids[2]]["kind"] == "photo", "photo restored as photo")
    r_ids = L[RNG]["ids"]
    check(r_ids[0] == t_ids[1] and r_ids[1] and r_ids[2] is None and len(r_ids) == 3, f"range link {r_ids}")
    check(L[RNG]["views"] == 7 and L[RNG]["label"] == "Range 12-17" and L[RNG].get("chan") == 99, "range link meta kept")
    check("FSB:RANGE0000001:1" in new_by_id[r_ids[1]]["caption"], "range-only file got a backup code")
    check(new_by_id[L[T3]["ids"][0]]["kind"] == "video_note" and new_by_id[L[T3]["ids"][0]]["caption"] is None, "video note has no caption")
    fdocs = list(FDB["files"].docs.values())
    f_v = next(d for d in fdocs if d.get("code") == "FSB:AAAAAAAAAAAA:0")
    check(f_v["msg_id"] == t_ids[0] and f_v["cap"] == CAP1, "files doc points to the new message with the original caption")
    check(len({d["msg_id"] for d in fdocs}) == len(fdocs), "no two file records share a message id")
    check(L[TOK]["views"] == 42, "views kept")
    check(os.listdir(rdir) == [".partial"] or [n for n in os.listdir(rdir) if n.startswith("dbbackup_") and n.endswith(".zip")] == [],
          f"parts are deleted after a full restore: {os.listdir(rdir)}")

    # ---------------- idempotent re-run: parts are gone, put them back -> nothing is uploaded twice
    print("re-run…")
    for p in j["parts"]:
        shutil.copy(os.path.join(inbox, p["name"]), rdir)
    before = len(client.sent)
    await rr.start(bot, 1, 3, names)
    await wait(rr)
    check(len(client.sent) == before, f"re-run uploaded {len(client.sent) - before} duplicates")
    check(rr.counts["skipped"] == 6 and rr.counts["restored"] == 0, f"re-run counts {rr.counts}")
    check(L[TOK]["ids"] == t_ids, "links unchanged by the re-run")

    # ---------------- corrupted file -> reported, never silently skipped
    print("corrupt file…")
    FDB["lb_rest_items"].docs.clear()
    FDB["links"].docs[TOK]["ids"] = [None, None, None]
    FDB["lb_jobs"].docs.pop("restore")
    for p in j["parts"]:
        shutil.copy(os.path.join(inbox, p["name"]), rdir)
    target = next(r for r in lbrestore.analyze(rdir)["parts"] if any(f["key"] == "AAAAAAAAAAAA:2" for f in r["manifest"]["files"]))
    raw = bytearray(open(target["path"], "rb").read())
    pos = raw.find(b"P" * 100)
    raw[pos + 10] ^= 0xFF
    open(target["path"], "wb").write(bytes(raw))
    FDB["lb_rest_items"].docs.clear()
    bot.send_message.reset_mock()
    await rr.start(bot, 1, 3, names)
    await wait(rr)
    failed = [d for d in FDB["lb_rest_items"].docs.values() if d.get("status") == "failed"]
    check(len(failed) == 1 and failed[0]["key"] == "AAAAAAAAAAAA:2", f"corrupt file listed as failed: {failed}")
    check(os.path.exists(target["path"]), "a part with a failed file is kept on the VPS")
    texts = " ".join(str(c.args[1]) if len(c.args) > 1 else str(c.kwargs.get("text", "")) for c in bot.send_message.call_args_list)
    check("AAAAAAAAAAAA:2" in texts and "checksum" in texts, "failure reason reported to the owner")
    check(FDB["links"].docs[TOK]["ids"][2] is None and FDB["links"].docs[TOK]["ids"][0], "failed slot stays empty, others restored")

    # ---------------- delivery failure: 3 failures -> nothing deleted, paused; then manual confirm
    print("delivery failure…")
    store2 = seed()
    client.store, client.sent, client.owner_docs = store2, [], []
    S._data["db_channel"] = {"id": OLD_CHAN, "title": "old"}
    for f in os.listdir(inbox):
        os.remove(os.path.join(inbox, f))
    bot2 = fake_bot()
    await R.start_scan(bot2, 1, 2)
    await wait(R)
    client.fail_owner_uploads = 3
    real_sleep = asyncio.sleep

    async def fast_sleep(t, *a):
        return await real_sleep(0 if t >= 1 else t)
    lbbackup.asyncio.sleep = fast_sleep
    await R.start(bot2)
    for _ in range(200):
        await real_sleep(0.05)
        if R.job["status"] == "waiting":
            break
    check(R.job["status"] == "waiting", f"paused after 3 failed uploads: {R.job['status']}")
    check(os.listdir(lbpaths.tmp_dir()) != [], "part is NOT deleted while delivery is unconfirmed")
    R.decide("retry")
    await wait(R)
    check(R.job["status"] == "done", f"finished after retry: {R.job['status']} {R.job.get('error')}")
    check(os.listdir(lbpaths.tmp_dir()) == [], "temp folder empty at the end")
    lbbackup.asyncio.sleep = real_sleep

    # ---------------- stop after the current part, then resume
    print("stop / resume…")
    client.sent, client.owner_docs = [], []
    await R.start_scan(bot2, 1, 2)
    await wait(R)
    await R.start(bot2)
    for _ in range(400):                               # stop while part 1 is in flight
        await asyncio.sleep(0.01)
        if R.job.get("step") in ("building", "uploading") or R.job["parts"]:
            break
    R.request_stop()
    await wait(R)
    n_done = len(R.job["parts"])
    check(R.job["status"] == "stopped" and 1 <= n_done < R.job["total_parts"], f"stopped after the current part: {R.job['status']} {n_done}")
    check(R.job["cur_part"] == n_done + 1 and os.listdir(lbpaths.tmp_dir()) == [], "stop leaves no temp file")
    await R.start(bot2)
    await wait(R)
    check(R.job["status"] == "done" and len(R.job["parts"]) == R.job["total_parts"], "resume finishes the remaining parts")


# ------------------------------------------------------------------ parallel downloader
LBM = sys.modules["utils.lbclient"]                    # the module (tests change its tuning constants)


class FileReferenceExpired(Exception):                 # same class name as Pyrogram's
    pass


class SlowClient(FakeClient):
    """FakeClient with per-call latency that makes later files finish BEFORE earlier ones, a call log, a byte counter
    (bytes served - bytes written = what sits in RAM), expiring file references and files that always fail."""

    def __init__(self, store):
        super().__init__(store)
        self.ver = {m: 0 for m in store}
        self.expire_once = set()
        self.always_fail = set()
        self.calls, self.done_order, self.msg_calls = [], [], []
        self.cur = self.peak = 0
        self.served = 0
        self.ram_peak = 0
        self.written = [0]
        self.thumbs_done = 0

    async def get_messages(self, chat, ids):
        ids = [ids] if isinstance(ids, int) else list(ids)
        self.msg_calls.append(list(ids))
        out = await super().get_messages(chat, ids)
        res = []
        for m in out:
            m2 = types.SimpleNamespace(**vars(m))
            m2.ref = self.ver.get(m2.id, 0)
            res.append(m2)
        return res

    async def stream_media(self, msg, limit=0, offset=0):
        mid = msg.id
        self.cur += 1
        self.peak = max(self.peak, self.cur)
        self.calls.append((mid, offset, limit))
        try:
            if mid in self.expire_once:
                self.expire_once.discard(mid)
                self.ver[mid] += 1
                await asyncio.sleep(0.002)
                raise FileReferenceExpired("[400 FILE_REFERENCE_EXPIRED]")
            if getattr(msg, "ref", 0) != self.ver.get(mid, 0):
                await asyncio.sleep(0.002)
                raise FileReferenceExpired("[400 FILE_REFERENCE_EXPIRED]")
            if mid in self.always_fail:
                await asyncio.sleep(0.002)
                raise IOError("simulated broken file")
            await asyncio.sleep(0.004 * (30 - mid % 30) / 30 + 0.003 * ((offset * 7 + mid) % 4))   # later files are faster
            data = msg.data
            part = data[offset * C.MiB:(offset + limit) * C.MiB] if limit else data
            pieces = [part[i:i + C.MiB] for i in range(0, len(part), C.MiB)] or [b""]
            for pc in pieces:
                self.served += len(pc)
                self.ram_peak = max(self.ram_peak, self.served - self.written[0])
                yield pc
            self.done_order.append((mid, offset))
        finally:
            self.cur -= 1

    async def download_media(self, fid, in_memory=False):
        await asyncio.sleep(0.003)
        self.thumbs_done += 1
        return await super().download_media(fid, in_memory)


def seed_many():
    """18 files: tiny, medium and multi-stripe ones; some with thumbnails; the link order is NOT the message-id order."""
    store, ids = {}, []
    sizes = [300_000, 3 * C.MiB + 17, 40_000, C.MiB, 4 * C.MiB + 5, 100_000, 2 * C.MiB + 1, 20_000, 2 * C.MiB,
             650_000, 1, 5 * C.MiB + 3, 10_000, C.MiB + 700, 800_000, 5_000, 3 * C.MiB, 123_457]
    for n, size in enumerate(sizes):
        mid = 1000 + n * 3
        kind = "video" if n % 3 == 0 else "document"
        store[mid] = mk_msg(mid, kind, os.urandom(size), f"cap {n}\n\n<code>FSB:PTOKEN{n // 6:06d}00:{n % 6}</code>",
                            name=f"f{n}.bin", thumb=(b"THUMB%d" % n) if n % 3 == 0 else None, duration=3)
        ids.append(mid)
    links = [{"_id": f"PTOKEN{t:06d}00", "ids": ids[t * 6:(t + 1) * 6], "creator": 5, "label": f"L{t}",
              "created": float(t), "views": 0} for t in range(3)]
    files = [{"_id": f"p{n}", "msg_id": mid, "kind": "document", "name": f"f{n}.bin", "by": 5, "date": float(n),
              "code": f"FSB:PTOKEN{n // 6:06d}00:{n % 6}", "cap": f"cap {n}"} for n, mid in enumerate(ids)]
    return store, links, files


def reset_db(links, files):
    for name in list(FDB.cols):
        FDB.cols[name].docs = {}
    FDB["links"].docs = {d["_id"]: copy.deepcopy(d) for d in links}
    FDB["files"].docs = {d["_id"]: copy.deepcopy(d) for d in files}
    FDB["settings"].docs = {"main": {"_id": "main", "protect": True, "db_channel": {"id": OLD_CHAN, "title": "old"}}}


def zip_view(path):
    """(entry names in order, {name: bytes}, manifest) of one delivered part."""
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    skip = ("manifest.json", C.MONGO_ENTRY)               # the Mongo export carries ids / times that differ per run
    return names, {n: zf.read(n) for n in names if n not in skip}, json.loads(zf.read("manifest.json"))


async def run_backup(client, bot):
    """One complete backup with the current tuning; returns {part name: zip_view} read from the owner's inbox."""
    inbox = os.path.join(TMP, "owner_inbox")
    shutil.rmtree(inbox, ignore_errors=True)
    client.owner_docs.clear()
    R = lbbackup.runner
    await R.start_scan(bot, 1, 2)
    await wait(R)
    check(R.job["status"] == "ready", f"scan status {R.job['status']} {R.job.get('error')}")
    client.msg_calls.clear()
    await R.start(bot)
    await wait(R)
    check(R.job["status"] == "done", f"parallel backup status {R.job['status']} {R.job.get('error')}")
    return {p["name"]: zip_view(os.path.join(inbox, p["name"])) for p in R.job["parts"]}


async def parallel():
    print("parallel downloader…")
    store, links, files = seed_many()
    reset_db(links, files)
    client = SlowClient(store)

    async def acquire():
        return client

    lbclient.acquire, lbclient.release = acquire, AsyncMock()
    lbbackup.part_limit = lambda: 8 * C.MiB
    S._data["db_channel"] = {"id": OLD_CHAN, "title": "old"}
    bot = fake_bot()
    R = lbbackup.runner

    real_write = C.Entry.write

    def counting_write(self, b):
        client.written[0] += len(b)
        return real_write(self, b)
    C.Entry.write = counting_write

    keep = {k: getattr(LBM, k) for k in ("FILES", "TRANSFERS", "STRIPE", "BUFFER_MB", "WRITE_MB")}
    try:
        # ---- reference: strictly one file / one request at a time
        LBM.FILES, LBM.TRANSFERS, LBM.STRIPE, LBM.BUFFER_MB, LBM.WRITE_MB = 1, 1, 1, 128, 1
        ref = await run_backup(client, bot)
        ref_peak_calls = client.peak
        check(ref_peak_calls == 1, f"reference run is sequential (peak {ref_peak_calls})")
        ref_names = list(ref)
        check(len(ref) >= 3, f"several parts in the reference run: {len(ref)}")
        check(os.listdir(lbpaths.tmp_dir()) == [], "temp folder empty after the reference run")

        # ---- parallel, out of order, tiny buffer (smaller than 2 stripes): RAM must stay under it
        for cfg in ((5, 8, 2, 6, 4), (6, 16, 1, 3, 1)):     # files, transfers, stripe MiB, buffer MiB, write MiB
            client.calls.clear(), client.done_order.clear()
            client.peak = client.cur = client.served = client.ram_peak = 0
            client.written[0] = 0
            client.thumbs_done = 0
            LBM.FILES, LBM.TRANSFERS, LBM.STRIPE, LBM.BUFFER_MB, LBM.WRITE_MB = cfg
            got = await run_backup(client, bot)
            tag = f"files={cfg[0]} transfers={cfg[1]} stripe={cfg[2]} buffer={cfg[3]}MiB"
            check(list(got) == ref_names, f"[{tag}] same parts {list(got)} vs {ref_names}")
            for name in ref_names:
                n1, d1, m1 = ref[name]
                n2, d2, m2 = got.get(name, ([], {}, {"files": []}))
                check(n1 == n2, f"[{tag}] {name}: zip entry ORDER differs")
                check(d1 == d2, f"[{tag}] {name}: zip contents differ")
                f1 = [(f["key"], f["path"], f["size"], f["sha256"], f["msg_id"]) for f in m1["files"]]
                f2 = [(f["key"], f["path"], f["size"], f["sha256"], f["msg_id"]) for f in m2["files"]]
                check(f1 == f2, f"[{tag}] {name}: manifest files / order differ")
                for f in m2["files"]:
                    check(hashlib.sha256(d2[f["path"]]).hexdigest() == f["sha256"], f"[{tag}] sha256 of {f['path']}")
                    check(d2[f["path"]] == store[f["msg_id"]].data, f"[{tag}] bytes of {f['path']} differ from the channel")
                    if f["thumb"]:
                        check(d2[f["thumb"]["path"]] == store[f["msg_id"]].thumb, f"[{tag}] thumb of {f['path']}")
                check([f.get("thumb") is not None for f in m1["files"]] == [f.get("thumb") is not None for f in m2["files"]],
                      f"[{tag}] thumbnails differ")
            check(client.peak > 1, f"[{tag}] downloads really overlap (peak {client.peak})")
            # out of order: some stripe of a LATER file completed before an earlier file's last stripe
            order = {m: i for i, m in enumerate(sorted({c[0] for c in client.calls}))}
            seq = [order[m] for m, _ in client.done_order]
            check(seq != sorted(seq), f"[{tag}] stripes completed out of order {seq[:12]}")
            check(client.thumbs_done >= 6, f"[{tag}] thumbnails fetched ({client.thumbs_done})")
            # RAM: peak accounted by the prefetcher AND bytes served-but-not-written seen from outside
            buf = cfg[3] * C.MiB
            st = R.last_stats
            check(st.get("peak_buffer", 1 << 60) <= max(buf, cfg[2] * C.MiB), f"[{tag}] accounted peak {st.get('peak_buffer')} > {buf}")
            check(client.ram_peak <= max(buf, cfg[2] * C.MiB) + cfg[4] * C.MiB, f"[{tag}] RAM in flight {client.ram_peak} > {buf} + block")
            check(st.get("peak_files", 99) <= cfg[0], f"[{tag}] open files {st.get('peak_files')} > {cfg[0]}")
            check(st.get("peak_inflight", 99) <= cfg[1], f"[{tag}] in-flight {st.get('peak_inflight')} > {cfg[1]}")
            check(client.peak <= cfg[1], f"[{tag}] client saw {client.peak} calls at once, limit {cfg[1]}")
            # batched messages: ONE get_messages per part (ids of the whole part), no per-file calls
            singles = [c for c in client.msg_calls if len(c) == 1]
            check(not singles and all(len(c) > 1 for c in client.msg_calls) and len(client.msg_calls) == len(ref_names),
                  f"[{tag}] get_messages calls {[len(c) for c in client.msg_calls]}")
            check(os.listdir(lbpaths.tmp_dir()) == [], f"[{tag}] temp folder empty")

        # ---- FILE_REFERENCE_EXPIRED: only the affected message is read again, once
        LBM.FILES, LBM.TRANSFERS, LBM.STRIPE, LBM.BUFFER_MB, LBM.WRITE_MB = 4, 8, 1, 16, 4
        target = 1000 + 4 * 3                                       # the 4 MiB file: several stripes fail together
        client.expire_once = {target}
        got = await run_backup(client, bot)
        refresh = [c for c in client.msg_calls if len(c) == 1]
        check(refresh == [[target]], f"exactly one single-message refresh, got {refresh}")
        check(all(got[n][1] == ref[n][1] and got[n][0] == ref[n][0] for n in ref_names), "zip identical after a reference refresh")

        # ---- a file that always fails: reported, everything else intact, temp folder clean
        real_sleep = asyncio.sleep

        async def fast_sleep(t, *a):                       # retry back-offs (0.5 - 6 s) are skipped, the 4 s ticker is not
            return await real_sleep(0 if (t >= 0.4 and t != 4) else t)
        asyncio.sleep = fast_sleep
        try:
            bad = 1000 + 6 * 3
            client.always_fail = {bad}
            got = await run_backup(client, bot)
        finally:
            asyncio.sleep = real_sleep
        client.always_fail = set()
        stored = {f["msg_id"] for n in got for f in got[n][2]["files"]}
        check(bad not in stored and len(stored) == len(store) - 1, f"failing file is not in any manifest ({len(stored)} stored)")
        check(any(x["msg_id"] == bad for x in R.job["skipped"]), "failing file is listed as not backed up")
        okfiles = {f["msg_id"]: f for n in got for f in got[n][2]["files"]}
        check(all(okfiles[m]["sha256"] == hashlib.sha256(store[m].data).hexdigest() for m in okfiles), "all other files intact")
        check(os.listdir(lbpaths.tmp_dir()) == [], "temp folder empty after a failed file")
    finally:
        C.Entry.write = real_write
        for k, v in keep.items():
            setattr(LBM, k, v)


async def client_diag():
    """The dedicated client reports what it really got; the silent fallbacks are loud now."""
    print("client diagnostics…")
    from utils.lbclient import LBClient
    from utils import mtproto as MT
    started = []

    class NoOpt:                                       # old build: no max_concurrent_transmissions, semaphore of 1
        def __init__(self, name, api_id, api_hash, bot_token, in_memory, no_updates, sleep_threshold):
            self.get_file_semaphore = asyncio.Semaphore(1)

        async def start(self):
            started.append(self)

        async def stop(self):
            started.remove(self)

    class WithOpt(NoOpt):
        def __init__(self, name, max_concurrent_transmissions=1, **kw):
            super().__init__(name, **kw)
            self.get_file_semaphore = asyncio.Semaphore(max_concurrent_transmissions)

    class Broken(NoOpt):
        async def start(self):
            raise ConnectionError("no route to Telegram")

    pyro = sys.modules["pyrogram"]
    keep = getattr(pyro, "Client", None), MT.mtproto.client
    try:
        for cls, label in ((NoOpt, "option missing"), (WithOpt, "option ok")):
            pyro.Client = cls
            lb = LBClient()
            c = await lb.acquire()
            check(lb.dedicated and lb.eff == LBM.TRANSFERS, f"[{label}] semaphore raised to {lb.eff}")
            check(c.get_file_semaphore._value == LBM.TRANSFERS, f"[{label}] semaphore value {c.get_file_semaphore._value}")
            pool = await lb.pool(c, 3)
            check(len(pool) == 3 and len(started) == 3, f"[{label}] pool of 3 clients ({len(pool)})")
            check(lb.effective_transfers(3) == min(LBM.TRANSFERS, LBM.TRANSFERS * 3), "effective transfers")
            await lb.release()
            check(not started and lb.client is None and not lb.extras, f"[{label}] every client stopped")
        # dedicated client cannot start -> shared session: WARNING for the panel, effective transfers from the real semaphore
        pyro.Client = Broken
        MT.mtproto.client = types.SimpleNamespace(get_file_semaphore=asyncio.Semaphore(1))
        lb = LBClient()
        c = await lb.acquire()
        check(c is MT.mtproto.client and not lb.dedicated and lb.eff == 1, f"shared fallback eff={lb.eff}")
        check(any("SHARED" in w for w in lb.warnings), f"shared fallback warns: {lb.warnings}")
        check(lb.effective_transfers(1) == 1 and (await lb.pool(c, 3)) == [c], "shared session: 1 transfer, no extra clients")
        await lb.release()
        check(lb.warnings == [] and lb.mode == "", "state reset after release")
        MT.mtproto.client = None
        lb = LBClient()
        try:
            await lb.acquire()
            check(False, "no MTProto at all must raise")
        except RuntimeError:
            pass
    finally:
        if keep[0] is None:
            del pyro.Client
        else:
            pyro.Client = keep[0]
        MT.mtproto.client = keep[1]


unit()
asyncio.run(scenario())
asyncio.run(parallel())
asyncio.run(client_diag())
shutil.rmtree(TMP, ignore_errors=True)
print("FAILED:\n" + "\n".join(fails) if fails else "ALL LOCAL BACKUP TESTS PASSED")
sys.exit(1 if fails else 0)
