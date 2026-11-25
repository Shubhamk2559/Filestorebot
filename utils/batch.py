"""
utils/batch.py
Post type 3 - "Folder Batch" (ptype = "batch"). Everything here only runs for channels whose Post Type is Folder Batch;
Classic and Image + Video channels never touch this module.

Source channel layout (range mode, first + last message link):

    file, file, file ... "Wedding Uploaded ✅"      <- a text message containing "Uploaded" closes the folder
    file, file ...       "Puja Uploaded ✅"

  * Every file ABOVE an "Uploaded" text (back to the previous one) = one folder / batch.
  * Photos  -> imgbb (cooldown between uploads, several API keys in rotation) -> ONE Telegraph article.
  * Other files (videos ...) -> ONE file-store link.
  * Every photo that has a caption = the thumbnail of its OWN post. All posts of a folder share the same
    Telegraph link + video link. No captioned photo -> one post with the first photo.

  scan_folders        messages -> folders
  keys / upload       imgbb API keys, rotation, cooldown
  worker_loop         resumable background worker (progress is saved in MongoDB after every step)
  apply_tags          {Preview Image} / {Video Link} / {Post Number} in the caption (only for batch channels)
  pick_post           which post may go out now (30+ day gap inside a folder, min. hours between any two posts)
  after_posted        pushes the not-before date of the other posts of the folder
"""
import asyncio
import base64
import html
import json
import logging
import random
import re
import time

import aiohttp

from database import db
from database.db import settings as S
from utils.helpers import notify_owner
from utils import preview as PV

log = logging.getLogger(__name__)

IMGBB_URL = PV.IMGBB_URL
TELEGRAPH = PV.TELEGRAPH
_TIMEOUT = aiohttp.ClientTimeout(total=90)

CD_MIN, CD_MAX = 5, 15              # seconds between two imgbb uploads (random)
KEY_COOLDOWN = 30 * 60              # a key that failed rests this long
RETRY_WAIT = 10 * 60                # a batch that could not go on (all keys down ...) is retried after this
DEFAULT_GAP_DAYS = 30               # min. days between two posts of the SAME folder
GAP_JITTER_DAYS = 8                 # + random 0..8 days, so the pattern does not show
DEFAULT_MIN_HOURS = 3               # min. hours between any two posts of the channel
DEFAULT_TG_TITLE = "{folder_name}"

UPLOADED_RE = re.compile(r"\buploaded\b", re.I)


# ------------------------------------------------------------------ scanning
def folder_name(text: str) -> str:
    """'Wedding Uploaded ✅' -> 'Wedding'"""
    t = UPLOADED_RE.split(text or "", 1)[0]
    t = re.sub(r"[✅✔☑️\s]+$", "", t).strip(" -:|_")
    return re.sub(r"\s+", " ", t).strip() or "Folder"


def item_or_marker(m):
    """A pyrogram message -> {'marker': name} for an 'Uploaded' text, a file item, or None."""
    from utils import autoposter as AP
    if m is None or getattr(m, "empty", False):
        return None
    if not getattr(m, "media", None):
        txt = (m.text or "").strip() if getattr(m, "text", None) else ""
        if txt and UPLOADED_RE.search(txt):
            return {"marker": folder_name(txt)}
        return None
    return AP.item_from_pyro(m)


def scan_folders(entries: list):
    """entries (message order) -> (folders, leftover_files).
    folder = {"name", "items": [file items]}; folders without any photo are dropped by the caller."""
    folders, cur, left = [], [], 0
    for e in entries:
        if "marker" in e:
            if cur:
                folders.append({"name": e["marker"], "items": cur})
            cur = []
        else:
            cur.append(e)
    left = len(cur)
    return folders, left


def plan_folder(items: list):
    """(photos, files, thumbs) of a folder. thumbs = the captioned photos (each = its own post).
    No captioned photo -> thumbs is empty and ONE post with the channel's fixed thumbnail is made."""
    photos = [i for i in items if i["kind"] == "photo"]
    files = [i for i in items if i["kind"] != "photo"]
    thumbs = [p for p in photos if p["cap"].strip()]
    return photos, files, thumbs


