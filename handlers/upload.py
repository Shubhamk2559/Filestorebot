
"""
handlers/upload.py
Admin file uploads: copy the file into the Database Channel, create a random-token
deep link. Also used by Batch mode (many files -> ONE link).
"""
import html
import logging

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

from database import db
from database.db import settings as S
from utils import keyboards as K
from utils.helpers import call, notify_owner
from utils.shortener import shorten

log = logging.getLogger(__name__)
MEDIA = ("document", "video", "audio", "photo", "animation", "voice", "video_note", "sticker")
NO_CAPTION = ("video_note", "sticker")   # Telegram cannot attach a caption to these -> no backup code
CAPTION_MAX = 1024


def media_info(msg):
    """Return (kind, display name) of the first media attached to a message, or (None, None)."""
    for attr in MEDIA:
        obj = getattr(msg, attr, None)
        if obj:
            name = None if attr == "photo" else getattr(obj, "file_name", None)
            name = name or db.CODE_RE.sub("", msg.caption or "").strip()[:40] or attr
            return attr, name
    return None, None


def _original_caption(msg) -> str:
    """The file's own caption (HTML) with any backup code removed."""
    return db.CODE_TAIL_RE.sub("", msg.caption_html or "").strip()


def _caption_with_code(msg, code: str) -> str:
    """Original caption + backup code line. Falls back to plain text if it would exceed Telegram's limit."""
    tag = f"<code>{code}</code>"
    orig = _original_caption(msg)
    plain = (msg.caption or "").strip()
    if len(plain) + len(code) + 2 <= CAPTION_MAX:
        return f"{orig}\n\n{tag}" if orig else tag
    room = max(0, CAPTION_MAX - len(code) - 2)
    return f"{html.escape(plain[:room])}\n\n{tag}"


async def _restore_status(context, msg):
    """One status message with a running count while the admin forwards old files back in."""
    n = context.user_data["restore_n"] = context.user_data.get("restore_n", 0) + 1
    text = f"♻️ <b>Restore mode</b>\n\n{n} file(s) restored from their caption codes."
    st = context.user_data.get("restore_status")
    if st:
        try:
            await context.bot.edit_message_text(text, chat_id=st[0], message_id=st[1])
            return
        except BadRequest:
            pass
    sent = await msg.reply_text(text)
    context.user_data["restore_status"] = (sent.chat_id, sent.message_id)


async def channel_restore(update, context):
    """
    Files forwarded INTO the Database Channel (e.g. from the old, suspended channel) keep their caption.
    The caption code tells us which link / slot they belong to, so every old link works again.
    """
    msg = update.channel_post
    channel = S.get("db_channel")
    if not msg or not channel or msg.chat_id != channel["id"]:
        return
    parsed = db.parse_code(msg.caption)
    kind, name = media_info(msg)
    if not parsed or not kind:
        return
    token, idx = parsed
    await db.restore_file(token, idx, msg.message_id, kind, name, _original_caption(msg), 0)
    log.info("Restored %s from caption code (new message id %s)", db.make_code(token, idx), msg.message_id)
    await notify_owner(context.bot, "restore",
                       "♻️ Files are being restored from caption codes in the Database Channel. "
                       "Old links will work again once all files are forwarded.", every=600)


async def announce_link(context, chat_id: int, token: str, count: int, label: str):
    """Send the shareable link (plus a shortened one if the shortener is enabled)."""
    link = f"https://t.me/{context.bot.username}?start={token}"
    text = (f"✅ <b>Link created</b>\n📎 {html.escape(label)} "
            f"({count} file{'s' if count != 1 else ''})\n\n🔗 <code>{link}</code>")
    short = await shorten(link)
    if short:
        text += f"\n\n💸 Shortened:\n<code>{html.escape(short)}</code>"
    await call(context.bot.send_message, chat_id, text, reply_markup=K.after_link(),
               parse_mode=ParseMode.HTML)


async def _batch_status(context, msg):
    """Keep ONE status message updated with the running file count."""
    n = len(context.user_data.get("batch_ids", []))
    text = f"📦 <b>Batch mode</b>\n\n{n} file(s) added.\nSend more files or tap <b>Done</b>."
    st = context.user_data.get("batch_status")
    if st:
        try:
            await context.bot.edit_message_text(text, chat_id=st[0], message_id=st[1],
                                                reply_markup=K.batch_kb())
            return
        except BadRequest:
            pass
    sent = await msg.reply_text(text, reply_markup=K.batch_kb())
    context.user_data["batch_status"] = (sent.chat_id, sent.message_id)


async def handle_upload(update, context):
    msg, user = update.effective_message, update.effective_user
    in_batch = context.user_data.get("state") == "batch"
    kind, name = media_info(msg)
    if not kind:
        if in_batch:
            await msg.reply_text("📎 Send files only, or tap <b>Done</b>.", reply_markup=K.batch_kb())
        return

    channel = S.get("db_channel")
    if not channel:
        await msg.reply_text(
            "⚠️ Set the <b>Database Channel</b> first.",
            reply_markup=M([[B("📡 Set Database Channel", callback_data="adm:dbch")]]))
        return

    parsed = db.parse_code(msg.caption)
    restoring = parsed is not None and kind not in NO_CAPTION
    if restoring:
        token, idx = parsed                    # file came from the old DB channel -> keep its code
        caption = msg.caption_html or msg.caption
    else:
        if in_batch:
            token = context.user_data.setdefault("batch_token", db.new_token())
            idx = len(context.user_data.get("batch_ids", []))
        else:
            token, idx = db.new_token(), 0
        caption = None if kind in NO_CAPTION else _caption_with_code(msg, db.make_code(token, idx))

    try:
        kw = {"caption": caption, "parse_mode": ParseMode.HTML} if caption is not None else {}
        copied = await call(context.bot.copy_message, channel["id"], msg.chat_id, msg.message_id, **kw)
    except (Forbidden, BadRequest) as e:
        await msg.reply_text(
            f"⚠️ Unable to save to the Database Channel: <code>{html.escape(str(e))}</code>\n"
            "Make sure the bot is still an admin there, or set the channel again.",
            reply_markup=M([[B("📡 Database Channel", callback_data="adm:dbch")]]))
        return
    except TelegramError as e:
        await msg.reply_text(f"⚠️ Upload failed: <code>{html.escape(str(e))}</code>")
        return

    if restoring:
        await db.restore_file(token, idx, copied.message_id, kind, name, _original_caption(msg), user.id)
        await _restore_status(context, msg)
        return

    await db.add_file(copied.message_id, kind, name, user.id,
                      code=db.make_code(token, idx) if caption is not None else None,
                      cap=_original_caption(msg))

    if in_batch:
        context.user_data.setdefault("batch_ids", []).append(copied.message_id)
        await _batch_status(context, msg)
        return

    await db.create_link([copied.message_id], user.id, name, token=token)
    await announce_link(context, msg.chat_id, token, 1, name)
