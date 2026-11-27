"""
handlers/zipvault.py
Vault Pack ZIP upload - panel screens (registered into handlers.admin's ACTIONS / STATES).

  Autoposter -> channel (Vault Pack) -> ➕ Create Post
      📦 ZIP UPLOAD     send one or more ZIPs (folders with images + videos) -> ▶️ START
      🖼 THUMBNAILS     the bot names a folder, you send its thumbnail(s):
                          1 image = 1 post · 2 images = 2 posts · ... (same Telegraph + video link, different thumbnail)
                          ✅ DONE            create the posts from the images you sent
                          📌 FIXED THUMBNAIL  no image: use the channel's fixed thumbnail (1 post)
                          ⏭ SKIP             decide later        📌 FIXED FOR ALL LEFT  every waiting folder, fixed thumbnail
                          ⏹ STOP             leave the screen (nothing is lost, folders keep waiting)

The engine (download / extract / imgbb / Telegraph / posts) is in utils/zipvault.py.
"""
import html
import logging
import time

from telegram import InlineKeyboardMarkup as M

from database import db
from database.db import settings as S
from handlers.admin import action, show, state
from handlers.upload import media_info
from utils import autoposter as AP
from utils import batch as BT
from utils import keyboards as K
from utils import zipvault as ZV
from utils.keyboards import ub as U
from utils.mtproto import mtproto
from utils.ui import styled

log = logging.getLogger(__name__)


async def _job(coro, bot, chat_id):
    try:
        await coro
    except Exception as e:                      # never let a background job die silently
        log.exception("ZIP upload job failed")
        try:
            await bot.send_message(chat_id, f"⚠️ ZIP UPLOAD FAILED: <code>{html.escape(str(e))}</code>")
        except Exception:
            pass


# ===================================================================== (1) ZIP upload
def _zip_text(zips: list) -> str:
    rows = "\n".join(f"• {html.escape(z['name'][:34])} ({ZV._size(z['size'])})" for z in zips[-10:])
    big = "" if mtproto.ready else "\n⚠️ MTPROTO IS OFF (API_ID / API_HASH): ONLY ZIPS UP TO 20 MB WORK."
    return (f"📦 <b>ZIP UPLOAD</b>\n\n{len(zips)} ZIP(S) RECEIVED.\n"
            "EACH ZIP = FOLDERS, EACH FOLDER = IMAGES + VIDEOS.\n"
            "SEND MORE ZIPS, OR TAP START.\n\n" + (rows or "<i>NONE YET</i>") + big)


def _zip_kb():
    return M([[U("▶️ Start", "adm:zvgo")], [U("❌ Cancel", "adm:cancel")]])


@action("zvnew")
async def a_zvnew(update, context, args):
    cid = int(args[0])
    back = K.back(f"apnew:{cid}")
    if not S.get("db_channel"):
        await show(update, "⚠️ SET THE DATABASE CHANNEL FIRST.", back)
        return
    if not BT.enabled():
        await show(update, "⚠️ ADD AN IMGBB KEY FIRST (CAPTION → IMGBB KEYS).", back)
        return
    q = update.callback_query
    context.user_data.update(state="zv_zip", zv_cid=cid, zv_zips=[],
                             zv_status=(q.message.chat_id, q.message.message_id))
    await show(update, _zip_text([]), _zip_kb())