# ------------------------------------------------------------------ imgbb keys (rotation)
class NoKeyAvailable(RuntimeError):
    pass


_rest: dict = {}        # key -> time until which it rests
_rr = [0]               # round robin pointer


def keys() -> list:
    out = list(S.get("imgbb_keys") or [])
    old = (S.get("imgbb_key") or "").strip()      # the single key of the older version still counts
    if old and old not in out:
        out.append(old)
    return out


def enabled() -> bool:
    return bool(keys())


async def _save_keys(ks: list):
    await S.set("imgbb_keys", ks)
    if (S.get("imgbb_key") or "").strip():
        await S.set("imgbb_key", "")          # the older single key now lives in the list


async def add_key(k: str):
    ks = keys()
    if k not in ks:
        ks.append(k)
    await _save_keys(ks)


async def remove_key(i: int):
    ks = keys()
    if 0 <= i < len(ks):
        _rest.pop(ks.pop(i), None)
    await _save_keys(ks)


def mask(k: str) -> str:
    return f"{k[:4]}…{k[-4:]}" if len(k) > 10 else "…"


def key_state(k: str) -> str:
    left = _rest.get(k, 0) - time.time()
    return f"😴 rests {int(left // 60) + 1} min" if left > 0 else "✅ ready"


async def upload(session: aiohttp.ClientSession, data: bytes) -> str:
    """imgbb upload with key rotation. A failing key rests 30 min and the next key is tried."""
    ks = keys()
    if not ks:
        raise NoKeyAvailable("no imgbb API key set")
    last = ""
    for n in range(len(ks)):
        k = ks[(_rr[0] + n) % len(ks)]
        if _rest.get(k, 0) > time.time():
            continue
        for attempt in range(2):                          # one quick retry for a network hiccup
            try:
                form = aiohttp.FormData()
                form.add_field("key", k)
                form.add_field("image", base64.b64encode(data).decode())
                async with session.post(IMGBB_URL, data=form) as r:
                    js = await r.json(content_type=None)
                if js.get("success"):
                    _rr[0] = (_rr[0] + n) % len(ks)
                    return js["data"]["url"]                    # direct link (i.ibb.co/...)
                last = f"{mask(k)}: {(js.get('error') or {}).get('message', js)}"
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = f"{mask(k)}: {type(e).__name__}"
                await asyncio.sleep(3)
        _rest[k] = time.time() + KEY_COOLDOWN
        log.warning("imgbb key %s rests: %s", mask(k), last)
    raise NoKeyAvailable(last or "every imgbb key is resting")


# ------------------------------------------------------------------ Telegraph
def render_title(tpl: str, name: str) -> str:
    t = (tpl or DEFAULT_TG_TITLE).replace("{folder_name}", name).replace("{Folder Name}", name)
    return (t.strip() or name or "Preview")[:256]


async def telegraph_page(session: aiohttp.ClientSession, title: str, urls: list) -> str:
    token = await PV._telegraph_token(session)
    content = [{"tag": "img", "attrs": {"src": u}} for u in urls]
    body = json.dumps(content)
    if len(body.encode()) > 60000:
        raise RuntimeError(f"Telegraph article too big ({len(urls)} images)")
    async with session.post(f"{TELEGRAPH}/createPage", data={
            "access_token": token, "title": title, "content": body, "return_content": "false"}) as r:
        js = await r.json(content_type=None)
    if not js.get("ok"):
        raise RuntimeError(f"telegraph: {js.get('error')}")
    return js["result"]["url"]


# ------------------------------------------------------------------ creating the batches
async def queue_folders(cid: int, folders: list, by: int) -> int:
    """Save every folder as a resumable job. Returns how many were saved. Nothing is uploaded here."""
    n = 0
    for f in folders:
        photos, files, thumbs = plan_folder(f["items"])
        if not photos:
            continue
        await db.ap_batches_col.insert_one({
            "_id": db.new_token(), "chan": cid, "name": f["name"], "by": by, "status": "pending",
            "items": f["items"], "img_urls": {}, "thumbs": {}, "stored": [], "vtoken": None,
            "tg_url": None, "next_try": 0, "error": "", "created": time.time()})
        n += 1
    return n


