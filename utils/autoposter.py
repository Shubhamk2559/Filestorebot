"""
utils/autoposter.py
Autoposter engine (no UI here - the panel lives in handlers/autoposter.py).

  parse_slots / fmt_slots   "09:00 2" style timing input
  item_from_ptb / item_from_pyro   one source message -> a small dict
  group_posts               caption file = thumbnail, captionless files below it = the post's files
  group_pairs               "Image + Video" type: image 1, video 1, image 2, video 2 ... -> one post per pair
  create_posts              copy everything into the Database Channel (with backup codes), make links + post records
  send_post                 publish one post (thumbnail + caption + "Get Files" button) to a channel / group
  ensure_queue              shuffled queue: every post once per cycle in random order (loop = reshuffle each cycle,
                            one pass = pause when every post is posted); new posts are mixed into the running cycle
  next_slot                 next upcoming timing (for the Queue screen)
  autopost_loop             background scheduler (all times in IST)
"""
import asyncio
import datetime as dt
import html
import logging
import re
import time
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.constants import ParseMode
from telegram.error import TelegramError

from database import db
from database.db import settings as S
from utils.helpers import call, notify_owner
from utils.links import share_link
from utils import preview as PV
from utils import batch as BT

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MEDIA = ("document", "video", "audio", "photo", "animation", "voice", "video_note", "sticker")
NO_CAPTION = ("video_note", "sticker")      # Telegram cannot attach a caption to these -> no backup code
CAPTION_MAX = 1024
MAX_PER_SLOT = 50
SLOT_GRACE = 30 * 60        # a slot is still run up to 30 min late (bot was restarting); after that it is skipped
COPY_DELAY = 0.15           # seconds between copies into the Database Channel (flood safety)
POST_DELAY = 3              # seconds between two posts of the same slot

_tasks: set = set()


