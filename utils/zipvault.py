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
                 folder with ONLY videos (no image): 10 random lossless frames (faces preferred) are captured
                 from the videos first (utils/framegrab.py) and used as its images; its post is then created
                 automatically with the channel's FIXED thumbnail
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
from utils import framegrab as FG
from utils import progress as P
from utils import zipscan as ZS
from utils.helpers import call
from utils.keyboards import ub
from utils.mtproto import mtproto
from utils.ui import styled

log = logging.getLogger(__name__)

BOT_API_MAX_SEND = 50 * 1024 * 1024        # Bot API upload limit (only used when the MTProto session is off)
BOT_API_MAX_GET = 20 * 1024 * 1024         # Bot API download limit
IMG_PARALLEL = max(1, int(os.getenv("IMG_PARALLEL", "4") or 4))   # imgbb uploads running at the same time
MAX_IMG_TRIES = 3                          # an image that keeps failing (not a key problem) is skipped after this
ACTIVE = ["pending", "working", "waiting"]
LIVE: dict = {}                            # channel id -> what the worker is doing right now (shown in the status message)


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


async def queue_zip_folders(cid: int, folders: list, by: int, root: str, zip_name: str) -> list:
    """One resumable job per folder that has at least one image OR video. A folder with videos only gets
    "frames": True (10 frames are captured from its videos by the worker). Nothing is uploaded here."""
    ids = []
    t0 = time.time()
    for i, f in enumerate(folders):
        if not f["images"] and not f["videos"]:
            continue
        bid = db.new_token()
        await db.ap_batches_col.insert_one({
            "_id": bid, "chan": cid, "name": f["name"], "by": by, "status": "pending", "src": "zip",
            "dir": root, "zip": zip_name, "frames": not f["images"], "frames_done": False,
            "imgs": [{"rel": r} for r in f["images"]], "vids": [{"rel": r} for r in f["videos"]],
            "items": [], "img_urls": {}, "img_failed": [], "img_tries": {}, "stored": [], "vtoken": None,
            "tg_url": None, "next_try": 0, "error": "", "created": t0 + i * 0.001})
        ids.append(bid)
    return ids


def _stop_kb(job_id: str):
    return M([[ub("🛑 Stop", f"adm:zvcancel:{job_id}")]])


def _job_kb(cid: int):
    return M([[ub("🖼 Thumbnails", f"adm:zvthumb:{cid}")], [ub("🔙 Back", f"adm:apnew:{cid}")]])


# ------------------------------------------------------------------ live status of the worker
async def work_text(cid: int, ids: list) -> str:
    total = len(ids)
    active = await db.ap_batches_col.count_documents({"_id": {"$in": ids}, "status": {"$in": ACTIVE}})
    head = f"⚙️ <b>ZIP UPLOAD · PROCESSING</b>\n\n📂 FOLDERS ▸ <b>{total - active}/{total}</b> DONE\n"
    live = LIVE.get(cid)
    if not live or live.get("bid") not in ids or time.time() - live.get("ts", 0) > 180:
        w = await db.ap_batches_col.find_one({"_id": {"$in": ids}, "status": "waiting"})
        if w:
            return (head + f"\n⏳ PAUSED ON “{html.escape(w['name'])}”. RETRY IN A FEW MINUTES.\n"
                    f"<code>{html.escape((w.get('error') or '')[:150])}</code>")
        return head + "\n⏳ WAITING FOR THE NEXT FOLDER…"
    t = head + (f"\n▶️ NOW ▸ <b>{html.escape(live['name'][:40])}</b>\n"
                f"🎬 VIDEOS ▸ <b>{live['v_done']}/{live['v_tot']}</b> · 🖼 IMAGES ▸ <b>{live['i_done']}/{live['i_tot']}</b>\n\n")
    st = live["stage"]
    if st == "frames":
        t += "🎞 CAPTURING 10 FRAMES FROM THE VIDEOS (FACES FIRST)…"
    elif st == "videos":
        t += f"🎬 UPLOADING VIDEO TO CHANNEL\n{live['file'][:40]}\n" + live["tr"].card()
    elif st == "images":
        it = live["itr"]
        sp = it.speed
        t += (f"🖼 UPLOADING IMAGES (imgbb)\n{P.bar(it.pct)} <b>{it.pct:.0f}%</b>\n"
              f"⚡ {sp:.1f} IMG/S · ⏱ ETA {P.clock(it.eta) if sp else '--'} · 🕐 {P.clock(it.elapsed)}")
    else:
        t += "📝 CREATING THE TELEGRAPH PAGE…"
    return t


