"""
utils/autoposter.py
Autoposter engine (no UI here - the panel lives in handlers/autoposter.py).

  parse_slots / fmt_slots   "09:00 2" style timing input
  item_from_ptb / item_from_pyro   one source message -> a small dict
  group_posts               caption file = thumbnail, captionless files below it = the post's files
  create_posts              copy everything into the Database Channel (with backup codes), make links + post records
  send_post                 publish one post (thumbnail + caption + "Get Files" button) to a channel / group
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


# ------------------------------------------------------------------ post caption / button (set from the admin panel)
DEFAULT_BTN = "📥 Get Files"
BTN_MAX = 40


def _plain_len(h: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", h)))


def render_caption(tpl: str, cap_html: str) -> str:
    """Fill the channel's caption template. {caption} = the post's own caption; no {caption} = a fixed caption."""
    tpl = (tpl or "").strip() or "{caption}"
    out = tpl.replace("{caption}", cap_html)
    if _plain_len(out) <= CAPTION_MAX:
        return out
    plain = html.unescape(re.sub(r"<[^>]+>", "", cap_html))
    room = max(0, CAPTION_MAX - _plain_len(tpl.replace("{caption}", "")))
    return tpl.replace("{caption}", html.escape(plain[:room]))


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

    posts, orphans = group_posts(sorted(items, key=lambda i: i["mid"]))
    total_files = sum(1 + len(p["files"]) for p in posts if p["files"])
    seq = await db.ap_last_seq(cid)
    made = skipped = copied_files = 0
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
            text += f"⏭ Files before the first caption (ignored): {orphans}\n"
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
        await progress()
    await progress(final=True)


# ------------------------------------------------------------------ publishing
async def send_post(bot, chat_id: int, post: dict):
    """Publish one post to the channel / group. Returns None on success, else an error text."""
    channel = S.get("db_channel")
    if not channel:
        return "Database Channel is not set"
    thumb = await db.thumb_msg_id(db.make_code(post["thumb_token"], 0))
    if not thumb:
        return "thumbnail not found in the Database Channel"
    ch = await db.ap_get_channel(chat_id) or {}
    btn = (ch.get("btn") or "").strip() or DEFAULT_BTN
    kb = M([[B(btn, url=share_link(bot.username, post["_id"]))]])
    try:
        await call(bot.copy_message, chat_id, channel["id"], thumb,
                   caption=render_caption(ch.get("cap_tpl"), post["cap"]),
                   parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as e:
        return str(e)
    await db.ap_mark_posted(post["_id"])
    return None


async def run_slot(bot, ch: dict, n: int):
    cid = ch["_id"]
    for p in await db.ap_pending(cid, n):
        err = await send_post(bot, cid, p)
        if err:
            await notify_owner(bot, f"ap_err_{cid}",
                               f"⚠️ Autoposter: cannot post in <b>{html.escape(ch['title'])}</b>:\n"
                               f"<code>{html.escape(err)}</code>", every=1800)
            return
        await asyncio.sleep(POST_DELAY)
    if await db.ap_pending_count(cid) == 0:
        await notify_owner(bot, f"ap_done_{cid}",
                           f"📭 Autoposter: all posts of <b>{html.escape(ch['title'])}</b> are posted. "
                           "Create more posts to continue.", every=86400)


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