async def counts(cid: int):
    c = db.ap_batches_col
    return (await c.count_documents({"chan": cid, "status": {"$ne": "done"}}),
            await c.count_documents({"chan": cid, "status": "done"}))


async def _set(bid: str, **f):
    await db.ap_batches_col.update_one({"_id": bid}, {"$set": f})


async def _process(bot, b: dict):
    """Idempotent: every step checks what is already saved, so a restart continues where it stopped."""
    from utils import autoposter as AP
    channel = S.get("db_channel")
    if not channel:
        raise RuntimeError("Database Channel is not set")
    bid, cid, by = b["_id"], b["chan"], b["by"]
    ch = await db.ap_get_channel(cid) or {}
    photos, files, thumbs = plan_folder(b["items"])

    # 1) videos / other files -> ONE file-store link
    vtoken = b.get("vtoken")
    if files and not vtoken:
        vtoken = db.new_token()
        await _set(bid, vtoken=vtoken)
    stored = list(b.get("stored") or [])
    if files and not b.get("link_done"):
        for i, f in enumerate(files):
            if i < len(stored):
                continue
            stored.append(await AP._store(bot, channel["id"], f, db.make_code(vtoken, i), by))
            await _set(bid, stored=stored)
        await db.create_link(stored, by, (b["name"] or "Folder")[:40] + " (videos)", token=vtoken)
        await _set(bid, link_done=True)

    # 2) thumbnails (captioned photos) -> saved in the Database Channel, so the post can always be rebuilt
    tmap = dict(b.get("thumbs") or {})
    for t in thumbs:
        k = str(t["mid"])
        if k not in tmap:
            tok = db.new_token()
            await AP._store(bot, channel["id"], t, db.make_code(tok, 0), by)
            tmap[k] = tok
            await _set(bid, thumbs=tmap)

    # 3) every photo -> imgbb (slowly, random cooldown)
    urls = dict(b.get("img_urls") or {})
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for p in photos:
            k = str(p["mid"])
            if k in urls:
                continue
            data = await PV._download(p["chat"], p["mid"])
            urls[k] = await upload(session, data)
            await _set(bid, img_urls=urls, status="working")
            await asyncio.sleep(random.uniform(CD_MIN, CD_MAX))

        # 4) ONE Telegraph article with all the photos
        tg = b.get("tg_url")
        if not tg:
            title = render_title(ch.get("tg_title"), b["name"])
            tg = await telegraph_page(session, title, [urls[str(p["mid"])] for p in photos])
            await _set(bid, tg_url=tg)

    # 5) one post per thumbnail, all with the same Telegraph + video link
    if not b.get("posts_made"):
        seq = await db.ap_last_seq(cid)
        new = []
        for t in thumbs:
            seq += 1
            tok = db.new_token()
            title = re.sub(r"\s+", " ", t["cap"]).strip()[:40] or b["name"][:40] or "Folder"
            await db.ap_add_post(cid, tok, tmap[str(t["mid"])], seq, title, t["cap_html"], "photo",
                                 len(files), by, ptype="batch", batch_id=bid, batch_name=b["name"],
                                 tg_url=tg, vtoken=vtoken or "", not_before=0)
            new.append(tok)
        if not thumbs:                         # no captioned photo -> one post with the fixed thumbnail
            seq += 1
            tok = db.new_token()
            await db.ap_add_post(cid, tok, None, seq, (b["name"] or "Folder")[:40], "", "photo",
                                 len(files), by, ptype="batch", batch_id=bid, batch_name=b["name"],
                                 tg_url=tg, vtoken=vtoken or "", not_before=0, fixed_thumb=True)
            new.append(tok)
        await db.ap_enqueue_new(cid, new)
        await _set(bid, posts_made=True)
    await _set(bid, status="done", error="", items=[])      # the heavy item list is not needed any more


