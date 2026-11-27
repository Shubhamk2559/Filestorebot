"""
utils/zipvault.py
Vault Pack - ZIP upload. Everything here only runs for Vault Pack channels (post type 3).

  You send one or more ZIPs. Each ZIP holds folders, each folder holds images + videos:

      pack.zip
        Wedding/  img1.jpg img2.jpg ... clip1.mp4 clip2.mp4
        Puja/     ...

  run_job      download ZIP -> extract -> find folders -> queue ONE resumable job per folder (collection ap_batches, src="zip")
  process      worker step of such a job (called from utils/batch.py):
                 videos / files -> Database Channel -> ONE file-store link per folder
                 images         -> imgbb (ORIGINAL bytes, no re-encoding, no resizing) -> Telegraph article(s)
                 then status "need_thumb": the folder waits until YOU choose its thumbnail(s)
  make_posts   thumbnails of a folder -> posts (1 thumbnail = 1 post; none = the channel's fixed thumbnail)

Progress is saved after every file, so a restart / empty imgbb keys only pause the work and it continues.
"""
import asyncio
import html
import json
import logging
import os
import random
import shutil
import time
from pathlib import Path

import aiohttp
from pymongo.errors import DuplicateKeyError
from telegram import InlineKeyboardMarkup as M
from telegram.constants import ParseMode

from database import db
from database.db import settings as S
from utils import autoposter as AP
from utils import batch as BT
from utils import zipscan as ZS
from utils.helpers import call
from utils.keyboards import ub
from utils.mtproto import mtproto
from utils.ui import styled

log = logging.getLogger(__name__)

BOT_API_MAX_SEND = 50 * 1024 * 1024        # Bot API upload limit (only used when the MTProto session is off)
BOT_API_MAX_GET = 20 * 1024 * 1024         # Bot API download limit
MAX_IMG_TRIES = 3                          # an image that keeps failing (not a key problem) is skipped after this
ACTIVE = ["pending", "working", "waiting"]


class Lost(RuntimeError):
    """The extracted files of a folder are gone (container was recreated): the ZIP has to be sent again."""


# ------------------------------------------------------------------ ZIP -> queued folders
def _size(n: int) -> str:
    return f"{n / 2**20:.0f} MB" if n >= 2**20 else f"{n / 1024:.0f} KB"


async def _download(bot, z: dict, dest: str, progress):
    """ZIP of the admin's chat -> disk. MTProto handles big ZIPs (up to 2 GB), the Bot API only up to 20 MB."""
    if mtproto.ready:
        msg = await mtproto.client.get_messages(z["chat"], z["mid"])
        if not msg or getattr(msg, "empty", False):
            raise RuntimeError("the ZIP message could not be read again - send it once more")
        await mtproto.client.download_media(msg, file_name=dest, progress=progress)
    else:
        if z.get("size", 0) > BOT_API_MAX_GET:
            raise RuntimeError("ZIP is over 20 MB and the MTProto session is off (set API_ID + API_HASH)")
        f = await bot.get_file(z["fid"])
        await f.download_to_drive(dest)
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise RuntimeError("download failed")


async def queue_zip_folders(cid: int, folders: list, by: int, root: str, zip_name: str) -> int:
    """One resumable job per folder that has at least one image. Nothing is uploaded here."""
    n = 0
    t0 = time.time()
    for i, f in enumerate(folders):
        if not f["images"]:
            continue
        await db.ap_batches_col.insert_one({
            "_id": db.new_token(), "chan": cid, "name": f["name"], "by": by, "status": "pending", "src": "zip",
            "dir": root, "zip": zip_name,
            "imgs": [{"rel": r} for r in f["images"]], "vids": [{"rel": r} for r in f["videos"]],
            "items": [], "img_urls": {}, "img_failed": [], "img_tries": {}, "stored": [], "vtoken": None,
            "tg_url": None, "next_try": 0, "error": "", "created": t0 + i * 0.001})
        n += 1
    return n


