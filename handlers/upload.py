
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
from utils.helpers import call
from utils.shortener import shorten

log = logging.getLogger(__name__)
MEDIA = ("document", "video", "audio", "photo", "animation", "voice", "video_note", "sticker")


def media_info(msg):
    """Return (kind, display name) of the first media attached to a message, or (None, None)."""
    for attr in MEDIA:
        obj = getattr(msg, attr, None)
        if obj:
            name = None if attr == "photo" else getattr(obj, "file_name", None)
            name = name or (msg.caption or "").strip()[:40] or attr
            return attr, name
    return None, None


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
    text = f"📦 <b>Batch mode</b>\n\n{n} file(s) added.\nKeep sending files, then tap <b>Done</b>."
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

    try:
        copied = await call(context.bot.copy_message, channel["id"], msg.chat_id, msg.message_id)
    except (Forbidden, BadRequest) as e:
        await msg.reply_text(
            f"⚠️ Could not copy to the Database Channel: <code>{html.escape(str(e))}</code>\n"
            "Make sure the bot is still an admin there (or re-set the channel from the panel).",
            reply_markup=M([[B("📡 Database Channel", callback_data="adm:dbch")]]))
        return
    except TelegramError as e:
        await msg.reply_text(f"⚠️ Upload failed: <code>{html.escape(str(e))}</code>")
        return

    await db.add_file(copied.message_id, kind, name, user.id)

    if in_batch:
        context.user_data.setdefault("batch_ids", []).append(copied.message_id)
        await _batch_status(context, msg)
        return

    token = await db.create_link([copied.message_id], user.id, name)
    await announce_link(context, msg.chat_id, token, 1, name)