async def worker_loop(bot):
    """Background worker: one folder at a time, oldest first. Survives restarts and key outages."""
    await asyncio.sleep(15)
    while True:
        try:
            b = await db.ap_batches_col.find_one(
                {"status": {"$in": ["pending", "working", "waiting"]}, "next_try": {"$lte": time.time()}},
                sort=[("created", 1)])
            if not b:
                await asyncio.sleep(20)
                continue
            try:
                await _process(bot, b)
                if not await db.ap_batches_col.count_documents({"chan": b["chan"], "status": {"$ne": "done"}}):
                    await notify_owner(bot, f"batch_done_{b['chan']}",
                                       "✅ <b>Folder Batch</b>: every folder is processed, the posts are in the queue.",
                                       every=60)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Batch %s paused: %s: %s", b["_id"], type(e).__name__, e)
                await _set(b["_id"], status="waiting", error=str(e)[:300], next_try=time.time() + RETRY_WAIT)
                await notify_owner(bot, f"batch_err_{b['chan']}",
                                   f"⚠️ <b>Folder Batch</b> paused on “{html.escape(b.get('name', ''))}”\n"
                                   f"<code>{html.escape(str(e)[:300])}</code>\n"
                                   "It continues by itself in 10 minutes (add an imgbb key if all keys are used up).",
                                   every=3600)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Batch worker error")
            await asyncio.sleep(30)


# ------------------------------------------------------------------ caption tags (batch channels only)
_TAGS = (
    (re.compile(r"\{\s*preview\s*(?:image|link)\s*\}", re.I), "tg"),
    (re.compile(r"\{\s*video\s*(?:link)?\s*\}", re.I), "video"),
    (re.compile(r"\{\s*post\s*(?:number|no)\s*\}", re.I), "no"),
)


def apply_tags(text: str, tg: str, video: str, number) -> str:
    vals = {"tg": tg or "", "video": video or "", "no": str(number)}
    for rx, key in _TAGS:
        text = rx.sub(lambda _m, v=vals[key]: v, text)
    return text


# ------------------------------------------------------------------ scheduling rules
def settings_of(ch: dict):
    return (int(ch.get("gap_days") or DEFAULT_GAP_DAYS),
            float(ch.get("min_hours") if ch.get("min_hours") is not None else DEFAULT_MIN_HOURS))


async def pick_post(cid: int, ch: dict):
    """The post that may go out now, or (None, reason). reason: 'empty' | 'wait'."""
    now = time.time()
    _, min_h = settings_of(ch)
    last = float(ch.get("last_batch_post") or 0)
    if now - last < min_h * 3600:
        return None, "wait"
    pend = await db.ap_posts_col.find({"chan": cid, "posted": False, "ptype": "batch"}).to_list(None)
    if not pend:
        return None, "empty"
    ready = [p for p in pend if float(p.get("not_before") or 0) <= now]
    if not ready:
        return None, "wait"
    return random.choice(ready), ""


async def after_posted(cid: int, post: dict, number: int):
    """Posted: remember the number, and push every other post of this folder at least gap days (+ jitter) away."""
    now = time.time()
    ch = await db.ap_get_channel(cid) or {}
    gap, _ = settings_of(ch)
    await db.ap_posts_col.update_one({"_id": post["_id"]},
                                     {"$set": {"posted": True, "posted_at": now, "posted_no": number}})
    upd = {"$set": {"last_batch_post": now, "queue_state.last_posted_at": now,
                    "queue_state.last_post_id": post["_id"]}}
    if number is not None:                       # the counter only moves for batch channels (tags are on)
        upd["$max"] = {"post_no": number}
    await db.ap_channels_col.update_one({"_id": cid}, upd)
    for sib in await db.ap_posts_col.find({"batch_id": post.get("batch_id"), "posted": False,
                                           "_id": {"$ne": post["_id"]}}, {"_id": 1}).to_list(None):
        nb = now + (gap + random.uniform(0, GAP_JITTER_DAYS)) * 86400
        await db.ap_posts_col.update_one({"_id": sib["_id"]}, {"$max": {"not_before": nb}})


async def restart_cycle(cid: int, ch: dict):
    """Loop mode: every post was posted -> all pending again, but nothing repeats before the gap is over."""
    gap, _ = settings_of(ch)
    await db.ap_posts_col.update_many(
        {"chan": cid, "ptype": "batch"},
        {"$set": {"posted": False, "posted_at": None, "not_before": time.time() + gap * 86400}})
