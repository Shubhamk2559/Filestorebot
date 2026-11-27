"""
utils/lbrestore.py - Recover From Zip (owner only).

Reads the backup parts from the restore folder (placed by you, or uploaded on the protected web page) and rebuilds the
Database Channel inside the CURRENT (new) channel:

  for every file: verify sha256 -> upload it (same kind, file name, duration, thumbnail) with the exact original caption
  (HTML entities intact) + the hidden backup code -> point the SAME link token / slot at the new message id
  + settings, users, links (order, views, labels) and Autoposter data from the MongoDB export.

Idempotent + resumable: every restored file is recorded in MongoDB (lb_rest_items, keyed by backup set + channel +
token:slot). Running Recover again skips what is done, so nothing is ever uploaded twice. A part is deleted from the VPS
only when every file in it is restored.
"""
import asyncio
import errno
import gzip
import html
import io
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path

from telegram.error import TelegramError

import config
from database import db
from database.db import settings as S
from utils import backup as BK
from utils import keyboards as K
from utils import lbcore as C
from utils import lbpaths
from utils.helpers import call
from utils.lbclient import PREFETCH, get_messages, kwfilter, lbclient, with_flood  # noqa: F401
from utils.lbbackup import JOBS, cleanup_tmp
from utils.ui import esc, head, hl

log = logging.getLogger(__name__)

RITEMS = db.db["lb_rest_items"]            # _id "<job>|<token>:<slot>": status done | failed, new message id, reason
PACE = float(os.getenv("LR_PACE", "3.0"))  # minimum seconds between two uploads into the channel
ATTEMPTS = 3
FATAL = ("CHAT_WRITE_FORBIDDEN", "CHAT_ADMIN_REQUIRED", "CHANNEL_INVALID", "CHANNEL_PRIVATE", "PEER_ID_INVALID",
         "USER_BANNED_IN_CHANNEL", "CHAT_SEND_MEDIA_FORBIDDEN")


class Fatal(Exception):
    pass


def analyze(folder: str) -> dict:
    """What is in the restore folder (blocking, reads only zip directories)."""
    found = C.scan_dir(folder)
    set_id, parts, others = C.pick_set(found)
    index = C.read_index(folder)
    if index and set_id and index.get("set_id") != set_id:
        index = None
    return {"found": found, "set_id": set_id, "parts": parts, "other_sets": others, "index": index,
            "missing": C.missing_parts(parts, index) if parts else [],
            "unreadable": [r for r in found if not r["manifest"]]}