@state("zv_zip")
async def st_zv_zip(update, context):
    msg = update.effective_message
    doc = msg.document
    name = (doc.file_name or "") if doc else ""
    if not doc or not (name.lower().endswith(".zip") or (doc.mime_type or "") in ("application/zip", "application/x-zip-compressed")):
        await msg.reply_text("⚠️ SEND A <b>.ZIP</b> FILE (SEND IT AS A FILE, NOT AS A PHOTO), OR TAP START.",
                             reply_markup=_zip_kb())
        return
    if not mtproto.ready and (doc.file_size or 0) > ZV.BOT_API_MAX_GET:
        await msg.reply_text("⚠️ THIS ZIP IS OVER 20 MB. FOR BIG ZIPS SET <code>API_ID</code> + <code>API_HASH</code> "
                             "(MTPROTO) IN THE .ENV AND RESTART THE BOT.", reply_markup=_zip_kb())
        return
    zips = context.user_data.setdefault("zv_zips", [])
    zips.append({"chat": msg.chat_id, "mid": msg.message_id, "name": name or "archive.zip",
                 "size": doc.file_size or 0, "fid": doc.file_id})
    st = context.user_data.get("zv_status")
    if st:
        try:
            await context.bot.edit_message_text(styled(_zip_text(zips)), chat_id=st[0], message_id=st[1],
                                                reply_markup=_zip_kb(), parse_mode="HTML")
        except Exception:
            pass


@action("zvgo")
async def a_zvgo(update, context, args):
    ud = context.user_data
    zips, cid = ud.get("zv_zips") or [], ud.get("zv_cid")
    if not zips or cid is None:
        if cid is not None:
            ud["state"] = "zv_zip"                  # the router cleared it; keep waiting for ZIPs
        await show(update, "⚠️ SEND AT LEAST ONE ZIP FIRST.\n\n" + _zip_text([]), _zip_kb())
        return
    for k in ("state", "zv_zips", "zv_cid", "zv_status"):
        ud.pop(k, None)
    await show(update, "⏳ <b>ZIP UPLOAD</b>\n\nSTARTED. THE STATUS IS IN THE NEXT MESSAGE.", K.back(f"apnew:{cid}"))
    status = await update.effective_chat.send_message("⏳ STARTING…")
    AP.spawn(_job(ZV.run_job(context.bot, cid, update.effective_user.id, zips, status.chat_id, status.message_id),
                  context.bot, status.chat_id))


# ===================================================================== (2) thumbnails
def _prompt(b: dict, got: int, left: int, note: str = "") -> str:
    imgs = len(b.get("imgs") or []) - len(b.get("img_failed") or [])
    vids = len(b.get("vids") or [])
    skipped = len(b.get("img_failed") or [])
    return (f"{note}🖼 <b>SEND THUMBNAIL</b>\n\n"
            f"📂 FOLDER ▸ <b>{html.escape(b['name'])}</b>\n"
            f"🖼 IMAGES ▸ <b>{imgs}</b> · 🎬 VIDEOS ▸ <b>{vids}</b>"
            + (f" · ⚠️ SKIPPED ▸ {skipped}" if skipped else "") + "\n"
            f"📥 RECEIVED ▸ <b>{got}</b> · ⏳ FOLDERS LEFT ▸ <b>{left}</b>\n\n"
            f"SEND THE THUMBNAIL(S) OF “{html.escape(b['name'])}” NOW.\n"
            "1 IMAGE = 1 POST · 2 IMAGES = 2 POSTS · …\n"
            "NO IMAGE? TAP FIXED THUMBNAIL.")


def _thumb_kb(cid: int):
    return M([[U("✅ Done", "adm:zvdone"), U("📌 Fixed Thumbnail", "adm:zvfix")],
              [U("⏭ Skip", "adm:zvskip"), U("⏹ Stop", "adm:zvstop")],
              [U("📌 Fixed For All Left", f"adm:zvfixall:{cid}")]])


async def _left(cid: int, skip: list) -> int:
    return max(0, await ZV.waiting_count(cid) - len(skip))


async def _screen(update, context, b: dict, note: str = ""):
    """Show the prompt of folder b (keeps the thumbnails already collected)."""
    ud = context.user_data
    cid = b["chan"]
    ud["state"] = "zv_thumb"
    q = update.callback_query
    if q:
        ud["zv_status"] = (q.message.chat_id, q.message.message_id)
    await show(update, _prompt(b, len(ud.get("zv_thumbs") or []), await _left(cid, ud.get("zv_skip") or []), note),
               _thumb_kb(cid))