async def run_job(bot, cid: int, by: int, zips: list, chat_id: int, msg_id: int):
    """Download + extract + scan every ZIP, queue the folders. The heavy part (imgbb / videos) is done by the worker."""
    last = [0.0]
    lines: list = []
    errors: list = []
    queued = skipped_nophoto = ignored = 0

    async def say(text: str, force: bool = False, final: bool = False):
        if not force and time.time() - last[0] < 3:
            return
        last[0] = time.time()
        try:
            await bot.edit_message_text(styled(text), chat_id=chat_id, message_id=msg_id,
                                        reply_markup=_job_kb(cid) if final else None, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    wd = ZS.work_dir()
    for zi, z in enumerate(zips, 1):
        tag = f"{zi}/{len(zips)}"
        root = os.path.join(wd, db.new_token())
        zpath = root + ".zip"
        try:
            await say(f"📦 <b>ZIP UPLOAD</b>\n\n⬇️ DOWNLOADING {tag}: {html.escape(z['name'])} ({_size(z['size'])})", True)

            async def prog(cur, tot, _t=tag, _z=z):
                pct = int(cur * 100 / tot) if tot else 0
                await say(f"📦 <b>ZIP UPLOAD</b>\n\n⬇️ DOWNLOADING {_t}: {html.escape(_z['name'])} — {pct}%")

            await _download(bot, z, zpath, prog)
            await say(f"📦 <b>ZIP UPLOAD</b>\n\n📂 UNPACKING {tag}: {html.escape(z['name'])}", True)
            count = await asyncio.to_thread(ZS.extract, zpath, root)
            loose = Path(z["name"]).stem or "Folder"        # loose files in the ZIP root -> a folder named like the ZIP
            folders = await asyncio.to_thread(ZS.scan_dir, root, loose)
            ok = await queue_zip_folders(cid, folders, by, root, z["name"])
            skipped_nophoto += sum(1 for f in folders if not f["images"])
            ignored += sum(f["ignored"] for f in folders)
            queued += ok
            for f in folders:
                if f["images"]:
                    lines.append(f"• {html.escape(f['name'][:30])} — 🖼 {len(f['images'])} · 🎬 {len(f['videos'])}")
            if not ok:
                shutil.rmtree(root, ignore_errors=True)
            log.info("ZIP %s: %s files, %s folders queued", z["name"], count, ok)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("ZIP %s failed: %s: %s", z.get("name"), type(e).__name__, e)
            errors.append(f"{html.escape(z['name'][:30])}: {html.escape(str(e)[:120])}")
            shutil.rmtree(root, ignore_errors=True)
        finally:
            try:
                os.remove(zpath)                      # the ZIP itself is not needed any more -> disk space back
            except OSError:
                pass

    if queued:
        text = (f"✅ <b>ZIP UPLOAD</b>\n\n<b>{queued}</b> FOLDER(S) QUEUED.\n"
                "THE BOT NOW UPLOADS THE IMAGES (imgbb → TELEGRAPH) AND THE VIDEOS (ONE LINK PER FOLDER).\n"
                "WHEN THE LINKS ARE READY YOU GET A MESSAGE WITH A 🖼 THUMBNAILS BUTTON.\n\n"
                + "\n".join(lines[:12]) + (f"\n… +{len(lines) - 12} MORE" if len(lines) > 12 else ""))
    else:
        text = "⚠️ <b>ZIP UPLOAD</b>\n\nNO FOLDER WITH IMAGES FOUND."
    if skipped_nophoto:
        text += f"\n⏭ SKIPPED (NO IMAGE): {skipped_nophoto}"
    if ignored:
        text += f"\n⏭ IGNORED FILES (NOT IMAGE / VIDEO): {ignored}"
    for e in errors:
        text += f"\n⚠️ {e}"
    await say(text, True, True)


def _job_kb(cid: int):
    return M([[ub("🖼 Thumbnails", f"adm:zvthumb:{cid}")], [ub("🔙 Back", f"adm:apnew:{cid}")]])


# ------------------------------------------------------------------ worker step (called from BT._process)
async def store_video(bot, channel_id: int, path: str, name: str, code: str, by: int) -> int:
    """A video / file from disk -> Database Channel under a backup code. Returns the channel message id."""
    ext = Path(path).suffix.lower()
    kind = "video" if ext in ZS.STREAMABLE else "document"
    cap = f"<code>{code}</code>"
    meta = await asyncio.to_thread(ZS.probe, path) if kind == "video" else {}
    thumb = meta.pop("thumb", None)
    try:
        if mtproto.ready:
            from pyrogram import enums
            c = mtproto.client
            await c.get_chat(channel_id)                      # make sure the channel peer is known to this session
            if kind == "video":
                m = await c.send_video(channel_id, path, caption=cap, parse_mode=enums.ParseMode.HTML,
                                       supports_streaming=True, file_name=name, thumb=thumb, **meta)
            else:
                m = await c.send_document(channel_id, path, caption=cap, parse_mode=enums.ParseMode.HTML,
                                          file_name=name, force_document=True)
            mid = m.id
        else:
            if os.path.getsize(path) > BOT_API_MAX_SEND:
                raise RuntimeError(f"{name} is over 50 MB and the MTProto session is off (set API_ID + API_HASH)")
            with open(path, "rb") as fh:
                if kind == "video":
                    m = await call(bot.send_video, channel_id, fh, caption=cap, parse_mode=ParseMode.HTML,
                                   supports_streaming=True, filename=name, **meta)
                else:
                    m = await call(bot.send_document, channel_id, fh, caption=cap, parse_mode=ParseMode.HTML,
                                   filename=name)
            mid = m.message_id
    finally:
        if thumb:
            try:
                os.remove(thumb)
            except OSError:
                pass
    await db.add_file(mid, kind, name, by, code=code, cap="")
    return mid


def _rm(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


async def _cleanup(b: dict):
    """Remove the job directory once no folder of it needs the files any more."""
    root = b.get("dir")
    if not root:
        return
    if not await db.ap_batches_col.count_documents({"dir": root, "status": {"$in": ACTIVE}}):
        shutil.rmtree(root, ignore_errors=True)


async def _telegraph(session, title: str, urls: list) -> str:
    """All images -> Telegraph. Normal folders = ONE article. A huge folder is split in linked parts
    (each part ends with a 'next part' link); the link of the FIRST part is returned."""
    chunks = ZS.chunk_nodes(urls)
    token = await BT.PV._telegraph_token(session)
    nxt = None
    for i in range(len(chunks) - 1, -1, -1):
        nodes = list(chunks[i])
        if nxt:
            nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": nxt}, "children": ["➡️ Next part"]}]})
        ttl = title if len(chunks) == 1 else f"{title} ({i + 1}/{len(chunks)})"
        async with session.post(f"{BT.TELEGRAPH}/createPage", data={
                "access_token": token, "title": ttl[:256], "content": json.dumps(nodes),
                "return_content": "false"}) as r:
            js = await r.json(content_type=None)
        if not js.get("ok"):
            raise RuntimeError(f"telegraph: {js.get('error')}")
        nxt = js["result"]["url"]
        if i:
            await asyncio.sleep(2)
    return nxt


async def process(bot, b: dict):
    """Idempotent: every step checks what is already saved, so a restart continues where it stopped."""
    channel = S.get("db_channel")
    if not channel:
        raise RuntimeError("Database Channel is not set")
    bid, cid, by = b["_id"], b["chan"], b["by"]
    root = b.get("dir") or ""
    ch = await db.ap_get_channel(cid) or {}
    vids, imgs = b.get("vids") or [], b.get("imgs") or []
    try:
        # 1) videos / files -> ONE file-store link for the folder
        vtoken = b.get("vtoken")
        if vids and not vtoken:
            vtoken = db.new_token()
            await BT._set(bid, vtoken=vtoken)
        stored = list(b.get("stored") or [])
        if vids and not b.get("link_done"):
            for i, v in enumerate(vids):
                if i < len(stored):
                    continue
                path = os.path.join(root, v["rel"])
                if not os.path.exists(path):
                    raise Lost(f"file missing: {v['rel']}")
                stored.append(await store_video(bot, channel["id"], path, os.path.basename(v["rel"]),
                                                db.make_code(vtoken, i), by))
                await BT._set(bid, stored=stored, status="working")
                _rm(path)
            try:
                await db.create_link(stored, by, (b["name"] or "Folder")[:40] + " (videos)", token=vtoken, chan=cid)
            except DuplicateKeyError:
                pass                                           # a crash happened right after the link was created
            await BT._set(bid, link_done=True)

        # 2) images -> imgbb (original bytes, slow random cooldown, key rotation) -> Telegraph
        urls = dict(b.get("img_urls") or {})
        failed = list(b.get("img_failed") or [])
        tries = dict(b.get("img_tries") or {})
        bad = {f["i"] for f in failed}
        async with aiohttp.ClientSession(timeout=BT._TIMEOUT) as session:
            for i, im in enumerate(imgs):
                k = str(i)
                if k in urls or i in bad:
                    continue
                path = os.path.join(root, im["rel"])
                if not os.path.exists(path):
                    raise Lost(f"file missing: {im['rel']}")
                if os.path.getsize(path) > ZS.IMGBB_MAX:
                    failed.append({"i": i, "name": os.path.basename(im["rel"]), "why": "over 32 MB (imgbb limit)"})
                    bad.add(i)
                    await BT._set(bid, img_failed=failed)
                    continue
                data = await asyncio.to_thread(Path(path).read_bytes)      # ORIGINAL bytes, never re-encoded
                try:
                    urls[k] = await BT.upload(session, data)
                except BT.NoKeyAvailable as e:
                    msg = str(e)
                    if "resting" in msg or "no imgbb API key" in msg:
                        raise                                              # key problem: pause, retry later
                    tries[k] = tries.get(k, 0) + 1
                    if tries[k] < MAX_IMG_TRIES:
                        await BT._set(bid, img_tries=tries)
                        raise
                    failed.append({"i": i, "name": os.path.basename(im["rel"]), "why": msg[:80]})
                    bad.add(i)
                    await BT._set(bid, img_failed=failed)
                    continue
                await BT._set(bid, img_urls=urls, status="working")
                _rm(path)
                await asyncio.sleep(random.uniform(BT.CD_MIN, BT.CD_MAX))

            ordered = [urls[str(i)] for i in range(len(imgs)) if str(i) in urls]
            if not ordered:
                raise Lost("no image could be uploaded to imgbb")
            tg = b.get("tg_url")
            if not tg:
                tg = await _telegraph(session, BT.render_title(ch.get("tg_title"), b["name"]), ordered)
                await BT._set(bid, tg_url=tg)
    except Lost as e:
        log.warning("ZIP folder %s failed: %s", b.get("name"), e)
        await BT._set(bid, status="failed", error=str(e)[:300])
        await _notify(bot, b, failed_name=b.get("name"), why=str(e))
        await _cleanup(b)
        return

    await BT._set(bid, status="need_thumb", error="")
    await _cleanup(b)
    await _notify(bot, b)


async def _notify(bot, b: dict, failed_name=None, why: str = ""):
    """When the last folder of the queue is ready: tell the admin and offer the thumbnail button."""
    cid, by = b["chan"], b["by"]
    if await db.ap_batches_col.count_documents({"chan": cid, "src": "zip", "status": {"$in": ACTIVE}}):
        if failed_name:
            try:
                await bot.send_message(by, f"⚠️ <b>VAULT PACK</b>: “{html.escape(failed_name)}” FAILED\n"
                                           f"<code>{html.escape(why[:200])}</code>\nSEND ITS ZIP AGAIN.")
            except Exception:
                pass
        return
    n = await waiting_count(cid)
    text = f"✅ <b>VAULT PACK</b>\n\n<b>{n}</b> FOLDER(S) HAVE THEIR LINKS (TELEGRAPH + VIDEO).\nNEXT: CHOOSE THE THUMBNAILS."
    if failed_name:
        text += f"\n⚠️ “{html.escape(failed_name)}” FAILED: <code>{html.escape(why[:200])}</code>\nSEND ITS ZIP AGAIN."
    try:
        await bot.send_message(by, styled(text), reply_markup=M([[ub("🖼 Thumbnails", f"adm:zvthumb:{cid}")]]))
    except Exception:
        log.warning("could not notify admin %s", by)


# ------------------------------------------------------------------ thumbnails -> posts
async def waiting_count(cid: int) -> int:
    return await db.ap_batches_col.count_documents({"chan": cid, "src": "zip", "status": "need_thumb"})


async def preparing_count(cid: int) -> int:
    return await db.ap_batches_col.count_documents({"chan": cid, "src": "zip", "status": {"$in": ACTIVE}})


async def next_waiting(cid: int, skip: list):
    return await db.ap_batches_col.find_one(
        {"chan": cid, "src": "zip", "status": "need_thumb", "_id": {"$nin": list(skip)}}, sort=[("created", 1)])


async def store_thumb(bot, channel_id: int, it: dict, code: str, by: int):
    """A thumbnail the admin sent -> Database Channel (so the post can always be rebuilt). Always stored as a PHOTO."""
    if it["kind"] == "photo":
        await AP._store(bot, channel_id, it, code, by)
        return
    f = await bot.get_file(it["fid"])                      # an image sent as a file -> posted as a photo
    data = bytes(await f.download_as_bytearray())
    m = await call(bot.send_photo, channel_id, data, caption=AP._caption_with_code(it, code),
                   parse_mode=ParseMode.HTML)
    await db.add_file(m.message_id, "photo", it["name"], by, code=code, cap=it["cap_html"])


async def make_posts(bot, b: dict, thumbs: list, by: int, fixed: bool = False) -> int:
    """Create the posts of one folder: every thumbnail = its own post (same Telegraph + video link),
    or ONE post with the fixed thumbnail. Returns the number of posts (0 = already done / nothing to do)."""
    cid, bid = b["chan"], b["_id"]
    r = await db.ap_batches_col.update_one({"_id": bid, "status": "need_thumb"}, {"$set": {"status": "finishing"}})
    if not r.modified_count:
        return 0                                           # double tap / already finished
    try:
        channel = S.get("db_channel")
        if not channel:
            raise RuntimeError("Database Channel is not set")
        ch = await db.ap_get_channel(cid) or {}
        if fixed and not ch.get("fthumb"):
            raise RuntimeError("fixed thumbnail not set (Caption → Thumbnail)")
        name = b["name"] or "Folder"
        n_files = len(b.get("vids") or [])
        tg, vtoken = b.get("tg_url") or "", b.get("vtoken") or ""
        tokens = []                                        # (thumb_token | None, caption html, title)
        if fixed:
            tokens.append((None, "", name[:40]))
        else:
            for i, t in enumerate(thumbs):                 # store ALL thumbnails first, so a failure leaves no half posts
                tt = db.new_token()
                await store_thumb(bot, channel["id"], t, db.make_code(tt, 0), by)
                plain = " ".join((t.get("cap") or "").split())[:40]
                title = plain or (name[:34] + (f" #{i + 1}" if len(thumbs) > 1 else ""))
                tokens.append((tt, t.get("cap_html") or "", title))
        seq = await db.ap_last_seq(cid)
        new = []
        for tt, cap, title in tokens:
            seq += 1
            tok = db.new_token()
            extra = {"fixed_thumb": True} if tt is None else {}
            await db.ap_add_post(cid, tok, tt, seq, title, cap, "photo", n_files, by, ptype="batch", batch_id=bid,
                                 batch_name=name, tg_url=tg, vtoken=vtoken, not_before=0, **extra)
            new.append(tok)
        await db.ap_enqueue_new(cid, new)
        await BT._set(bid, status="done", posts_made=True, error="")
        return len(new)
    except Exception:
        await BT._set(bid, status="need_thumb")
        raise
