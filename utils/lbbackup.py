"""
utils/lbbackup.py - Local Database Backup (owner only).

Reads every file of the Database Channel over MTProto and packs them into ZIP parts (default 1.5 GB each) together
with their captions, order, share-link records and a full MongoDB export. ONE part at a time:

    build part (streamed straight into the zip, no temp copy of the files) -> verify (sha256 + zip CRC)
    -> upload it to the owner's chat over MTProto (+ a protected download link as the alternative)
    -> only after delivery is confirmed: delete it from the VPS -> next part

The job state lives in MongoDB (collections lb_jobs / lb_items), so a restart resumes at the first undelivered part.
Why files are listed from MongoDB: a bot cannot read channel history, but it can read messages by id - and every
file of the channel is known by id (links.ids + files).
"""
import asyncio
import errno
import gzip
import json
import logging
import os
import secrets
import shutil
import time
from pathlib import Path

from telegram.error import BadRequest, TelegramError

import config
from database import db
from database.db import settings as S
from utils import backup as BK
from utils import keyboards as K
from utils import lbcore as C
from utils import lbpaths, lbweb
from utils.helpers import call
from utils.lbclient import (PREFETCH, describe, download_thumb, get_messages, iter_media, kwfilter, lbclient,
                            with_flood)
from utils.ui import esc, head, hl

log = logging.getLogger(__name__)

JOBS = db.db["lb_jobs"]            # _id "backup": the one backup job (state, plan, delivered parts)
ITEMS = db.db["lb_items"]          # one document per file of the plan
SKIP_KEEP = 200                    # how many "not backed up" entries are kept in the index
MONGO_SNAPSHOT = "lb_mongo.jsonl.gz"


class DiskFull(Exception):
    pass


# ------------------------------------------------------------------ temp folder
def cleanup_tmp(keep=()):
    """Delete leftovers of a crashed run (and finished parts). Only files of this feature are touched."""
    d = lbpaths.tmp_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        if name in keep:
            continue
        if name.startswith(("dbbackup_", "lb_")) or name.endswith((".part", ".tmp")):
            p = os.path.join(d, name)
            try:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            except OSError:
                pass


def disk_free() -> int:
    try:
        return shutil.disk_usage(lbpaths.tmp_dir()).free
    except OSError:
        return 0


def part_limit() -> int:
    mb = S.get("lb_part_mb") or C.DEFAULT_PART_MB
    try:
        mb = int(mb)
    except (TypeError, ValueError):
        mb = C.DEFAULT_PART_MB
    return min(C.MAX_PART_MB, max(C.MIN_PART_MB, mb)) * C.MiB


# ------------------------------------------------------------------ MongoDB snapshot
async def export_mongo(bot, path: str) -> dict:
    """Stream the whole MongoDB export to a gzip file (same lines as the 'fsb-backup' file). Returns the counts."""
    me = await bot.get_me()
    bid = secrets.token_hex(3)
    now = time.time()
    headline = {"t": "fsb-backup", "v": BK.FORMAT, "id": bid, "created": now, "bot": me.username,
                "created_str": time.strftime("%d %b %Y %H:%M", time.localtime(now)),
                "db_name": config.DB_NAME, "base_url": config.BASE_URL}
    counts = {}
    img = await BK._start_image(bot)
    with gzip.open(path, "wb", compresslevel=6) as gz:
        gz.write(BK._line(headline) + b"\n")
        if img:
            gz.write(BK._line({"t": "asset", "name": "start_image", "b64": img}) + b"\n")
        for name in BK.COLLECTIONS:
            n = 0
            async for d in db.db[name].find({}):
                gz.write(BK._line({"t": "doc", "c": name, "d": d}) + b"\n")
                n += 1
                if n % 500 == 0:
                    await asyncio.sleep(0)
            counts[name] = n
        gz.write(BK._line({"t": "end", "id": bid, "part": 1, "of": 1}) + b"\n")
    return counts