async def _next(update, context, cid: int, note: str = ""):
    """Move on to the next waiting folder (or show the end screen)."""
    ud = context.user_data
    skip = ud.get("zv_skip") or []
    b = await ZV.next_waiting(cid, skip)
    ud["zv_thumbs"] = []
    if not b:
        for k in ("state", "zv_bid", "zv_thumbs", "zv_status"):
            ud.pop(k, None)
        rest = await ZV.waiting_count(cid)
        busy = await ZV.preparing_count(cid)
        text = note + "🎉 <b>THUMBNAILS</b>\n\n" + ("ALL FOLDERS HAVE THEIR POSTS ✅" if not rest else
                                                    f"⏭ {rest} FOLDER(S) WERE SKIPPED. TAP THUMBNAILS AGAIN WHEN READY.")
        if busy:
            text += f"\n⚙️ {busy} FOLDER(S) ARE STILL BEING PREPARED. YOU GET A MESSAGE WHEN THEY ARE READY."
        await show(update, text, M([[U("🖼 Thumbnails", f"adm:zvthumb:{cid}")], [U("🔙 Back", f"adm:apnew:{cid}")]])
                   if rest or busy else K.back(f"apc:{cid}"))
        return
    ud["zv_bid"], ud["zv_cid"] = b["_id"], cid
    await _screen(update, context, b, note)


@action("zvthumb")
async def a_zvthumb(update, context, args):
    cid = int(args[0])
    ud = context.user_data
    ud["zv_skip"] = []
    if not await ZV.waiting_count(cid):
        busy = await ZV.preparing_count(cid)
        text = "🖼 <b>THUMBNAILS</b>\n\nNO FOLDER IS WAITING FOR A THUMBNAIL."
        if busy:
            text += f"\n⚙️ {busy} FOLDER(S) ARE STILL BEING PREPARED (IMAGES / VIDEOS UPLOADING)."
        await show(update, text, M([[U("🔄 Refresh", f"adm:zvthumb:{cid}")], [U("🔙 Back", f"adm:apnew:{cid}")]]))
        return
    await _next(update, context, cid)


def _item(msg):
    """The thumbnail the admin sent: a photo, or an image sent as a file. None = not an image."""
    kind, name = media_info(msg)
    if kind == "photo":
        return AP.item_from_ptb(msg, "photo", name)
    if kind == "document" and (msg.document.mime_type or "").startswith("image/"):
        it = AP.item_from_ptb(msg, "document", name)
        it["fid"] = msg.document.file_id
        return it
    return None


@state("zv_thumb")
async def st_zv_thumb(update, context):
    msg = update.effective_message
    ud = context.user_data
    cid, bid = ud.get("zv_cid"), ud.get("zv_bid")
    if cid is None or not bid:
        return
    it = _item(msg)
    if not it:
        await msg.reply_text("⚠️ SEND AN IMAGE (PHOTO OR IMAGE FILE), OR USE THE BUTTONS.", reply_markup=_thumb_kb(cid))
        return
    ud.setdefault("zv_thumbs", []).append(it)
    st = ud.get("zv_status")
    if st and time.time() - ud.get("zv_ts", 0) > 1.2:
        ud["zv_ts"] = time.time()
        b = await db.ap_batches_col.find_one({"_id": bid})
        if b:
            try:
                await context.bot.edit_message_text(
                    styled(_prompt(b, len(ud["zv_thumbs"]), await _left(cid, ud.get("zv_skip") or []))),
                    chat_id=st[0], message_id=st[1], reply_markup=_thumb_kb(cid), parse_mode="HTML")
            except Exception:
                pass


async def _current(update, context):
    ud = context.user_data
    bid = ud.get("zv_bid")
    b = await db.ap_batches_col.find_one({"_id": bid}) if bid else None
    if not b or b.get("status") != "need_thumb":
        cid = ud.get("zv_cid")
        if cid is None:
            await show(update, "⚠️ SESSION EXPIRED. OPEN THUMBNAILS AGAIN.", K.back("ap"))
        else:
            await _next(update, context, cid)
        return None
    return b


