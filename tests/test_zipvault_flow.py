"""Offline flow test of the Vault Pack ZIP engine with a fake database (python3 tests/test_zipvault_flow.py).
Checks: worker step (videos -> ONE link, images -> imgbb original bytes -> Telegraph, status need_thumb, files removed),
resume after a pause, thumbnails -> posts (1 thumbnail = 1 post, none = fixed thumbnail), double tap safety."""
import asyncio
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
src = open(os.path.join(HERE, "test_ui.py"), encoding="utf-8").read()
exec(compile(src.split("import config  # noqa: E402")[0], "stubs", "exec"))      # reuse the telegram / motor stubs

import config  # noqa: E402,F401
from database import db  # noqa: E402
from database.db import settings as S  # noqa: E402
from utils import batch as BT  # noqa: E402
from utils import zipvault as ZV  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def match(doc, flt):
    for k, v in flt.items():
        if isinstance(v, dict):
            for op, x in v.items():
                if op == "$in" and doc.get(k) not in x:
                    return False
                if op == "$nin" and doc.get(k) in x:
                    return False
                if op == "$ne" and doc.get(k) == x:
                    return False
        elif doc.get(k) != v:
            return False
    return True


class Res:
    def __init__(self, n):
        self.modified_count = n


class Col:
    def __init__(self):
        self.docs = []

    async def insert_one(self, d):
        self.docs.append(d)

    async def find_one(self, flt, sort=None):
        r = [d for d in self.docs if match(d, flt)]
        if sort:
            r.sort(key=lambda d: d.get(sort[0][0], 0))
        return r[0] if r else None

    async def update_one(self, flt, upd):
        for d in self.docs:
            if match(d, flt):
                d.update(upd.get("$set", {}))
                return Res(1)
        return Res(0)

    async def count_documents(self, flt):
        return len([d for d in self.docs if match(d, flt)])