def spawn(coro):
    """Run a coroutine in the background and keep a reference so it is not garbage-collected."""
    t = asyncio.ensure_future(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


# ------------------------------------------------------------------ timings
_SPLIT = re.compile(r"[\n,;]+")
_SLOT = re.compile(r"^(\d{1,2})[:.](\d{2})(?:\s*(?:x|×|\*)?\s*(\d{1,3}))?$", re.I)


def parse_slots(text: str) -> list:
    """'09:00 2' / '14:30' / '21:00 x3', one per line or comma separated -> [{"t": "09:00", "n": 2}, ...]"""
    out = []
    for part in _SPLIT.split(text or ""):
        part = part.strip()
        if not part:
            continue
        m = _SLOT.match(part)
        if not m:
            raise ValueError(f"Cannot read: {part}")
        h, mi, n = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
        if h > 23 or mi > 59:
            raise ValueError(f"Invalid time: {part}")
        if not 1 <= n <= MAX_PER_SLOT:
            raise ValueError(f"Posts per time must be 1-{MAX_PER_SLOT}: {part}")
        out.append({"t": f"{h:02d}:{mi:02d}", "n": n})
    if not out:
        raise ValueError("No time found")
    return out


def fmt_slots(slots: list) -> str:
    return ", ".join(f"{s['t']} ×{s['n']}" for s in sorted(slots, key=lambda s: s["t"])) or "not set"


# ------------------------------------------------------------------ source messages -> items
def item_from_ptb(msg, kind: str, name: str) -> dict:
    """A file the admin sent / forwarded to the bot."""
    return {"chat": msg.chat_id, "mid": msg.message_id, "kind": kind, "name": name,
            "cap": (msg.caption or ""), "cap_html": (msg.caption_html or ""),
            "gid": str(msg.media_group_id) if getattr(msg, "media_group_id", None) else None}


def item_from_pyro(m):
    """A pyrogram Message read from the source channel (range mode). None = not a file."""
    if m is None or getattr(m, "empty", False) or not getattr(m, "media", None) or not m.chat:
        return None
    kind = getattr(m.media, "value", str(m.media))
    if kind not in MEDIA:
        return None
    cap = m.caption
    obj = getattr(m, kind, None)
    name = None if kind == "photo" else getattr(obj, "file_name", None)
    plain = str(cap) if cap else ""
    return {"chat": m.chat.id, "mid": m.id, "kind": kind, "name": name or plain[:40] or kind,
            "cap": plain, "cap_html": (cap.html if cap else ""),
            "gid": str(m.media_group_id) if getattr(m, "media_group_id", None) else None}


def group_posts(items: list):
    """
    A file WITH a caption starts a new post and is its thumbnail.
    Every file WITHOUT a caption below it belongs to that post, until the next captioned file.
    A captionless album below a captioned file = files of that post (all photos in the same link).
    An album that itself has a caption = its own post.
    Returns (posts, orphans): orphans = captionless files that came before the first caption.
    """
    albums = {}
    for it in items:
        if it.get("gid"):
            albums.setdefault(it["gid"], []).append(it)
    posts, cur, orphans, done = [], None, 0, set()
    for it in items:
        gid = it.get("gid")
        # a captioned album = ONE post (all its files in one link, the captioned item is the thumbnail)
        if gid and gid not in done and any(x["cap"].strip() for x in albums[gid]):
            done.add(gid)
            files = albums[gid]
            thumb = next(x for x in files if x["cap"].strip())
            cur = {"thumb": thumb, "files": list(files), "album": True}
            posts.append(cur)
        elif gid in done:
            continue
        elif it["cap"].strip():
            cur = {"thumb": it, "files": []}
            posts.append(cur)
        elif cur is not None:          # captionless file / album -> files of the post above it
            cur["files"].append(it)
        else:
            orphans += 1
    return posts, orphans


IMG_KINDS = ("photo",)
VID_KINDS = ("video", "animation", "document")


def group_pairs(items: list):
    """
    "Image + Video" type: the source is  image 1, video 1, image 2, video 2 ...
    (image N + video N) = ONE post with two links: the image (free preview) and the video (ad gate).
    Returns (posts, orphans). Each post = {"thumb": image item, "files": [video item], "pair": True}.
    An image not followed by a video, or a video without an image above it, is an orphan (ignored).
    """
    posts, img, orphans = [], None, 0
    for it in items:
        if it["kind"] in IMG_KINDS:
            if img is not None:
                orphans += 1                    # the previous image never got its video
            img = it
        elif it["kind"] in VID_KINDS:
            if img is None:
                orphans += 1                    # a video without an image above it
                continue
            posts.append({"thumb": img, "files": [it], "pair": True})
            img = None
        else:
            orphans += 1
    if img is not None:
        orphans += 1
    return posts, orphans


# ------------------------------------------------------------------ post caption / button (set from the admin panel)
DEFAULT_BTN = "📥 Get Files"
DEFAULT_BTN_IMG = "🖼 Preview"
DEFAULT_BTN_VID = "🎬 Watch Video"
BTN_MAX = 40


def _plain_len(h: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", h)))


def render_caption(tpl: str, cap_html: str) -> str:
    """ONE fixed caption for every post, exactly as the admin wrote it (no placeholder, the post's own
    caption is NOT mixed in). Only if no fixed caption is set, the post's own caption is used."""
    tpl = (tpl or "").replace("{caption}", "").strip()
    if not tpl:
        return cap_html
    if _plain_len(tpl) <= CAPTION_MAX:
        return tpl
    return tpl[:CAPTION_MAX]


# ------------------------------------------------------------------ storing into the Database Channel
def _caption_with_code(it: dict, code: str) -> str:
    tag = f"<code>{code}</code>"
    orig, plain = it["cap_html"].strip(), it["cap"].strip()
    if len(plain) + len(code) + 2 <= CAPTION_MAX:
        return f"{orig}\n\n{tag}" if orig else tag
    room = max(0, CAPTION_MAX - len(code) - 2)
    return f"{html.escape(plain[:room])}\n\n{tag}"


async def _store(bot, channel_id: int, it: dict, code: str, by: int) -> int:
    """Copy one source message into the Database Channel under a backup code. Returns the new message id."""
    caption = None if it["kind"] in NO_CAPTION else _caption_with_code(it, code)
    kw = {"caption": caption, "parse_mode": ParseMode.HTML} if caption is not None else {}
    copied = await call(bot.copy_message, channel_id, it["chat"], it["mid"], **kw)
    await db.add_file(copied.message_id, it["kind"], it["name"], by,
                      code=code if caption is not None else None, cap=it["cap_html"])
    await asyncio.sleep(COPY_DELAY)
    return copied.message_id


async def create_posts(bot, items: list, cid: int, by: int, status_chat: int, status_msg: int):
    """Group `items` into posts, store them in the Database Channel and register them for channel `cid`."""
    channel = S.get("db_channel")
    kb = M([[B("📺 Open channel", callback_data=f"adm:apc:{cid}")]])
    if not channel:
        await bot.edit_message_text("⚠️ Database Channel is not set.", chat_id=status_chat,
                                    message_id=status_msg, reply_markup=kb)
        return

    ch = await db.ap_get_channel(cid) or {}
    pair_mode = ch.get("ptype") == "pair"
    items = sorted(items, key=lambda i: i["mid"])
    posts, orphans = group_pairs(items) if pair_mode else group_posts(items)
    total_files = sum(1 + len(p["files"]) for p in posts if p["files"])
    seq = await db.ap_last_seq(cid)
    made = skipped = copied_files = 0
    new_tokens = []
    error = ""
    last_edit = 0.0

    async def progress(final=False):
        nonlocal last_edit
        if not final and time.time() - last_edit < 3:
            return
        last_edit = time.time()
        head = "⏳ <b>Creating posts…</b>" if not final else ("✅ <b>Posts created</b>" if not error else "⚠️ <b>Stopped</b>")
        text = (f"{head}\n\n📝 Posts: {made}\n📁 Files stored: {copied_files}/{total_files}\n")
        if skipped:
            text += f"⏭ Skipped (caption but no files below): {skipped}\n"
        if orphans:
            text += (f"⏭ Files without their image / video partner (ignored): {orphans}\n" if pair_mode
                     else f"⏭ Files before the first caption (ignored): {orphans}\n")
        if error:
            text += f"\n❌ <code>{html.escape(error)}</code>"
        try:
            await bot.edit_message_text(text, chat_id=status_chat, message_id=status_msg,
                                        reply_markup=kb if final else None)
        except TelegramError:
            pass

    for p in posts:
        if not p["files"]:
            skipped += 1
            continue
        if p.get("pair"):
            # image -> free link (token), video -> ad-gated link (vtoken); both stored with their own backup code
            img, vid = p["thumb"], p["files"][0]
            token, vtoken = db.new_token(), db.new_token()
            try:
                img_id = await _store(bot, channel["id"], img, db.make_code(token, 0), by)
                copied_files += 1
                vid_id = await _store(bot, channel["id"], vid, db.make_code(vtoken, 0), by)
                copied_files += 1
            except TelegramError as e:
                error = str(e)
                break
            cap_html = img["cap_html"] if img["cap"].strip() else vid["cap_html"]
            plain = img["cap"].strip() or vid["cap"].strip()
            title = re.sub(r"\s+", " ", plain).strip()[:40] or "Image + Video"
            preview_url, image_url = await PV.make_preview_url(channel["id"], img_id, title)   # imgbb -> Telegraph (None = off / failed)
            await db.create_link([img_id], by, title + " (preview)", token=token, no_ads=True)
            await db.create_link([vid_id], by, title + " (video)", token=vtoken)
            seq += 1
            await db.ap_add_post(cid, token, None, seq, title, cap_html, "photo", 2, by,
                                 ptype="pair", vtoken=vtoken, preview_url=preview_url, image_url=image_url)
            made += 1
            new_tokens.append(token)
            await progress()
            continue
        token, thumb_token = db.new_token(), db.new_token()
        ids = []
        try:
            for i, f in enumerate(p["files"]):
                ids.append(await _store(bot, channel["id"], f, db.make_code(token, i), by))
                copied_files += 1
                await progress()
            # the thumbnail is saved too, so the post can be rebuilt even if the bot / source channel is gone
            await _store(bot, channel["id"], p["thumb"], db.make_code(thumb_token, 0), by)
            copied_files += 1
        except TelegramError as e:
            error = str(e)
            break
        th = p["thumb"]
        title = re.sub(r"\s+", " ", th["cap"]).strip()[:40] or ("Album" if p.get("album") else "Post")
        await db.create_link(ids, by, title, token=token)
        seq += 1
        await db.ap_add_post(cid, token, thumb_token, seq, title, th["cap_html"], th["kind"], len(ids), by)
        made += 1
        new_tokens.append(token)
        await progress()
    await db.ap_enqueue_new(cid, new_tokens)      # mix the new posts into the running shuffled cycle
    await progress(final=True)


# ------------------------------------------------------------------ publishing
async def send_post(bot, chat_id: int, post: dict, mark: bool = True):
    """Publish one post to the channel / group. Returns None on success, else an error text.
    mark=False: the caller (the queue) records the post as posted itself."""
    channel = S.get("db_channel")
    if not channel:
        return "Database Channel is not set"
    ch = await db.ap_get_channel(chat_id) or {}
    if post.get("ptype") == "batch":
        return await _send_batch(bot, chat_id, channel, post)
    if post.get("ptype") == "pair":
        thumb, kb = await _pair_media(bot, ch, post)
    else:
        thumb = await db.thumb_msg_id(db.make_code(post["thumb_token"], 0))
        btn = (ch.get("btn") or "").strip() or DEFAULT_BTN
        kb = M([[B(btn, url=share_link(bot.username, post["_id"]))]])
    if not thumb:
        return ("fixed thumbnail not set (Caption & Button -> Fixed Thumbnail)" if post.get("ptype") == "pair"
                else "thumbnail not found in the Database Channel")
    try:
        await call(bot.copy_message, chat_id, channel["id"], thumb,
                   caption=render_caption(ch.get("cap_tpl"), "" if post.get("ptype") == "pair" else post["cap"]),
                   parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as e:
        return str(e)
    if mark:
        await db.ap_mark_posted(post["_id"])
    return None


_numlocks: dict = {}


def _fit(cap: str) -> str:
    """Keep a caption inside Telegram's limit (after the tags were replaced it can be a bit longer)."""
    if _plain_len(cap) <= CAPTION_MAX:
        return cap
    plain = html.unescape(re.sub(r"<[^>]+>", "", cap))
    return html.escape(plain[:CAPTION_MAX])


async def _send_batch(bot, chat_id: int, channel: dict, post: dict):
    """Folder Batch post: the captioned photo + the channel's fixed caption. The tags {Preview Image} / {Video Link} /
    {Post Number} are replaced ONLY when the channel's type is Folder Batch (otherwise the text goes out as written).
    The post number is taken now, at posting time: last number + 1. It is saved only after Telegram accepted the
    post, and the whole step is serialised per channel -> never a repeat, never a gap, a failed send reuses it."""
    if post.get("fixed_thumb"):                # folder without a captioned photo -> the channel's fixed thumbnail
        ftok = (await db.ap_get_channel(chat_id) or {}).get("fthumb")
        if not ftok:
            return "fixed thumbnail not set (Caption & Button -> Fixed Thumbnail)"
        thumb = await db.thumb_msg_id(db.make_code(ftok, 0))
    else:
        thumb = await db.thumb_msg_id(db.make_code(post["thumb_token"], 0))
    if not thumb:
        return "thumbnail not found in the Database Channel"
    async with _numlocks.setdefault(chat_id, asyncio.Lock()):
        ch = await db.ap_get_channel(chat_id) or {}
        tags_on = ch.get("ptype") == "batch"
        number = int(ch.get("post_no") or 0) + 1
        cap = render_caption(ch.get("cap_tpl"), post["cap"])
        if tags_on:
            video = share_link(bot.username, post["vtoken"]) if post.get("vtoken") else ""
            cap = BT.apply_tags(cap, post.get("tg_url") or "", video, number)
        try:
            await call(bot.copy_message, chat_id, channel["id"], thumb, caption=_fit(cap), parse_mode=ParseMode.HTML)
        except TelegramError as e:
            return str(e)
        await BT.after_posted(chat_id, post, number if tags_on else None)
    return None


async def _pair_media(bot, ch: dict, post: dict):
    """(DB-channel message id to show, keyboard) of an Image + Video post.
    The picture = the channel's fixed thumbnail only (the post's own image is never used). Two buttons: preview + video."""
    thumb = None
    if ch.get("fthumb"):
        thumb = await db.thumb_msg_id(db.make_code(ch["fthumb"], 0))      # ONLY the fixed thumbnail, never the post's image
    b_img = (ch.get("btn_img") or "").strip() or DEFAULT_BTN_IMG
    b_vid = (ch.get("btn_vid") or "").strip() or DEFAULT_BTN_VID
    # Telegraph page while it (and its imgbb picture) still works, else the bot link - the image is always backed up
    # in the Database Channel behind that link, so the Preview button never dies.
    img_url = post.get("preview_url")
    if not img_url or not await PV.alive(img_url, post.get("image_url")):
        img_url = share_link(bot.username, post["_id"])
    kb = M([[B(b_img, url=img_url),
             B(b_vid, url=share_link(bot.username, post["vtoken"]))]])
    return thumb, kb


_locks: dict = {}


def _lock(cid: int) -> asyncio.Lock:
    return _locks.setdefault(cid, asyncio.Lock())


async def ensure_queue(cid: int, ch: dict):
    """Makes sure a cycle is running. Loop mode: a finished cycle is reshuffled with ALL posts.
    One pass: only the posts that are not posted yet are queued."""
    qs = ch.get("queue_state") or {}
    queue = qs.get("queue") or []
    if queue and int(qs.get("cursor", 0)) < len(queue):
        return ch
    loop = ch.get("loop", True)
    if loop and int(qs.get("cycle", 0)) > 0:
        return await db.ap_rebuild_cycle(cid)
    ch = await db.ap_rebuild_cycle(cid, only_unused=True)       # first start: keeps what was already posted
    if ch and loop and not (ch.get("queue_state") or {}).get("queue") and (await db.ap_counts(cid))[0]:
        return await db.ap_rebuild_cycle(cid)                   # everything was posted already -> new shuffled cycle
    return ch


def next_slot(slots: list):
    """(datetime, slot) of the next upcoming timing in IST, or None."""
    now = dt.datetime.now(IST)
    best = None
    for sl in slots or []:
        h, m = map(int, sl["t"].split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += dt.timedelta(days=1)
        if best is None or t < best[0]:
            best = (t, sl)
    return best


async def run_slot(bot, ch: dict, n: int):
    cid = ch["_id"]
    async with _lock(cid):
        ch = await db.ap_get_channel(cid)
        if not ch or not ch.get("enabled", True):
            return
        loop = ch.get("loop", True)
        if ch.get("ptype") == "batch":
            await _run_slot_batch(bot, ch)
            return
        ch = await ensure_queue(cid, ch)
        if not ch:
            return
        sent = 0
        while sent < n:
            ch = await db.ap_get_channel(cid)
            if not ch or not ch.get("enabled", True):
                return
            qs = ch.get("queue_state") or {}
            queue, cursor = qs.get("queue") or [], int(qs.get("cursor", 0))
            if cursor >= len(queue):
                break                                   # cycle finished: the next time reshuffles (loop)
            post = await db.ap_get_post(queue[cursor])
            if not post or post.get("chan") != cid or post.get("posted"):
                await db.ap_skip_cursor(cid)            # deleted, or already posted (e.g. with Post now)
                continue
            err = await send_post(bot, cid, post, mark=False)
            if err:
                await notify_owner(bot, f"ap_err_{cid}",
                                   f"⚠️ Autoposter: cannot post in <b>{html.escape(ch['title'])}</b>:\n"
                                   f"<code>{html.escape(err)}</code>", every=1800)
                if err.startswith("thumbnail not found"):
                    await db.ap_skip_cursor(cid)        # this post can never be sent -> do not block the queue
                    continue
                break
            await db.ap_advance(cid, post["_id"])
            sent += 1
            await asyncio.sleep(POST_DELAY)

        ch = await db.ap_get_channel(cid) or {}
        qs = ch.get("queue_state") or {}
        queue, cursor = qs.get("queue") or [], int(qs.get("cursor", 0))
        finished = cursor >= len(queue)
        await db.ap_finalize(cid, sent, finished and bool(queue))
        if finished and not loop and (await db.ap_counts(cid))[0]:
            await db.ap_set(cid, enabled=False)
            await notify_owner(bot, f"ap_done_{cid}",
                               f"⏸ Autoposter: every post of <b>{html.escape(ch.get('title', ''))}</b> is posted "
                               "(one pass). Create more posts and turn it ON, or reshuffle to start over.", every=60)


async def _run_slot_batch(bot, ch: dict):
    """Folder Batch channel: at most ONE post per time. Posts of the same folder keep the gap (30+ days, random jitter),
    and any two posts of the channel are at least `min_hours` apart - so nothing ever goes out together."""
    cid = ch["_id"]
    post, why = await BT.pick_post(cid, ch)
    if not post:
        if why == "empty":
            total = (await db.ap_counts(cid))[0]
            busy = (await BT.counts(cid))[0]
            if total and not busy:
                if ch.get("loop", True):
                    await BT.restart_cycle(cid, ch)
                else:
                    await db.ap_set(cid, enabled=False)
                    await notify_owner(bot, f"ap_done_{cid}",
                                       f"⏸ Autoposter: every post of <b>{html.escape(ch.get('title', ''))}</b> is posted "
                                       "(one pass). Create more posts and turn it ON.", every=60)
        return
    err = await send_post(bot, cid, post, mark=False)
    if err:
        await notify_owner(bot, f"ap_err_{cid}",
                           f"⚠️ Autoposter: cannot post in <b>{html.escape(ch['title'])}</b>:\n"
                           f"<code>{html.escape(err)}</code>", every=1800)


async def autopost_loop(bot):
    await asyncio.sleep(10)
    while True:
        try:
            now = dt.datetime.now(IST)
            today = now.strftime("%Y-%m-%d")
            for ch in await db.ap_channels_col.find({"enabled": True}).to_list(500):
                for slot in ch.get("slots", []):
                    h, m = map(int, slot["t"].split(":"))
                    late = (now - now.replace(hour=h, minute=m, second=0, microsecond=0)).total_seconds()
                    if 0 <= late <= SLOT_GRACE and await db.ap_claim_slot(ch["_id"], f"{today} {slot['t']}"):
                        spawn(run_slot(bot, ch, int(slot.get("n", 1))))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Autoposter loop error")
        await asyncio.sleep(20)