# ------------------------------------------------------------------ the runner
class BackupRunner:
    def __init__(self):
        self.job = None                 # in-memory mirror of the lb_jobs document
        self.task = None
        self.bot = None
        self.live: dict = {}
        self.meter = C.Meter()
        self.stop_flag = False
        self.scan_stop = False
        self._decision = None
        self._web_evt = None
        self.dl_url = None
        self.dl_token = None
        self.t0 = 0.0
        self.bytes_run = 0

    # ---------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    async def load(self):
        self.job = await JOBS.find_one({"_id": "backup"})

    async def _save(self, **fields):
        if self.job is None:
            return
        self.job.update(fields)
        await JOBS.update_one({"_id": "backup"}, {"$set": fields})

    def _spawn(self, coro):
        self.task = asyncio.ensure_future(coro)
        return self.task

    # ---------------------------------------------------------- public API (called by the panel)
    async def start_scan(self, bot, chat_id: int, msg_id: int):
        """Step 1: read the channel, plan the parts, wait for the owner's "Start"."""
        self.bot = bot
        self.stop_flag = self.scan_stop = False
        old = self.job
        if old:
            await self._discard_items(old.get("set_id"))
        now = time.time()
        date = time.strftime("%Y%m%d", time.localtime(now))
        job = {"_id": "backup", "status": "scanning", "set_id": f"{date}-{secrets.token_hex(2)}", "date": date,
               "created": now, "limit": part_limit(), "total_files": 0, "total_bytes": 0, "total_parts": 0,
               "plan": [], "cur_part": 1, "parts": [], "files_done": 0, "step": "scanning", "error": "",
               "panel": [chat_id, msg_id], "bot": getattr(bot, "username", None), "db_channel": S.get("db_channel"),
               "skipped": [], "skipped_n": 0, "scan": {"done": 0, "total": 0}}
        await JOBS.replace_one({"_id": "backup"}, job, upsert=True)
        self.job = job
        self.live = {}
        self._spawn(self._scan_task())

    async def start(self, bot):
        """Step 2: the owner confirmed -> run (also used to resume a stopped / paused job)."""
        self.bot = bot
        if self.running or not self.job:
            return
        self.stop_flag = False
        self._spawn(self._run())

    def attach(self, chat_id: int, msg_id: int):
        if self.job:
            self.job["panel"] = [chat_id, msg_id]
            asyncio.ensure_future(JOBS.update_one({"_id": "backup"}, {"$set": {"panel": [chat_id, msg_id]}}))

    def request_stop(self):
        self.stop_flag = True
        self.scan_stop = True
        if self.job and self.job.get("status") == "waiting":
            self.decide("stop")

    def decide(self, what: str):
        if self._decision and not self._decision.done():
            self._decision.set_result(what)

    async def web_done(self, n: int, tok: str):
        """The owner tapped ✅ Downloaded on the download page."""
        if self.job and self.job.get("cur_part") == n and tok == self.dl_token and self._web_evt:
            self._web_evt.set()

    async def discard(self):
        """Forget the job (and its plan). Only when nothing is running."""
        if self.running:
            return False
        if self.job:
            await self._discard_items(self.job.get("set_id"))
        await JOBS.delete_one({"_id": "backup"})
        self.job = None
        cleanup_tmp()
        return True

    async def _discard_items(self, set_id):
        if set_id:
            await ITEMS.delete_many({"set": set_id})

    async def shutdown(self):
        if self.running:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    # ---------------------------------------------------------- scan + plan
    async def _scan_task(self):
        client = None
        try:
            client = await lbclient.acquire()
            await self._scan(client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Backup scan failed")
            await self._save(status="failed", error=f"{type(e).__name__}: {e}"[:300])
        finally:
            if client is not None:
                await lbclient.release()

    async def _scan(self, client):
        j = self.job
        chan = S.get("db_channel")
        if not chan:
            raise RuntimeError("Database Channel is not set")
        chan_id, sid = chan["id"], j["set_id"]

        # 1) every (link, slot) -> message id, in link order; then the files that no link points to
        refs, order = {}, []
        async for link in db.links_col.find({}, {"ids": 1}).sort("created", 1):
            for idx, mid in enumerate(link.get("ids") or []):
                if mid:
                    if mid not in refs:
                        refs[mid] = []
                        order.append(mid)
                    refs[mid].append([link["_id"], idx])
        fdocs = {}
        async for f in db.files_col.find({}, {"msg_id": 1, "code": 1, "cap": 1, "name": 1, "by": 1, "date": 1}):
            if f.get("msg_id") is not None:
                fdocs[f["msg_id"]] = f
        order += sorted(m for m in fdocs if m not in refs)
        await self._save(scan={"done": 0, "total": len(order)})

        # 2) read the messages (200 per request) and describe every file
        costs, used, skipped, seq, total_bytes, batch = [], set(), [], 0, 0, []
        for i in range(0, len(order), 200):
            if self.scan_stop:
                await self._save(status="stopped", error="Scan cancelled")
                return
            ids = order[i:i + 200]
            for mid, msg in zip(ids, await get_messages(client, chan_id, ids)):
                info, why = describe(msg)
                if not info:
                    skipped.append({"msg_id": mid, "reason": why, "refs": refs.get(mid, [])[:3]})
                    continue
                fd = fdocs.get(mid) or {}
                pc = db.parse_code(fd.get("code")) or db.parse_code(info["caption"])
                if pc:
                    token, idx = pc
                elif refs.get(mid):
                    token, idx = refs[mid][0]
                else:
                    token, idx = "orphan", mid
                key = f"{token}:{idx}"
                if key in used:
                    skipped.append({"msg_id": mid, "reason": "second copy of the same backup code, no link uses it",
                                    "key": key})
                    continue
                used.add(key)
                if fd.get("code") is not None and fd.get("cap") is not None:
                    cap = fd["cap"]                          # what users receive today
                else:
                    cap = db.CODE_TAIL_RE.sub("", info["caption_html"]).strip()
                batch.append({
                    "_id": f"{sid}:{seq:07d}", "set": sid, "seq": seq, "key": key, "token": token, "idx": idx,
                    "refs": refs.get(mid, []), "msg_id": mid, "kind": info["kind"], "size": info["size"],
                    "name": fd.get("name") or info["file_name"] or f"{info['kind']}_{mid}",
                    "file_name": info["file_name"], "mime": info["mime"], "duration": info["duration"],
                    "width": info["width"], "height": info["height"], "performer": info["performer"],
                    "title": info["title"], "length": info["length"], "supports_streaming": info["supports_streaming"],
                    "caption_html": cap, "code": db.make_code(token, idx), "by": fd.get("by", config.OWNER_ID),
                    "date": fd.get("date") or info["date"]})
                costs.append(C.entry_cost(info["kind"], info["size"]))
                total_bytes += info["size"]
                seq += 1
            if len(batch) >= 500:
                await ITEMS.insert_many(batch)
                batch = []
            await self._save(scan={"done": min(i + 200, len(order)), "total": len(order)})
        if batch:
            await ITEMS.insert_many(batch)

        # 3) MongoDB snapshot (its compressed size is reserved in part 1) + the plan
        snap = os.path.join(lbpaths.tmp_dir(), MONGO_SNAPSHOT)
        counts = await export_mongo(self.bot, snap)
        mongo_size = os.path.getsize(snap)
        plan = C.plan_parts(costs, j["limit"], first_reserve=mongo_size)
        await self._save(status="ready", step="ready", total_files=seq, total_bytes=total_bytes,
                         total_parts=len(plan), plan=[list(p) for p in plan], mongo_size=mongo_size,
                         mongo_counts=counts, skipped=skipped[:SKIP_KEEP], skipped_n=len(skipped),
                         links_total=await db.links_col.count_documents({}))

    # ---------------------------------------------------------- the main loop
    async def _run(self):
        client = None
        ticker = None
        try:
            client = await lbclient.acquire()
            self._web_evt = asyncio.Event()
            cleanup_tmp(keep=(MONGO_SNAPSHOT,) if self.job["cur_part"] == 1 else ())
            await self._save(status="running", error="")
            self.t0, self.bytes_run = time.time(), 0
            ticker = asyncio.ensure_future(self._ticker())
            while self.job["cur_part"] <= self.job["total_parts"] and not self.stop_flag:
                if not await self._do_part(client, self.job["cur_part"]):
                    break
            if self.job["cur_part"] > self.job["total_parts"]:
                await self._finish(complete=True)
            elif self.job.get("status") == "running":
                await self._finish(complete=False)          # stopped by the owner after a finished part
        except asyncio.CancelledError:
            raise                                           # restart / shutdown: state stays "running" -> resumes
        except DiskFull:
            await self._fail("THE VPS DISK IS FULL. FREE SPACE, THEN RESUME.")
        except Exception as e:
            log.exception("Backup failed")
            await self._fail(f"{type(e).__name__}: {e}"[:300])
        finally:
            if ticker:
                ticker.cancel()
            if client is not None:
                await lbclient.release()
            cleanup_tmp()

    async def _fail(self, msg: str):
        await self._save(status="failed", error=msg, step="idle")
        await self._render_now()
        await self._say(head("⚠️", "Backup Stopped", f"<code>{esc(msg)}</code>\n\n" +
                             hl("PARTS ALREADY DELIVERED ARE SAFE. TAP <b>RESUME</b> IN THE BACKUP SCREEN.")))

    async def _do_part(self, client, n: int) -> bool:
        """Build -> verify -> deliver -> delete one part. False = the job stopped / paused here."""
        rec = path = None
        for attempt in range(3):
            self.live.update(step="building", part_files=0, part_bytes=0)
            self.meter.reset()
            await self._save(step="building")
            rec, path = await self._build(client, n)
            self.live["step"] = "verifying"
            ok, why, _ = await asyncio.to_thread(C.verify_zip, path)
            if ok:
                break
            log.warning("Part %s failed verification (%s), rebuilding", n, why)
            self._rm(path)
            rec = None
        if rec is None:
            raise RuntimeError(f"part {n} failed its integrity check: {why}")
        rec["sha256"] = await asyncio.to_thread(C.sha256_file, path)

        via = await self._deliver(client, rec, path)
        if via is None:                                      # the owner stopped while we waited
            self._rm(path)
            await self._save(status="stopped", step="idle")
            await self._finish(complete=False)
            return False

        await self._save(step="deleting")
        self.live["step"] = "deleting"
        self._rm(path)
        rec.update(delivered=True, via=via, at=time.time())
        parts = list(self.job["parts"]) + [rec]
        await self._save(parts=parts, cur_part=n + 1, files_done=self.job["files_done"] + rec["files"],
                         step="idle")
        self.live.update(part_files=0, link=None)
        return True

    @staticmethod
    def _rm(path):
        try:
            os.remove(path)
        except OSError:
            pass

    # ---------------------------------------------------------- building one part
    def _tick(self, n: int):
        self.bytes_run += n
        self.live["part_bytes"] = self.live.get("part_bytes", 0) + n
        self.meter.add(self.bytes_run)

    async def _build(self, client, n: int):
        j = self.job
        a, b = j["plan"][n - 1]
        name = C.part_name(j["date"], n)
        path = Path(lbpaths.tmp_dir()) / name
        self._rm(path)
        items = await ITEMS.find({"set": j["set_id"], "seq": {"$gte": a, "$lt": b}}).sort("seq", 1).to_list(None)
        chan_id = j["db_channel"]["id"]
        w = C.PartWriter(path)
        files, junk, failed = [], [], []
        try:
            mongo = await self._add_mongo(w) if n == 1 else None
            for it in items:
                rec, err = await self._add_file(client, w, chan_id, it, junk)
                self.live["part_files"] = self.live.get("part_files", 0) + 1
                if rec:
                    files.append(rec)
                else:
                    failed.append({"key": it["key"], "msg_id": it["msg_id"], "reason": err})
            links = await self._link_records(files)
            manifest = {"format": C.FORMAT, "v": C.VERSION, "set_id": j["set_id"], "date": j["date"], "part": n,
                        "total_parts": j["total_parts"], "created": j["created"], "built": time.time(),
                        "bot": j.get("bot"), "db_channel": j.get("db_channel"), "mongo": mongo,
                        "files": files, "links": links, "junk": junk}
            size = await asyncio.to_thread(w.finish, manifest)
        except BaseException:
            w.abort()
            raise
        if failed:
            skipped = (self.job.get("skipped") or []) + failed
            await self._save(skipped=skipped[:SKIP_KEEP], skipped_n=self.job.get("skipped_n", 0) + len(failed))
        rec = {"n": n, "name": name, "size": size, "files": len(items), "stored": len(files),
               "bytes": sum(f["size"] for f in files), "delivered": False}
        return rec, path

    async def _add_mongo(self, w: C.PartWriter) -> dict:
        snap = os.path.join(lbpaths.tmp_dir(), MONGO_SNAPSHOT)
        if not os.path.isfile(snap):                         # a restart removed it: export again
            await export_mongo(self.bot, snap)
        size = os.path.getsize(snap)
        with w.entry(C.MONGO_ENTRY, size) as ent:
            with open(snap, "rb") as f:
                while True:
                    chunk = await asyncio.to_thread(f.read, C.MiB)
                    if not chunk:
                        break
                    ent.write(chunk)
        self._rm(snap)
        return {"path": C.MONGO_ENTRY, "size": ent.size, "sha256": ent.sha256,
                "counts": self.job.get("mongo_counts", {})}

    async def _add_file(self, client, w: C.PartWriter, chan_id, it: dict, junk: list):
        """Stream one file into the part. Returns (manifest entry, "") or (None, reason)."""
        err = "unknown error"
        for attempt in range(3):
            ent = None
            try:
                msg = (await get_messages(client, chan_id, [it["msg_id"]]))[0]
                info, why = describe(msg)
                if not info:
                    return None, why
                size = info["size"]
                arc = C.media_path(it["token"], it["idx"], info["file_name"] or it["name"], info["kind"])
                if attempt:
                    arc += f".r{attempt}"
                ent = w.entry(arc, size)
                async for chunk in iter_media(client, msg, size, PREFETCH):
                    await asyncio.to_thread(ent.write, chunk)
                    self._tick(len(chunk))
                ent.close()
                if size and ent.size != size:
                    raise IOError(f"size mismatch ({ent.size} of {size} bytes)")
                thumb = None
                data = await download_thumb(client, info["thumb_fid"])
                if data:
                    tp = C.thumb_path(it["token"], it["idx"])
                    thumb = {"path": tp, "sha256": w.add_bytes(tp, data)}
                return {
                    "key": it["key"], "token": it["token"], "idx": it["idx"], "refs": it["refs"],
                    "kind": info["kind"], "name": it["name"], "file_name": info["file_name"], "mime": info["mime"],
                    "caption_html": it["caption_html"], "code": it["code"], "size": ent.size, "sha256": ent.sha256,
                    "msg_id": it["msg_id"], "date": it["date"], "path": arc, "by": it["by"],
                    "duration": info["duration"], "width": info["width"], "height": info["height"],
                    "performer": info["performer"], "title": info["title"], "length": info["length"],
                    "supports_streaming": info["supports_streaming"], "thumb": thumb}, ""
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if isinstance(e, OSError) and e.errno == errno.ENOSPC:
                    raise DiskFull()
                err = f"{type(e).__name__}: {e}"[:200]
                log.warning("Backup of message %s failed (try %d): %s", it["msg_id"], attempt + 1, err)
                if ent is not None:
                    try:
                        ent.close()
                        junk.append(ent.arc)             # a partial copy stays in the zip; the manifest never points to it
                    except Exception:
                        pass
                await asyncio.sleep(2 * (attempt + 1))
        return None, err

    async def _link_records(self, files: list) -> list:
        tokens = sorted({r[0] for f in files for r in f["refs"]})
        out = []
        for i in range(0, len(tokens), 500):
            async for l in db.links_col.find({"_id": {"$in": tokens[i:i + 500]}}):
                out.append({"token": l["_id"], "label": l.get("label"), "ids": l.get("ids") or [],
                            "views": l.get("views", 0), "creator": l.get("creator"), "no_ads": bool(l.get("no_ads")),
                            "chan": l.get("chan"), "created": l.get("created")})
        return out

    # ---------------------------------------------------------- delivery
    async def _tg_send(self, client, rec: dict, path):
        from pyrogram import enums
        j = self.job
        cap = (f"💽 <b>BACKUP PART {rec['n']}/{j['total_parts']}</b> · {C.human(rec['size'])}\n"
               f"SHA256 <code>{rec['sha256']}</code>")

        def progress(cur, total):
            self.live["up_bytes"] = cur
            self.meter.add(cur)

        self.meter.reset()
        kw = kwfilter(client.send_document, chat_id=config.OWNER_ID, document=str(path), file_name=rec["name"],
                      caption=cap, parse_mode=enums.ParseMode.HTML, force_document=True, progress=progress)
        msg = await with_flood(client.send_document, **kw)
        got = getattr(getattr(msg, "document", None), "file_size", None)
        if not msg or (got is not None and got != rec["size"]):
            raise IOError("Telegram did not confirm the full upload")
        return msg

    async def _deliver(self, client, rec: dict, path):
        """'tg' (Telegram returned the sent message) | 'web' (you tapped ✅ Downloaded) | None (you stopped)."""
        tok = await lbweb.create("dl", lbweb.DL_TTL, n=rec["n"], path=str(path), name=rec["name"], size=rec["size"],
                                 sha=rec["sha256"], hits=0)
        self.dl_token, self.dl_url = tok, lbweb.url(f"/bkdl/{tok}")
        self._web_evt.clear()
        self.live.update(step="uploading", sha=rec["sha256"], link=self.dl_url, up_bytes=0, size=rec["size"])
        await self._save(step="uploading")
        try:
            while True:
                last = ""
                for attempt in range(1, 4):
                    self.live["attempt"] = attempt
                    up = asyncio.ensure_future(self._tg_send(client, rec, path))
                    web = asyncio.ensure_future(self._web_evt.wait())
                    try:
                        await asyncio.wait({up, web}, return_when=asyncio.FIRST_COMPLETED)
                        if web.done() and not up.done():
                            return "web"
                        if up.done():
                            if up.exception() is None:
                                return "tg"
                            last = f"{type(up.exception()).__name__}: {up.exception()}"[:200]
                            log.warning("Part %s upload failed (try %d): %s", rec["n"], attempt, last)
                    finally:
                        for t in (up, web):
                            if not t.done():
                                t.cancel()
                        await asyncio.gather(up, web, return_exceptions=True)
                    if attempt < 3:
                        await asyncio.sleep(5 * attempt)
                # 3 failures: do NOT delete anything, ask the owner
                self._decision = asyncio.get_running_loop().create_future()
                web = asyncio.ensure_future(self._web_evt.wait())
                await self._save(status="waiting", error=last)
                await self._render_now()
                await self._say(head("⚠️", "Upload Failed", f"PART <b>{rec['n']}</b> COULD NOT BE SENT 3 TIMES.\n"
                                     f"<code>{esc(last)}</code>\n\n" +
                                     hl("THE PART IS KEPT ON THE VPS. RETRY, OR DOWNLOAD IT FROM THE LINK AND TAP "
                                        "<b>DOWNLOADED</b>.")))
                try:
                    await asyncio.wait({self._decision, web}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    web.cancel()
                what = "confirm" if self._web_evt.is_set() else (self._decision.result() if self._decision.done() else "retry")
                self._decision = None
                if what == "stop":
                    return None
                if what == "confirm":
                    return "web"
                await self._save(status="running", error="")
        finally:
            await lbweb.drop(tok)
            self.dl_token = self.dl_url = None

    # ---------------------------------------------------------- finishing
    async def _finish(self, complete: bool):
        j = self.job
        status = "done" if complete else "stopped"
        await self._save(status=status, step="idle", finished=time.time())
        parts = j["parts"]
        index = C.build_index(j, parts, complete)
        ipath = Path(lbpaths.tmp_dir()) / f"lb_{C.INDEX_NAME}"
        ipath.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            with open(ipath, "rb") as f:
                await call(self.bot.send_document, config.OWNER_ID, document=f, filename=C.INDEX_NAME,
                           caption=f"🧾 <b>{C.INDEX_NAME}</b> · {len(parts)}/{j['total_parts']} PARTS")
        except TelegramError as e:
            log.warning("Could not send the index: %s", e)
        self._rm(ipath)
        await self._render_now()
        for text in self.final_texts(complete):
            await self._say(text)

    def final_texts(self, complete: bool) -> list:
        j = self.job
        parts = j["parts"]
        total = sum(p["size"] for p in parts)
        title = "Backup Complete" if complete else "Backup Stopped"
        intro = (f"PARTS ▸ <b>{len(parts)}/{j['total_parts']}</b>\n"
                 f"TOTAL SIZE ▸ <b>{C.human(total)}</b>\n"
                 f"FILES ▸ <b>{sum(p['stored'] for p in parts):,}/{j['total_files']:,}</b>")
        if j.get("skipped_n"):
            intro += f"\n⚠️ NOT BACKED UP ▸ <b>{j['skipped_n']}</b> (LISTED IN {C.INDEX_NAME})"
        if not complete:
            intro += "\n\nTAP <b>RESUME</b> IN THE BACKUP SCREEN TO CONTINUE WITH THE NEXT PART."
        lines = [f"<b>{p['name']}</b>\n<code>{p['sha256']}</code>" for p in parts]
        texts, cur = [], head("✅" if complete else "⏹", title, intro)
        for ln in lines:
            if len(cur) + len(ln) > 3600:
                texts.append(cur)
                cur = ""
            cur += ("\n\n" if cur else "") + ln
        texts.append(cur)
        if complete:
            texts[-1] += "\n\n" + hl("CHECK THAT YOU RECEIVED EVERY PART AND THAT EACH SHA256 MATCHES.")
        return texts

    # ---------------------------------------------------------- progress message
    def render(self):
        """(text, markup) of the live progress message."""
        j = self.job or {}
        st = j.get("status")
        step = self.live.get("step") or j.get("step")
        total_parts = j.get("total_parts", 0)
        n = min(j.get("cur_part", 1), max(1, total_parts))
        if st == "scanning":
            sc = j.get("scan", {})
            return (head("💽", "Database Backup", f"🔎 READING THE CHANNEL ▸ <b>{sc.get('done', 0):,}/{sc.get('total', 0):,}</b>"),
                    K.kb([], K.ub("⏹ Stop Backup", "adm:lbstop")))
        if st in ("done", "stopped", "failed"):
            icon, title = {"done": ("✅", "Backup Complete"), "stopped": ("⏹", "Backup Stopped"),
                           "failed": ("⚠️", "Backup Stopped")}[st]
            done_parts = len(j.get("parts", []))
            desc = f"PARTS DELIVERED ▸ <b>{done_parts}/{total_parts}</b>"
            if st == "failed" and j.get("error"):
                desc += f"\n<code>{esc(j['error'])}</code>"
            rows = [[K.ub("▶️ Resume Backup", "adm:lbresume")]] if st in ("stopped", "failed") else []
            return head(icon, title, desc), K.kb(rows, K.ub("🔙 Back", "adm:lb"))
        done = j.get("files_done", 0) + (self.live.get("part_files", 0) if st in ("running", "waiting") else 0)
        labels = {"building": "⚙️ BUILDING", "verifying": "⚙️ BUILDING · VERIFYING", "uploading": "⬆️ UPLOADING",
                  "deleting": "🗑 DELETING", "idle": "⏳ NEXT PART"}
        thr = self.bytes_run / max(1.0, time.time() - self.t0) if self.t0 else 0
        left = max(0, j.get("total_bytes", 0) - sum(p.get("bytes", 0) for p in j.get("parts", []))
                   - self.live.get("part_bytes", 0))
        eta = left / thr if thr > 0 else None
        lines = [f"PART ▸ <b>{n}/{total_parts}</b>",
                 f"FILES ▸ <b>{done:,}/{j.get('total_files', 0):,}</b>",
                 f"STEP ▸ <b>{labels.get(step, step or '…')}</b>"]
        if st == "running":
            lines.append(f"SPEED ▸ <b>{C.human(self.meter.speed())}/S</b> · ETA <b>{C.eta_text(eta)}</b>")
            if step == "uploading":
                size = self.live.get("size") or 1
                lines.append(f"UPLOAD ▸ <b>{self.live.get('up_bytes', 0) * 100 // size}%</b> · "
                             f"TRY <b>{self.live.get('attempt', 1)}/3</b>")
            if self.stop_flag:
                lines.append("⏹ STOPPING AFTER THIS PART…")
        if step == "uploading" or st == "waiting":
            if self.live.get("sha"):
                lines.append(f"SHA256 ▸ <code>{self.live['sha']}</code>")
            if self.dl_url:
                lines.append(f'🔗 <a href="{self.dl_url}">DOWNLOAD LINK</a>')
        if st == "waiting":
            lines.append("\n" + hl("⚠️ UPLOAD FAILED. NOTHING WAS DELETED."))
            if j.get("error"):
                lines.append(f"<code>{esc(j['error'])}</code>")
        rows = []
        if st == "waiting":
            rows.append([K.ub("🔁 Retry Upload", "adm:lbretry"), K.ub("✅ Downloaded", "adm:lbdone")])
        if self.dl_url:
            rows.append([K.link("🔗 Download Link", self.dl_url)])
        stop_btn = None if (self.stop_flag and st != "waiting") else K.ub("⏹ Stop Backup", "adm:lbstop")
        return head("💽", "Database Backup", "\n".join(lines)), K.kb(rows, stop_btn)

    async def _render_now(self):
        panel = (self.job or {}).get("panel")
        if not panel or not self.bot:
            return
        text, markup = self.render()
        try:
            await self.bot.edit_message_text(text, chat_id=panel[0], message_id=panel[1], reply_markup=markup)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            if "not found" in str(e).lower() or "can't be edited" in str(e).lower():
                try:
                    m = await self.bot.send_message(config.OWNER_ID, text, reply_markup=markup)
                    await self._save(panel=[m.chat_id, m.message_id])
                except TelegramError:
                    pass
        except TelegramError as e:
            log.debug("Progress edit failed: %s", e)

    async def _ticker(self):
        while True:
            await asyncio.sleep(4)
            try:
                await self._render_now()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("progress tick failed", exc_info=True)

    async def _say(self, text: str):
        try:
            await call(self.bot.send_message, config.OWNER_ID, text)
        except TelegramError as e:
            log.warning("Could not message the owner: %s", e)


runner = BackupRunner()


# ------------------------------------------------------------------ start-up / shutdown
async def on_startup(bot):
    """Indexes, leftover cleanup, resume of an interrupted backup."""
    try:
        await ITEMS.create_index([("set", 1), ("seq", 1)])
        await lbweb.init()
    except Exception:
        log.exception("Local backup: index creation failed")
    runner.bot = bot
    lbweb.hooks["dl_done"] = runner.web_done
    cleanup_tmp()                                           # a crashed run leaves temp files behind
    try:
        await runner.load()
    except Exception:
        log.exception("Local backup: could not load the job")
        return
    j = runner.job
    if not j:
        return
    if j.get("status") == "scanning":                       # interrupted before the plan existed
        await runner._discard_items(j.get("set_id"))
        await JOBS.delete_one({"_id": "backup"})
        runner.job = None
        await runner._say(head("⚠️", "Backup Not Started", "THE BOT RESTARTED WHILE READING THE CHANNEL.\nSTART THE BACKUP AGAIN."))
    elif j.get("status") == "running":
        log.info("Local backup: resuming at part %s", j.get("cur_part"))
        await runner._say(head("♻️", "Backup Resumed", f"THE BOT RESTARTED. CONTINUING WITH PART <b>{j['cur_part']}/{j['total_parts']}</b>."))
        await runner.start(bot)
    elif j.get("status") == "waiting":
        await runner._save(status="stopped", error="")
        await runner._say(head("⏸", "Backup Paused", f"THE BOT RESTARTED. PART <b>{j['cur_part']}</b> WAS NOT DELIVERED YET.\n"
                               "OPEN THE BACKUP SCREEN AND TAP <b>RESUME</b>."))