async def main():
    S._data = __import__("copy").deepcopy(db.DEFAULTS)
    S._data["db_channel"] = {"id": -100, "title": "Store"}
    S._data["imgbb_keys"] = ["k" * 20]
    S.set = AsyncMock()
    batches, posts = Col(), []
    db.ap_batches_col = batches
    db.ap_get_channel = AsyncMock(return_value={"_id": 5, "fthumb": "FT", "tg_title": "{folder_name}"})
    db.ap_last_seq = AsyncMock(return_value=0)
    db.ap_add_post = AsyncMock(side_effect=lambda cid, tok, tt, seq, title, cap, kind, n, by, **kw: posts.append(
        dict(tok=tok, thumb=tt, seq=seq, title=title, n=n, **kw)))
    db.ap_enqueue_new = AsyncMock()
    db.create_link = AsyncMock()
    db.add_file = AsyncMock()
    BT.CD_MIN = BT.CD_MAX = 0
    ZV.IMG_PARALLEL = 1

    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "Wedding"))
        for n, data in (("1.jpg", b"ORIGINAL-1"), ("2.jpg", b"ORIGINAL-2"), ("3.png", b"ORIGINAL-3")):
            open(os.path.join(root, "Wedding", n), "wb").write(data)
        open(os.path.join(root, "Wedding", "c.mp4"), "wb").write(b"video")
        b = {"_id": "B1", "chan": 5, "name": "Wedding", "by": 7, "status": "pending", "src": "zip", "dir": root,
             "imgs": [{"rel": f"Wedding/{n}"} for n in ("1.jpg", "2.jpg", "3.png")], "vids": [{"rel": "Wedding/c.mp4"}],
             "items": [], "img_urls": {}, "img_failed": [], "img_tries": {}, "stored": [], "vtoken": None,
             "tg_url": None, "next_try": 0, "error": "", "created": 1}
        await batches.insert_one(b)

        uploaded, calls = [], {"n": 0}

        async def fake_upload(session, data):
            calls["n"] += 1
            if calls["n"] == 2 and not calls.get("failed"):          # imgbb breaks once on the 2nd image -> pause
                calls["failed"] = True
                raise BT.NoKeyAvailable("rate limit")
            uploaded.append(data)
            return f"https://i.ibb.co/x/{len(uploaded)}.jpg"

        BT.upload = fake_upload
        ZV.store_video = AsyncMock(return_value=901)
        ZV._telegraph = AsyncMock(return_value="https://telegra.ph/Wedding-01-01")
        ZV._notify = AsyncMock()

        try:
            await ZV.process(MagicMock(), dict(b))
            check(False, "a failing imgbb upload must pause the folder (raise)")
        except BT.NoKeyAvailable:
            pass
        saved = await batches.find_one({"_id": "B1"})
        check(saved["link_done"] and saved["stored"] == [901], "video link created once, before the images")
        check(saved["status"] == "working" and len(saved["img_urls"]) == 1, "progress saved after every image")
        db.create_link.assert_awaited_once()
        check(not os.path.exists(os.path.join(root, "Wedding", "c.mp4")), "video file removed after upload")

        await ZV.process(MagicMock(), dict(saved))                    # resume
        done = await batches.find_one({"_id": "B1"})
        check(done["status"] == "need_thumb" and done["tg_url"], "folder waits for thumbnails")
        check(uploaded == [b"ORIGINAL-1", b"ORIGINAL-2", b"ORIGINAL-3"], f"original bytes, in order, once each: {uploaded}")
        check(ZV.store_video.await_count == 1 and db.create_link.await_count == 1, "video not stored twice on resume")
        check(ZV._telegraph.await_args.args[2] == [f"https://i.ibb.co/x/{i}.jpg" for i in (1, 2, 3)], "telegraph gets all images in order")
        check(not os.path.exists(root), "job directory removed when the folder is finished")
        ZV._notify.assert_awaited()

        # thumbnails -> posts
        ZV.store_thumb = AsyncMock()
        th = [{"kind": "photo", "cap": "", "cap_html": "", "mid": 1}, {"kind": "photo", "cap": "Hot", "cap_html": "<b>Hot</b>", "mid": 2}]
        n = await ZV.make_posts(MagicMock(), dict(done), th, 7)
        check(n == 2 and len(posts) == 2, f"2 thumbnails = 2 posts ({n})")
        check({p["tg_url"] for p in posts} == {done["tg_url"]} and {p["vtoken"] for p in posts} == {done["vtoken"]}, "same Telegraph + video link")
        check(posts[0]["thumb"] != posts[1]["thumb"] and posts[0]["n"] == 1 and posts[1]["seq"] == 2, "different thumbnails, numbered")
        check((await batches.find_one({"_id": "B1"}))["status"] == "done", "folder done")
        check(await ZV.make_posts(MagicMock(), dict(done), th, 7) == 0 and len(posts) == 2, "double tap creates nothing")

        # fixed thumbnail
        await batches.insert_one({"_id": "B2", "chan": 5, "name": "Puja", "by": 7, "status": "need_thumb", "src": "zip",
                                  "vids": [], "imgs": [{}], "tg_url": "https://telegra.ph/p", "vtoken": "", "created": 2})
        b2 = await ZV.next_waiting(5, [])
        check(b2["_id"] == "B2", "next waiting folder")
        check(await ZV.next_waiting(5, ["B2"]) is None, "skipped folder is not offered again")
        n = await ZV.make_posts(MagicMock(), b2, [], 7, fixed=True)
        check(n == 1 and posts[-1].get("fixed_thumb") and posts[-1]["thumb"] is None, "fixed thumbnail post")
        db.ap_get_channel = AsyncMock(return_value={"_id": 5})
        await batches.insert_one({"_id": "B3", "chan": 5, "name": "X", "by": 7, "status": "need_thumb", "src": "zip",
                                  "vids": [], "imgs": [], "created": 3})
        try:
            await ZV.make_posts(MagicMock(), await ZV.next_waiting(5, []), [], 7, fixed=True)
            check(False, "fixed thumbnail missing must raise")
        except RuntimeError:
            pass
        check((await batches.find_one({"_id": "B3"}))["status"] == "need_thumb", "folder stays waiting after an error")


        # ---- folder with ONLY videos: 10 frames -> imgbb (original bytes) -> Telegraph, post with the FIXED thumbnail
        n0 = len(batches.docs)
        ids = await ZV.queue_zip_folders(5, [{"name": "OnlyVid", "images": [], "videos": ["OnlyVid/v1.mp4"], "ignored": 0},
                                             {"name": "Nothing", "images": [], "videos": [], "ignored": 2},
                                             {"name": "Normal", "images": ["Normal/1.jpg"], "videos": [], "ignored": 0}],
                                         7, "/tmp/none", "pack.zip")
        check(len(ids) == 2 and len(batches.docs) == n0 + 2, "video-only + normal folder queued, empty folder skipped")
        qd = {d["name"]: d for d in batches.docs[n0:]}
        check(qd["OnlyVid"]["frames"] is True and qd["Normal"]["frames"] is False, "only the video-only folder asks for frames")
        batches.docs[n0:] = []                                                  # keep the rest of this test simple

        posts.clear()
        db.ap_get_channel = AsyncMock(return_value={"_id": 5, "fthumb": "FT", "tg_title": "{folder_name}"})
        with tempfile.TemporaryDirectory() as root2:
            os.makedirs(os.path.join(root2, "Clips"))
            for n in ("v1.mp4", "v2.mp4"):
                open(os.path.join(root2, "Clips", n), "wb").write(b"video-" + n.encode())
            b4 = {"_id": "B4", "chan": 5, "name": "Clips", "by": 7, "status": "pending", "src": "zip", "dir": root2,
                  "frames": True, "frames_done": False, "imgs": [], "vids": [{"rel": "Clips/v1.mp4"}, {"rel": "Clips/v2.mp4"}],
                  "items": [], "img_urls": {}, "img_failed": [], "img_tries": {}, "stored": [], "vtoken": None,
                  "tg_url": None, "next_try": 0, "error": "", "created": 9}
            await batches.insert_one(b4)
            seen_paths = {}

            def fake_grab(paths, out_dir, count, cancel=None, seed=None):
                seen_paths["videos_exist"] = all(os.path.exists(p) for p in paths)       # frames BEFORE videos are removed
                os.makedirs(out_dir)
                out = []
                for i in range(count):
                    p = os.path.join(out_dir, f"frame_{i + 1:02d}.png")
                    open(p, "wb").write(f"FRAME-{i}".encode())
                    out.append(p)
                return out

            ZV.FG.grab = fake_grab
            got = []

            async def up2(session, data):
                got.append(data)
                return f"https://i.ibb.co/f/{len(got)}.png"

            BT.upload = up2
            ZV.store_video = AsyncMock(return_value=902)
            ZV._telegraph = AsyncMock(return_value="https://telegra.ph/Clips-01-01")
            ZV._notify = AsyncMock()
            await ZV.process(MagicMock(), dict(b4))
            d4 = await batches.find_one({"_id": "B4"})
            check(seen_paths.get("videos_exist"), "frames are captured before the videos are uploaded / deleted")
            check(got == [f"FRAME-{i}".encode() for i in range(10)], f"10 frames uploaded untouched, in order ({len(got)})")
            check(ZV._telegraph.await_args.args[2] == [f"https://i.ibb.co/f/{i}.png" for i in range(1, 11)], "telegraph gets the 10 frames")
            check(ZV.store_video.await_count >= 2 and d4["link_done"], "both videos stored behind one link")
            check(d4["status"] == "done" and d4["frames_done"] and len(d4["imgs"]) == 10, f"folder finished by itself ({d4['status']})")
            check(len(posts) == 1 and posts[0].get("fixed_thumb") and posts[0]["thumb"] is None, "ONE post with the fixed thumbnail")
            check(posts[0]["tg_url"] == "https://telegra.ph/Clips-01-01" and posts[0]["n"] == 2, "post has the preview link + 2 files")
            check(not os.path.exists(root2), "frames + videos removed from disk")

        # no fixed thumbnail set -> the folder waits for a manual thumbnail instead of failing
        posts.clear()
        db.ap_get_channel = AsyncMock(return_value={"_id": 5, "tg_title": "{folder_name}"})
        with tempfile.TemporaryDirectory() as root3:
            os.makedirs(os.path.join(root3, "Clips2"))
            open(os.path.join(root3, "Clips2", "v.mp4"), "wb").write(b"v")
            await batches.insert_one({"_id": "B5", "chan": 5, "name": "Clips2", "by": 7, "status": "pending", "src": "zip",
                                      "dir": root3, "frames": True, "frames_done": False, "imgs": [], "vids": [{"rel": "Clips2/v.mp4"}],
                                      "items": [], "img_urls": {}, "img_failed": [], "img_tries": {}, "stored": [], "vtoken": None,
                                      "tg_url": None, "next_try": 0, "error": "", "created": 10})
            await ZV.process(MagicMock(), dict(await batches.find_one({"_id": "B5"})))
            check((await batches.find_one({"_id": "B5"}))["status"] == "need_thumb" and not posts, "no fixed thumbnail: folder waits, no post")

        # frames cannot be captured (e.g. broken videos) -> clear failure, not a crash
        with tempfile.TemporaryDirectory() as root4:
            os.makedirs(os.path.join(root4, "Bad"))
            open(os.path.join(root4, "Bad", "v.mp4"), "wb").write(b"v")
            ZV.FG.grab = lambda *a, **k: []
            await batches.insert_one({"_id": "B6", "chan": 5, "name": "Bad", "by": 7, "status": "pending", "src": "zip",
                                      "dir": root4, "frames": True, "frames_done": False, "imgs": [], "vids": [{"rel": "Bad/v.mp4"}],
                                      "items": [], "img_urls": {}, "img_failed": [], "img_tries": {}, "stored": [], "vtoken": None,
                                      "tg_url": None, "next_try": 0, "error": "", "created": 11})
            await ZV.process(MagicMock(), dict(await batches.find_one({"_id": "B6"})))
            d6 = await batches.find_one({"_id": "B6"})
            check(d6["status"] == "failed" and "frame" in d6["error"], f"unreadable videos -> failed folder ({d6['status']})")

asyncio.run(main())
print("FAILED:\n" + "\n".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