@action("zvdone")
async def a_zvdone(update, context, args):
    ud = context.user_data
    b = await _current(update, context)
    if not b:
        return
    thumbs = ud.get("zv_thumbs") or []
    if not thumbs:
        await _screen(update, context, b, "⚠️ SEND AT LEAST ONE IMAGE, OR TAP FIXED THUMBNAIL.\n\n")
        return
    try:
        n = await ZV.make_posts(context.bot, b, thumbs, update.effective_user.id)
    except Exception as e:
        log.exception("make_posts failed")
        await _screen(update, context, b, f"⚠️ <code>{html.escape(str(e)[:200])}</code>\n\n")
        return
    await _next(update, context, b["chan"], f"✅ {n} POST(S) CREATED FOR “{html.escape(b['name'])}”.\n\n")


@action("zvfix")
async def a_zvfix(update, context, args):
    b = await _current(update, context)
    if not b:
        return
    try:
        n = await ZV.make_posts(context.bot, b, [], update.effective_user.id, fixed=True)
    except Exception as e:
        await _screen(update, context, b, f"⚠️ {html.escape(str(e)[:200]).upper()}\n\n")
        return
    await _next(update, context, b["chan"], f"✅ {n} POST CREATED FOR “{html.escape(b['name'])}” (FIXED THUMBNAIL).\n\n")


@action("zvskip")
async def a_zvskip(update, context, args):
    ud = context.user_data
    b = await _current(update, context)
    if not b:
        return
    ud.setdefault("zv_skip", []).append(b["_id"])
    await _next(update, context, b["chan"], f"⏭ SKIPPED “{html.escape(b['name'])}”.\n\n")


@action("zvstop")
async def a_zvstop(update, context, args):
    ud = context.user_data
    cid = ud.get("zv_cid")
    for k in ("state", "zv_bid", "zv_thumbs", "zv_skip", "zv_status", "zv_cid"):
        ud.pop(k, None)
    rest = await ZV.waiting_count(cid) if cid is not None else 0
    await show(update, f"⏹ <b>THUMBNAILS</b>\n\nSTOPPED. {rest} FOLDER(S) STILL WAIT FOR A THUMBNAIL.",
               K.back(f"apnew:{cid}" if cid is not None else "ap"))


@action("zvfixall")
async def a_zvfixall(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid) or {}
    n = await ZV.waiting_count(cid)
    if not ch.get("fthumb"):
        b = await _current(update, context)
        if b:
            await _screen(update, context, b, "⚠️ FIXED THUMBNAIL NOT SET (CAPTION → THUMBNAIL).\n\n")
        return
    await show(update, f"📌 <b>FIXED FOR ALL</b>\n\nUSE THE FIXED THUMBNAIL FOR ALL <b>{n}</b> WAITING FOLDER(S)?\n"
                       "EACH FOLDER GETS ONE POST.",
               M([[U("✅ Yes", f"adm:zvfixally:{cid}"), U("❌ No", f"adm:zvthumb:{cid}")]]))


@action("zvfixally")
async def a_zvfixally(update, context, args):
    cid = int(args[0])
    made = posts = 0
    while True:
        b = await ZV.next_waiting(cid, [])
        if not b:
            break
        try:
            posts += await ZV.make_posts(context.bot, b, [], update.effective_user.id, fixed=True)
            made += 1
        except Exception as e:
            log.exception("fixed-for-all failed")
            await show(update, f"⚠️ <b>FIXED FOR ALL</b>\n\nSTOPPED AFTER {made} FOLDER(S): "
                               f"<code>{html.escape(str(e)[:200])}</code>", K.back(f"apnew:{cid}"))
            return
    context.user_data.pop("zv_skip", None)
    await show(update, f"✅ <b>FIXED FOR ALL</b>\n\n{posts} POST(S) CREATED FOR {made} FOLDER(S).", K.back(f"apc:{cid}"))