async def cancel_job(job_id: str) -> str:
    """🛑 Stop: kills the download / unpack, cancels every queued folder of the job and removes its files."""
    job = P.JOBS.get(job_id)
    if not job:
        return "NOTHING IS RUNNING ANY MORE."
    job["cancelled"] = True
    cid = job["cid"]
    ids = list(job["ids"])
    if LIVE.get(cid) and LIVE[cid].get("bid") in ids:
        LIVE[cid]["cancel"] = True                     # the running upload stops at the next chunk
    if ids:
        dirs = {d["dir"] async for d in db.ap_batches_col.find({"_id": {"$in": ids}}, {"dir": 1}) if d.get("dir")}
        await db.ap_batches_col.update_many({"_id": {"$in": ids}, "status": {"$in": ACTIVE + ["need_thumb"]}},
                                            {"$set": {"status": "cancelled", "error": "stopped by admin"}})
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
    task = job.get("task")
    if task and not task.done():
        task.cancel()
    return f"🛑 STOPPED. {len(ids)} QUEUED FOLDER(S) CANCELLED, TEMP FILES REMOVED."


async def run_job(bot, cid: int, by: int, zips: list, chat_id: int, msg_id: int, job_id: str = ""):
    """Download + extract + scan every ZIP, queue the folders, then show the live status of the worker."""
    job = P.JOBS.setdefault(job_id, {"cid": cid, "by": by, "ids": [], "cancelled": False, "task": None})
    tr = P.Tracker()
    st = {"stage": "start", "tag": "", "z": None}
    lines: list = []
    errors: list = []
    queued = skipped_nophoto = ignored = auto_frames = 0
    t_start = time.time()

    def render() -> str:
        z = st["z"]
        head = "📦 <b>ZIP UPLOAD</b>\n\n"
        if not z:
            return head + "⏳ STARTING…"
        what = {"download": "⬇️ DOWNLOADING", "unpack": "📂 UNPACKING"}.get(st["stage"], "⏳")
        return (head + f"{what} {st['tag']}\n📄 {html.escape(z['name'][:40])} ({P.size(z['size'])})\n\n" + tr.card())

    async def edit(text: str, markup):
        try:
            await bot.edit_message_text(styled(text), chat_id=chat_id, message_id=msg_id,
                                        reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception:                               # "message is not modified", flood limits ...
            pass

    async def reporter():
        while True:
            await asyncio.sleep(2.5)
            text = await work_text(cid, job["ids"]) if st["stage"] == "work" else render()
            await edit(text, _stop_kb(job_id))

    rep = asyncio.ensure_future(reporter())
    wd = ZS.work_dir()
    try:
        for zi, z in enumerate(zips, 1):
            st.update(stage="download", tag=f"{zi}/{len(zips)}", z=z)
            tr.reset()
            tr.tot = z["size"]
            root = os.path.join(wd, db.new_token())
            zpath = root + ".zip"
            try:
                await edit(render(), _stop_kb(job_id))

                async def prog(cur, tot):                # cheap: only stores the numbers, the reporter shows them
                    tr.update(cur, tot)

                await _download(bot, z, zpath, prog)
                st["stage"] = "unpack"
                tr.reset()
                tr.tot = os.path.getsize(zpath)
                await edit(render(), _stop_kb(job_id))
                count = await asyncio.to_thread(ZS.extract, zpath, root, lambda d, t, n: tr.update(d, t))
                loose = Path(z["name"]).stem or "Folder"      # loose files in the ZIP root -> a folder named like the ZIP
                folders = await asyncio.to_thread(ZS.scan_dir, root, loose)
                ids = await queue_zip_folders(cid, folders, by, root, z["name"])
                job["ids"].extend(ids)
                skipped_nophoto += sum(1 for f in folders if not f["images"] and not f["videos"])
                auto_frames += sum(1 for f in folders if not f["images"] and f["videos"])
                ignored += sum(f["ignored"] for f in folders)
                queued += len(ids)
                for f in folders:
                    if f["images"]:
                        lines.append(f"• {html.escape(f['name'][:30])} — 🖼 {len(f['images'])} · 🎬 {len(f['videos'])}")
                    elif f["videos"]:
                        lines.append(f"• {html.escape(f['name'][:30])} — 🎞 {FG.COUNT} FRAMES · 🎬 {len(f['videos'])}")
                if not ids:
                    shutil.rmtree(root, ignore_errors=True)
                if ids:
                    st["stage"] = "work"                 # the worker starts on the first folder while the next ZIP downloads
                log.info("ZIP %s: %s files, %s folders queued", z["name"], count, len(ids))
            except asyncio.CancelledError:
                shutil.rmtree(root, ignore_errors=True)
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

        st["stage"] = "work" if queued else "end"
        while queued:                                     # follow the worker until every folder of this job is done
            left = await db.ap_batches_col.count_documents({"_id": {"$in": job["ids"]}, "status": {"$in": ACTIVE}})
            if not left:
                break
            await asyncio.sleep(3)
    except asyncio.CancelledError:
        rep.cancel()
        await edit("🛑 <b>ZIP UPLOAD</b>\n\nSTOPPED. TEMP FILES REMOVED.", M([[ub("🔙 Back", f"adm:apnew:{cid}")]]))
        raise
    finally:
        rep.cancel()
        P.JOBS.pop(job_id, None)

    took = P.clock(time.time() - t_start)
    if queued:
        c = db.ap_batches_col
        failed = await c.count_documents({"_id": {"$in": job["ids"]}, "status": "failed"})
        ready = queued - failed
        waiting = await waiting_count(cid)
        note = "TAP 🖼 THUMBNAILS TO CHOOSE THE THUMBNAIL OF EACH FOLDER." if waiting else "ALL POSTS ARE CREATED."
        text = (f"✅ <b>ZIP UPLOAD</b>\n\n<b>{ready}</b> OF {queued} FOLDER(S) READY ⏱ {took}.\n{note}\n\n"
                + "\n".join(lines[:12]) + (f"\n… +{len(lines) - 12} MORE" if len(lines) > 12 else ""))
        if failed:
            text += f"\n⚠️ FAILED: {failed} (SEND THEIR ZIP AGAIN)"
    else:
        text = "⚠️ <b>ZIP UPLOAD</b>\n\nNO FOLDER WITH IMAGES OR VIDEOS FOUND."
    if auto_frames and queued:
        text += f"\n🎞 VIDEO-ONLY FOLDERS: {auto_frames} (10 FRAMES EACH, FIXED THUMBNAIL)"
    if skipped_nophoto:
        text += f"\n⏭ SKIPPED (NO IMAGE / VIDEO): {skipped_nophoto}"
    if ignored:
        text += f"\n⏭ IGNORED FILES (NOT IMAGE / VIDEO): {ignored}"
    for e in errors:
        text += f"\n⚠️ {e}"
    await edit(text, _job_kb(cid))


# ------------------------------------------------------------------ worker step (called from BT._process)
async def store_video(bot, channel_id: int, path: str, name: str, code: str, by: int, progress=None) -> int:
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
                                       supports_streaming=True, file_name=name, thumb=thumb, progress=progress, **meta)
            else:
                m = await c.send_document(channel_id, path, caption=cap, parse_mode=enums.ParseMode.HTML,
                                          file_name=name, force_document=True, progress=progress)
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