class RestoreRunner:
    def __init__(self):
        self.job = None
        self.task = None
        self.bot = None
        self.live: dict = {}
        self.stop_flag = False
        self.meter = C.Meter()
        self.t0 = 0.0
        self.bytes_run = 0

    # ---------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    async def load(self):
        self.job = await JOBS.find_one({"_id": "restore"})

    async def _save(self, **fields):
        if self.job is None:
            return
        self.job.update(fields)
        await JOBS.update_one({"_id": "restore"}, {"$set": fields})

    @property
    def jid(self) -> str:
        return self.job["job_id"]

    # ---------------------------------------------------------- public API
    async def start(self, bot, chat_id: int, msg_id: int, part_names: list):
        """Start (or continue) the recover job of this backup set + the CURRENT Database Channel."""
        self.bot = bot
        if self.running:
            return
        chan = S.get("db_channel")
        if not chan:
            raise RuntimeError("Database Channel is not set")
        info = await asyncio.to_thread(analyze, lbpaths.restore_dir())
        if not info["set_id"]:
            raise RuntimeError("no backup parts found in the restore folder")
        jid = f"{info['set_id']}:{chan['id']}"
        old = await JOBS.find_one({"_id": "restore"})
        if old and old.get("job_id") == jid:
            names = sorted(set(old.get("parts", [])) | set(part_names))        # same set + channel: continue
            job = {**old, "parts": names, "status": "running", "error": "", "panel": [chat_id, msg_id]}
            await JOBS.replace_one({"_id": "restore"}, job)
        else:
            job = {"_id": "restore", "job_id": jid, "set_id": info["set_id"], "channel": chan, "parts": sorted(part_names),
                   "status": "running", "started": time.time(), "panel": [chat_id, msg_id], "error": "",
                   "prepared": False, "mongo_done": False, "total_files": 0, "notes": [], "parts_deleted": []}
            await JOBS.replace_one({"_id": "restore"}, job, upsert=True)
        self.job = job
        await self._resume()

    async def _resume(self):
        self.stop_flag = False
        self.task = asyncio.ensure_future(self._run())

    async def resume_after_restart(self, bot):
        self.bot = bot
        await self._resume()

    def attach(self, chat_id: int, msg_id: int):
        if self.job:
            self.job["panel"] = [chat_id, msg_id]
            asyncio.ensure_future(JOBS.update_one({"_id": "restore"}, {"$set": {"panel": [chat_id, msg_id]}}))

    def request_stop(self):
        self.stop_flag = True

    async def shutdown(self):
        if self.running:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    # ---------------------------------------------------------- the main loop
    async def _run(self):
        client, ticker = None, None
        try:
            client = await lbclient.acquire()
            tmp = Path(lbpaths.tmp_dir()) / "lb_restore"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            self.tmp = tmp
            self.t0, self.bytes_run = time.time(), 0
            self.counts = {"restored": 0, "skipped": 0, "failed": 0}
            ticker = asyncio.ensure_future(self._ticker())

            folder = lbpaths.restore_dir()
            found = {r["name"]: r for r in await asyncio.to_thread(C.scan_dir, folder)}
            parts = sorted((found[n] for n in self.job["parts"] if n in found and found[n]["manifest"]),
                           key=lambda r: r["manifest"]["part"])
            if not parts:
                raise Fatal("none of the selected parts is in the restore folder any more")
            index = C.read_index(folder)
            await self._save(total_files=sum(len(r["manifest"]["files"]) for r in parts), status="running")
            if not self.job.get("prepared"):
                await self._prepare(parts)

            self.part_failed = {}
            for r in parts:
                if self.stop_flag:
                    break
                await self._do_part(client, r, index)
            self.stopped = self.stop_flag
            await self._finish(client, parts)
        except asyncio.CancelledError:
            raise                                   # restart / shutdown: job stays "running" -> resumes
        except Fatal as e:
            await self._fail(str(e))
        except Exception as e:
            log.exception("Recover failed")
            await self._fail(f"{type(e).__name__}: {e}"[:300])
        finally:
            if ticker:
                ticker.cancel()
            if client is not None:
                await lbclient.release()
            shutil.rmtree(getattr(self, "tmp", ""), ignore_errors=True)

    async def _fail(self, msg: str):
        await self._save(status="failed", error=msg)
        await self._render_now()
        await self._say(head("⚠️", "Recover Stopped", f"<code>{esc(msg)}</code>\n\n" +
                             hl("NOTHING IS LOST. FIX THE PROBLEM, THEN TAP <b>RECOVER</b> AGAIN TO CONTINUE.")))

    # ---------------------------------------------------------- one-time preparation
    async def _prepare(self, parts: list):
        """When the NEW channel is not the channel of the backup, every old message id in MongoDB points into a dead
        channel and could collide with an unrelated NEW message (ids start from 1 again). Null them all first."""
        old = ((parts[0]["manifest"].get("db_channel")) or {}).get("id")
        new = self.job["channel"]["id"]
        notes = list(self.job.get("notes", []))
        if old is None or old != new:
            nulled = await self._null_links()
            await db.files_col.delete_many({})
            notes.append(f"NEW CHANNEL: {nulled} LINKS RESET, THEIR FILES COME BACK AS THEY ARE RESTORED")
        await self._save(prepared=True, notes=notes)

    @staticmethod
    async def _null_links() -> int:
        n = await db.links_col.count_documents({})
        try:                                           # one server-side pass (MongoDB 4.2+)
            await db.links_col.update_many({}, [{"$set": {"ids": {"$map": {"input": {"$ifNull": ["$ids", []]},
                                                                           "as": "x", "in": None}}}}])
        except Exception:
            async for l in db.links_col.find({}, {"ids": 1}):
                await db.links_col.update_one({"_id": l["_id"]}, {"$set": {"ids": [None] * len(l.get("ids") or [])}})
        return n

    # ---------------------------------------------------------- one part
    async def _do_part(self, client, rec: dict, index):
        m, zpath = rec["manifest"], rec["path"]
        n = m["part"]
        self.live.update(part=n, step="checking", name=rec["name"])
        exp = next((p["sha256"] for p in (index or {}).get("parts", []) if p["part"] == n), None)
        if exp:
            got = await asyncio.to_thread(C.sha256_file, zpath)
            if got != exp:
                notes = list(self.job.get("notes", [])) + [f"PART {n}: SHA256 DOES NOT MATCH THE INDEX (EACH FILE IS STILL CHECKED)"]
                await self._save(notes=notes)
        with zipfile.ZipFile(zpath) as zf:
            if m.get("mongo") and not self.job.get("mongo_done"):
                self.live["step"] = "importing"
                await self._import_mongo(zf, m["mongo"])
                await self._save(mongo_done=True)
            await self._upsert_links(m.get("links", []))
            bad = []
            for e in m["files"]:
                if self.stop_flag:
                    return
                if not await self._restore_entry(client, zf, e, n):
                    bad.append(e["key"])
        self.part_failed[n] = bad
        if not bad:                                    # fully restored -> free the disk
            try:
                os.remove(zpath)
                await self._save(parts_deleted=sorted(set(self.job.get("parts_deleted", [])) | {rec["name"]}))
            except OSError as e:
                log.warning("Could not delete %s: %s", zpath, e)

    # ---------------------------------------------------------- MongoDB
    async def _import_mongo(self, zf, mg: dict):
        """Merge the export: only documents that do not exist yet are added (settings: only missing keys). Links are
        added with EMPTY slots (they fill up as files are restored); files are rebuilt from the manifests."""
        buf, n_lines, img = {}, 0, None

        async def flush(name):
            items = buf.pop(name, [])
            if not items:
                return
            if name == "settings":
                for d in items:
                    d.pop("db_channel", None)          # the channel you just set stays
                await BK._restore_settings(items, "merge")
            else:
                if name == "links":
                    for d in items:
                        d["ids"] = [None] * len(d.get("ids") or [])
                await BK._restore_collection(name, items, "merge")

        with zf.open(mg["path"]) as raw, gzip.GzipFile(fileobj=raw) as gz:
            for line in gz:
                n_lines += 1
                if n_lines % 300 == 0:
                    await asyncio.sleep(0)
                if not line.strip():
                    continue
                o = BK.json_util.loads(line, json_options=BK.JSON_OPTS)
                t = o.get("t")
                if t == "doc" and o.get("c") in BK.COLLECTIONS and o["c"] != "files":
                    buf.setdefault(o["c"], []).append(o["d"])
                    if len(buf[o["c"]]) >= 500:
                        await flush(o["c"])
                elif t == "asset" and o.get("name") == "start_image":
                    img = o.get("b64")
        for name in list(buf):
            await flush(name)
        await S.load()
        if img:
            try:
                await BK._restore_start_image(self.bot, config.OWNER_ID, img, "merge")
            except Exception:
                log.warning("Start image could not be restored", exc_info=True)

    async def _upsert_links(self, links: list):
        for l in links:
            ins = {"ids": [None] * len(l.get("ids") or []), "label": l.get("label") or "Restored link",
                   "creator": l.get("creator") or config.OWNER_ID, "views": l.get("views", 0),
                   "created": l.get("created") or time.time()}
            if l.get("no_ads"):
                ins["no_ads"] = True
            if l.get("chan") is not None and not l.get("no_ads"):
                ins["chan"] = l["chan"]
            await db.links_col.update_one({"_id": l["token"]}, {"$setOnInsert": ins}, upsert=True)

    # ---------------------------------------------------------- one file
    async def _restore_entry(self, client, zf, e: dict, part_no: int) -> bool:
        """True = restored / already restored. False = failed (recorded with the reason)."""
        key, _id = e["key"], f"{self.jid}|{e['key']}"
        prev = await RITEMS.find_one({"_id": _id})
        if prev and prev.get("status") == "done":
            if not prev.get("applied"):
                await self._apply_db(e, prev["new_id"])
                await RITEMS.update_one({"_id": _id}, {"$set": {"applied": True}})
            self.counts["skipped"] += 1
            return True

        self.live.update(step="extracting", current=e["name"])
        started = time.time()
        stem = f"{C.safe_token(e['token'])}_{e['idx']}"
        path = self.tmp / (stem + C.safe_ext(e.get("file_name") or e["name"], e["kind"]))
        thumb = None
        try:
            free = shutil.disk_usage(self.tmp).free
            if free < e["size"] + 64 * C.MiB:
                return await self._failed(_id, e, part_no, f"not enough free disk ({C.human(free)} left)")
            ok, why = await asyncio.to_thread(C.extract_entry, zf, e["path"], path, e["sha256"], e["size"])
            if not ok:
                return await self._failed(_id, e, part_no, f"checksum / extract: {why}")
            if e.get("thumb"):
                tp = self.tmp / (stem + ".thumb.jpg")
                tok, _ = await asyncio.to_thread(C.extract_entry, zf, e["thumb"]["path"], tp, e["thumb"]["sha256"])
                thumb = str(tp) if tok else None

            await RITEMS.update_one({"_id": _id}, {"$set": {"job": self.jid, "key": key, "part": part_no,
                                                            "status": "uploading", "at": time.time()}}, upsert=True)
            self.live["step"] = "uploading"
            new_id, err = None, ""
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    new_id = await self._upload(client, e, str(path), thumb)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    err = f"{type(ex).__name__}: {ex}"[:200]
                    if any(f in str(ex).upper() for f in FATAL):
                        raise Fatal(f"the bot cannot post in the new Database Channel ({err}). "
                                    "Make it admin with “Post Messages”.")
                    log.warning("Upload of %s failed (try %d): %s", key, attempt, err)
                    await asyncio.sleep(3 * attempt)
            if new_id is None:
                return await self._failed(_id, e, part_no, f"upload failed: {err}")
            await RITEMS.update_one({"_id": _id}, {"$set": {"status": "done", "new_id": new_id, "applied": False,
                                                            "at": time.time()}})
            await self._apply_db(e, new_id)
            await RITEMS.update_one({"_id": _id}, {"$set": {"applied": True}})
            self.counts["restored"] += 1
            self.bytes_run += e["size"]
            self.meter.add(self.bytes_run)
            return True
        finally:
            for p in (path, thumb):
                if p:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            wait = PACE - (time.time() - started)
            if wait > 0 and not self.stop_flag:
                await asyncio.sleep(wait)

    async def _failed(self, _id: str, e: dict, part_no: int, reason: str) -> bool:
        await RITEMS.update_one({"_id": _id}, {"$set": {"job": self.jid, "key": e["key"], "part": part_no,
                                                        "status": "failed", "reason": reason, "at": time.time()}},
                                upsert=True)
        self.counts["failed"] += 1
        return False

    async def _upload(self, client, e: dict, path: str, thumb) -> int:
        """Upload one file into the new Database Channel (MTProto, so 2 GB files work). Returns the new message id."""
        from pyrogram import enums
        kind, chan_id = e["kind"], self.job["channel"]["id"]

        def progress(cur, total):
            self.live["up_bytes"], self.live["up_total"] = cur, total

        kw = dict(chat_id=chan_id, progress=progress)
        if kind not in C.NO_CAPTION:
            kw.update(caption=C.caption_with_code(e.get("caption_html", ""), e["code"]), parse_mode=enums.ParseMode.HTML)
        name = e.get("file_name")
        opt = {"duration": e.get("duration"), "width": e.get("width"), "height": e.get("height"),
               "thumb": thumb, "file_name": name}
        if kind == "photo":
            fn, extra = client.send_photo, {"photo": path}
        elif kind == "video":
            ss = e.get("supports_streaming")
            fn, extra = client.send_video, {"video": path, "supports_streaming": True if ss is None else ss, **opt}
        elif kind == "audio":
            fn, extra = client.send_audio, {"audio": path, "performer": e.get("performer"), "title": e.get("title"),
                                            "duration": e.get("duration"), "thumb": thumb, "file_name": name}
        elif kind == "animation":
            fn, extra = client.send_animation, {"animation": path, **opt}
        elif kind == "voice":
            fn, extra = client.send_voice, {"voice": path, "duration": e.get("duration")}
        elif kind == "video_note":
            fn, extra = client.send_video_note, {"video_note": path, "duration": e.get("duration"),
                                                 "length": e.get("length"), "thumb": thumb}
        elif kind == "sticker":
            fn, extra = client.send_sticker, {"sticker": path}
        else:
            fn, extra = client.send_document, {"document": path, "thumb": thumb, "file_name": name,
                                               "force_document": True}
        args = {k: v for k, v in {**kw, **extra}.items() if v is not None}
        try:
            msg = await with_flood(fn, **kwfilter(fn, **args))
        except Exception as ex:
            if "caption" in args and any(w in str(ex).upper() for w in ("CAPTION_TOO_LONG", "ENTITY", "MESSAGE_TOO_LONG")):
                plain = html.unescape(C._TAG.sub("", e.get("caption_html", ""))).strip()
                args["caption"] = C.caption_with_code(html.escape(plain[:900]), e["code"])
                msg = await with_flood(fn, **kwfilter(fn, **args))
            else:
                raise
        return int(msg.id)

    async def _apply_db(self, e: dict, new_id: int):
        """Point the same token / slot at the new message (idempotent)."""
        doc = {"msg_id": new_id, "kind": e["kind"], "name": e["name"], "by": e.get("by") or config.OWNER_ID,
               "date": e.get("date") or time.time()}
        if e["kind"] in C.NO_CAPTION:
            flt = {"bk": e["key"]}
            doc["bk"] = e["key"]
        else:
            flt = {"code": e["code"]}
            doc.update(code=e["code"], cap=e.get("caption_html", ""))
        await db.files_col.update_one(flt, {"$set": doc}, upsert=True)
        for token, idx in e.get("refs", []):
            r = await db.links_col.update_one({"_id": token}, {"$set": {f"ids.{idx}": new_id}})
            if not r.matched_count:
                await db.links_col.update_one(
                    {"_id": token}, {"$setOnInsert": {"ids": [], "creator": config.OWNER_ID, "label": "Restored link",
                                                      "created": time.time(), "views": 0}}, upsert=True)
                await db.links_col.update_one({"_id": token}, {"$set": {f"ids.{idx}": new_id}})
            db.LINK_CACHE.pop(token, None)

    # ---------------------------------------------------------- end of the job
    async def _finish(self, client, parts: list):
        stopped = self.stopped
        report = await self._report(client, parts, stopped)
        await self._save(status="stopped" if stopped else "done", finished=time.time())
        await self._render_now()
        for text in report["texts"]:
            await self._say(text)
        if report["failed_file"]:
            try:
                await call(self.bot.send_document, config.OWNER_ID,
                           document=io.BytesIO(report["failed_file"].encode()), filename="recover_failed.txt",
                           caption="📄 FAILED FILES")
            except TelegramError:
                pass

    async def _report(self, client, parts: list, stopped: bool) -> dict:
        jid = self.jid
        done = {}
        async for d in RITEMS.find({"job": jid, "status": "done"}, {"key": 1, "new_id": 1}):
            done[d["key"]] = d["new_id"]
        failed = [d async for d in RITEMS.find({"job": jid, "status": "failed"})]
        keys = {e["key"] for r in parts for e in r["manifest"]["files"]}
        failed = [d for d in failed if d["key"] in keys and d["key"] not in done]
        total = len(keys)

        # does every link token resolve?
        slots, tokens = {}, set()
        for r in parts:
            for e in r["manifest"]["files"]:
                for t, i in e.get("refs", []):
                    slots[(t, i)] = e["key"]
                    tokens.add(t)
        toks = sorted(tokens)
        by_tok = {}
        for (t, i), k in slots.items():
            by_tok.setdefault(t, []).append((i, k))
        bad_links = []
        for i in range(0, len(toks), 500):
            docs = {d["_id"]: d async for d in db.links_col.find({"_id": {"$in": toks[i:i + 500]}}, {"ids": 1})}
            for t in toks[i:i + 500]:
                d = docs.get(t)
                if not d:
                    bad_links.append((t, "link record missing"))
                    continue
                ids = d.get("ids") or []
                bad = sum(1 for idx, k in by_tok[t] if k in done and (idx >= len(ids) or ids[idx] != done[k]))
                miss = sum(1 for idx, k in by_tok[t] if k not in done)
                if bad:
                    bad_links.append((t, f"{bad} slot(s) point to the wrong message"))
                elif miss:
                    bad_links.append((t, f"{miss} file(s) not restored"))

        # do the new messages really exist in the channel?
        gone = 0
        checked = 0
        ids = list(done.values())
        try:
            for i in range(0, len(ids), 200):
                for m in await get_messages(client, self.job["channel"]["id"], ids[i:i + 200]):
                    checked += 1
                    if m is None or getattr(m, "empty", False):
                        gone += 1
        except Exception as ex:
            log.warning("Message check failed: %s", ex)
            checked = -1

        c = self.counts
        deleted = self.job.get("parts_deleted", [])
        title = "Recover Stopped" if stopped else ("Recover Complete" if not failed and not bad_links else "Recover Finished")
        icon = "⏹" if stopped else ("✅" if not failed and not bad_links else "⚠️")
        lines = [f"RESTORED ▸ <b>{c['restored']:,}</b>", f"SKIPPED (ALREADY DONE) ▸ <b>{c['skipped']:,}</b>",
                 f"FAILED ▸ <b>{len(failed):,}</b>", f"FILES IN SELECTED PARTS ▸ <b>{total:,}</b>",
                 f"LINKS THAT RESOLVE ▸ <b>{len(tokens) - len(bad_links):,}/{len(tokens):,}</b>"]
        if checked >= 0:
            lines.append(f"MESSAGES FOUND IN THE CHANNEL ▸ <b>{checked - gone:,}/{checked:,}</b>")
        if deleted:
            lines.append(f"PARTS REMOVED FROM THE VPS ▸ <b>{len(deleted)}</b>")
        for note in self.job.get("notes", []):
            lines.append(f"ℹ️ {note}")
        texts = [head(icon, title, "\n".join(lines))]
        if failed:
            body = "\n".join(f"<code>{esc(d['key'])}</code> — {esc(d.get('reason', '?'))}" for d in failed[:15])
            more = f"\n… +{len(failed) - 15} MORE (SEE THE FILE)" if len(failed) > 15 else ""
            texts.append(head("⚠️", "Failed Files", body + more + "\n\n" +
                              hl("THEIR PARTS WERE KEPT. RUN <b>RECOVER</b> AGAIN TO RETRY ONLY THESE.")))
        if bad_links:
            texts.append(head("⚠️", "Links To Check", "\n".join(f"<code>{esc(t)}</code> — {esc(w)}" for t, w in bad_links[:15])))
        if stopped:
            texts.append(head("⏹", "Stopped", "TAP <b>RECOVER</b> AGAIN TO CONTINUE. FINISHED FILES ARE NEVER UPLOADED TWICE."))
        failed_file = ""
        if len(failed) > 15 or bad_links:
            failed_file = "\n".join([f"{d['key']}\t{d.get('reason', '?')}" for d in failed] +
                                    [f"LINK {t}\t{w}" for t, w in bad_links])
        return {"texts": texts, "failed_file": failed_file}

    # ---------------------------------------------------------- progress message
    def render(self):
        j = self.job or {}
        st = j.get("status")
        if st in ("done", "stopped", "failed"):
            icon, title = {"done": ("✅", "Recover Complete"), "stopped": ("⏹", "Recover Stopped"),
                           "failed": ("⚠️", "Recover Stopped")}[st]
            desc = f"RESTORED ▸ <b>{getattr(self, 'counts', {}).get('restored', 0):,}</b>"
            if st == "failed" and j.get("error"):
                desc += f"\n<code>{esc(j['error'])}</code>"
            return head(icon, title, desc), K.kb([], K.ub("🔙 Back", "adm:lr"))
        c = getattr(self, "counts", {"restored": 0, "skipped": 0, "failed": 0})
        total = j.get("total_files", 0)
        done = c["restored"] + c["skipped"]
        labels = {"checking": "🔎 CHECKING", "importing": "🗄 IMPORTING DATABASE", "extracting": "📦 UNPACKING",
                  "uploading": "⬆️ UPLOADING"}
        step = self.live.get("step")
        thr = self.bytes_run / max(1.0, time.time() - self.t0) if self.t0 else 0
        left = max(0, total - done - c["failed"])
        per = (time.time() - self.t0) / max(1, c["restored"]) if c["restored"] else None
        eta = per * left if per else None
        lines = [f"PART ▸ <b>{self.live.get('part', '…')}</b>",
                 f"RESTORED ▸ <b>{done:,}/{total:,}</b> · FAILED ▸ <b>{c['failed']:,}</b>",
                 f"STEP ▸ <b>{labels.get(step, step or '…')}</b>",
                 f"SPEED ▸ <b>{C.human(self.meter.speed() or thr)}/S</b> · ETA <b>{C.eta_text(eta)}</b>"]
        if self.stop_flag:
            lines.append("⏹ STOPPING…")
        return (head("♻️", "Recover From Zip", "\n".join(lines)),
                K.kb([], None if self.stop_flag else K.ub("⏹ Stop Recover", "adm:lrstop")))

    async def _render_now(self):
        panel = (self.job or {}).get("panel")
        if not panel or not self.bot:
            return
        text, markup = self.render()
        try:
            await self.bot.edit_message_text(text, chat_id=panel[0], message_id=panel[1], reply_markup=markup)
        except TelegramError as e:
            if "not found" in str(e).lower():
                try:
                    m = await self.bot.send_message(config.OWNER_ID, text, reply_markup=markup)
                    await self._save(panel=[m.chat_id, m.message_id])
                except TelegramError:
                    pass

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


restorer = RestoreRunner()


async def on_startup(bot):
    """Resume a recover that was interrupted by a restart."""
    try:
        await RITEMS.create_index("job")
        await restorer.load()
    except Exception:
        log.exception("Recover: could not load the job")
        return
    restorer.bot = bot
    j = restorer.job
    if j and j.get("status") == "running":
        log.info("Recover: resuming job %s", j.get("job_id"))
        await restorer._say(head("♻️", "Recover Resumed", "THE BOT RESTARTED. CONTINUING WHERE IT STOPPED.\n"
                                 "FILES ALREADY RESTORED ARE SKIPPED."))
        await restorer.resume_after_restart(bot)