async def _save(bid: str, **f):
    """Like BT._set, but never brings a folder back to life after the admin pressed 🛑 Stop."""
    await db.ap_batches_col.update_one({"_id": bid, "status": {"$ne": "cancelled"}}, {"$set": f})


async def _cancelled(bid: str) -> bool:
    d = await db.ap_batches_col.find_one({"_id": bid})
    return bool(d and d.get("status") == "cancelled")


async def process(bot, b: dict):
    """Idempotent: every step checks what is already saved, so a restart continues where it stopped."""
    channel = S.get("db_channel")
    if not channel:
        raise RuntimeError("Database Channel is not set")
    bid, cid, by = b["_id"], b["chan"], b["by"]
    root = b.get("dir") or ""
    ch = await db.ap_get_channel(cid) or {}
    vids, imgs = b.get("vids") or [], b.get("imgs") or []
    live = LIVE[cid] = {"bid": bid, "name": b.get("name") or "Folder", "stage": "videos", "ts": time.time(),
                        "cancel": False, "file": "", "tr": P.Tracker(), "itr": P.Tracker(),
                        "v_done": len(b.get("stored") or []), "v_tot": len(vids),
                        "i_done": len(b.get("img_urls") or {}), "i_tot": len(imgs)}
    try:
        # 0) folder with ONLY videos: capture 10 random lossless frames (faces first) BEFORE the videos are
        #    uploaded and deleted from disk. They become the folder's images (imgbb -> Telegraph, step 2).
        if b.get("frames") and not b.get("frames_done"):
            live["stage"] = "frames"
            paths = [os.path.join(root, v["rel"]) for v in vids]
            if not all(os.path.exists(p) for p in paths):
                raise Lost("video files missing")
            out_dir = os.path.join(root, f"_frames_{bid}")
            shutil.rmtree(out_dir, ignore_errors=True)                 # a half-finished earlier try
            try:
                got = await asyncio.to_thread(FG.grab, paths, out_dir, FG.COUNT, lambda: live["cancel"])
            except FG.Cancelled:
                return
            except RuntimeError as e:
                raise Lost(str(e))
            if not got:
                raise Lost("no frame could be captured from the videos")
            imgs = [{"rel": os.path.relpath(g, root)} for g in got]
            await _save(bid, imgs=imgs, frames_done=True, status="working")
            live["i_tot"] = len(imgs)
            live["ts"] = time.time()

        # 1) videos / files -> ONE file-store link for the folder
        vtoken = b.get("vtoken")
        if vids and not vtoken:
            vtoken = db.new_token()
            await _save(bid, vtoken=vtoken)
        stored = list(b.get("stored") or [])
        if vids and not b.get("link_done"):
            todo_bytes = sum(os.path.getsize(os.path.join(root, v["rel"])) for i, v in enumerate(vids)
                             if i >= len(stored) and os.path.exists(os.path.join(root, v["rel"])))
            live["tr"].tot = todo_bytes
            base = 0                                           # bytes of the videos already finished in this run
            for i, v in enumerate(vids):
                if i < len(stored):
                    continue
                path = os.path.join(root, v["rel"])
                if not os.path.exists(path):
                    raise Lost(f"file missing: {v['rel']}")
                vsize = os.path.getsize(path)
                live["file"] = os.path.basename(v["rel"])

                async def vprog(cur, tot, _base=base):
                    live["tr"].update(_base + cur, 0)
                    live["ts"] = time.time()
                    if live["cancel"] and mtproto.client:
                        mtproto.client.stop_transmission()    # 🛑 Stop pressed: abort this upload at once

                stored.append(await store_video(bot, channel["id"], path, os.path.basename(v["rel"]),
                                                db.make_code(vtoken, i), by, progress=vprog))
                base += vsize
                live["v_done"] = len(stored)
                await _save(bid, stored=stored, status="working")
                _rm(path)
            try:
                await db.create_link(stored, by, (b["name"] or "Folder")[:40] + " (videos)", token=vtoken, chan=cid)
            except DuplicateKeyError:
                pass                                           # a crash happened right after the link was created
            await _save(bid, link_done=True)

        # 2) images -> imgbb (original bytes, IMG_PARALLEL at a time, key rotation) -> Telegraph
        live["stage"] = "images"
        urls = dict(b.get("img_urls") or {})
        failed = list(b.get("img_failed") or [])
        tries = dict(b.get("img_tries") or {})
        bad = {f["i"] for f in failed}
        todo = [i for i in range(len(imgs)) if str(i) not in urls and i not in bad]
        live["itr"].tot = len(todo)
        sem, lock, fatal = asyncio.Semaphore(IMG_PARALLEL), asyncio.Lock(), []

        async def one(session, i: int):
            async with sem:
                if fatal or live["cancel"]:
                    return
                k, im = str(i), imgs[i]
                path = os.path.join(root, im["rel"])
                if not os.path.exists(path):
                    fatal.append(Lost(f"file missing: {im['rel']}"))
                    return
                if os.path.getsize(path) > ZS.IMGBB_MAX:
                    async with lock:
                        failed.append({"i": i, "name": os.path.basename(im["rel"]), "why": "over 32 MB (imgbb limit)"})
                        bad.add(i)
                        await _save(bid, img_failed=failed)
                    return
                data = await asyncio.to_thread(Path(path).read_bytes)      # ORIGINAL bytes, never re-encoded
                try:
                    url = await BT.upload(session, data)
                except BT.NoKeyAvailable as e:
                    msg = str(e)
                    if "resting" in msg or "no imgbb API key" in msg:
                        fatal.append(e)                                    # key problem: pause, retry later
                        return
                    tries[k] = tries.get(k, 0) + 1
                    if tries[k] < MAX_IMG_TRIES:
                        async with lock:
                            await _save(bid, img_tries=tries)
                        fatal.append(e)
                        return
                    async with lock:
                        failed.append({"i": i, "name": os.path.basename(im["rel"]), "why": msg[:80]})
                        bad.add(i)
                        await _save(bid, img_failed=failed)
                    return
                async with lock:
                    urls[k] = url
                    await _save(bid, img_urls=urls, status="working")
                    live["i_done"] = len(urls)
                    live["itr"].update(live["itr"].cur + 1, 0)
                    live["ts"] = time.time()
                _rm(path)
                await asyncio.sleep(random.uniform(BT.CD_MIN, BT.CD_MAX))

        async with aiohttp.ClientSession(timeout=BT._TIMEOUT) as session:
            await asyncio.gather(*(one(session, i) for i in todo))
            if live["cancel"] or await _cancelled(bid):
                return
            if fatal:
                raise fatal[0]
            ordered = [urls[str(i)] for i in range(len(imgs)) if str(i) in urls]
            if not ordered:
                raise Lost("no image could be uploaded to imgbb")
            tg = b.get("tg_url")
            if not tg:
                live["stage"] = "telegraph"
                tg = await _telegraph(session, BT.render_title(ch.get("tg_title"), b["name"]), ordered)
                await _save(bid, tg_url=tg)
    except Lost as e:
        log.warning("ZIP folder %s failed: %s", b.get("name"), e)
        await _save(bid, status="failed", error=str(e)[:300])
        await _notify(bot, b, failed_name=b.get("name"), why=str(e))
        await _cleanup(b)
        return
    except Exception:
        if live["cancel"] or await _cancelled(bid):            # aborted by 🛑 Stop: not an error
            return
        raise
    finally:
        if LIVE.get(cid) is live:
            live["ts"] = 0

    await _save(bid, status="need_thumb", error="")
    if b.get("frames"):                                    # video-only folder: post with the FIXED thumbnail right away
        await _auto_fixed(bot, b)
    await _cleanup(b)
    await _notify(bot, b)


async def _auto_fixed(bot, b: dict) -> bool:
    """Video-only folder (frames as preview): create its post with the channel's fixed thumbnail, no questions asked.
    Without a fixed thumbnail the folder just waits in 🖼 Thumbnails like any other."""
    ch = await db.ap_get_channel(b["chan"]) or {}
    if not ch.get("fthumb"):
        return False
    doc = await db.ap_batches_col.find_one({"_id": b["_id"]})
    if not doc or doc.get("status") != "need_thumb":
        return False
    try:
        return bool(await make_posts(bot, doc, [], b["by"], fixed=True))
    except Exception as e:
        log.warning("auto fixed-thumbnail post of %s failed: %s", b.get("name"), e)
        return False


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
    if not n:
        text = "✅ <b>VAULT PACK</b>\n\nALL FOLDERS ARE DONE, THE POSTS ARE CREATED.\n(VIDEO-ONLY FOLDERS: 10 FRAMES + FIXED THUMBNAIL.)"
        if failed_name:
            text += f"\n⚠️ “{html.escape(failed_name)}” FAILED: <code>{html.escape(why[:200])}</code>\nSEND ITS ZIP AGAIN."
        try:
            await bot.send_message(by, styled(text))
        except Exception:
            log.warning("could not notify admin %s", by)
        return
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
