"""
handlers/start.py
The ONLY typed command: /start.
  admin, no token  -> admin panel
  user, no token   -> start image + caption + join buttons + "Try Again / Verify"
  with token       -> force-sub check -> deliver files (or show the join screen)
Also: the "✅ I've Joined" / "Verify" callback and the file delivery routine.
"""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from database import db
from database.db import settings as S
from handlers import admin
from utils.helpers import call, is_admin, notify_owner, render_caption, retry_seconds
from utils.membership import get_missing
from utils.streamer import human_size, streamer
import config

log = logging.getLogger(__name__)
USER_GONE = ("blocked", "deactivated", "can't initiate", "user is deleted")


# ------------------------------------------------------------------ /start
async def start_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = update.effective_user, update.effective_message
    await db.add_user(user.id)
    context.user_data.clear()  # drop any half-finished admin prompt

    if not is_admin(user.id) and await db.is_banned(user.id):
        await msg.reply_text("🚫 You are banned from using this bot.")
        return

    token = context.args[0] if context.args else ""
    if not token:
        if is_admin(user.id):
            await admin.send_panel(update, context)
        else:
            await send_gate(context, user, msg.chat_id, token="")
        return
    await handle_token(context, user, msg.chat_id, token)


async def handle_token(context, user, chat_id: int, token: str):
    link = await db.get_link(token)
    if not link:
        await call(context.bot.send_message, chat_id, "❌ This link is invalid or has been revoked.")
        return
    if not is_admin(user.id):  # admins bypass force-subscribe
        missing = await get_missing(context.bot, user.id)
        if missing:
            await send_gate(context, user, chat_id, token, missing)
            return
    await deliver(context.bot, chat_id, link)


# ------------------------------------------------------------------ join screen
async def send_gate(context, user, chat_id: int, token: str = "", missing=None):
    """Start image + caption + channel buttons + custom buttons + verify button."""
    channels = []
    if S.get("fsub_enabled"):
        channels = missing if missing is not None else S.get("fsub_channels")
    rows, row = [], []
    for ch in channels:
        row.append(B(f"📢 {ch['title'][:28]}", url=ch["link"]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    for custom_row in S.get("custom_buttons"):
        rows.append([B(t, url=u) for t, u in custom_row])
    rows.append([B("✅ I've Joined" if token else "🔄 Try Again / Verify", callback_data=f"chk:{token}")])

    caption = render_caption(S.get("start_caption"), user)
    kb = M(rows)
    image = S.get("start_image")
    if image:
        try:
            await call(context.bot.send_photo, chat_id, image, caption=caption,
                       reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except BadRequest as e:  # bad file_id / caption too long -> fall back to text
            log.warning("Start image failed: %s", e)
    await call(context.bot.send_message, chat_id, caption, reply_markup=kb,
               parse_mode=ParseMode.HTML)


async def check_callback(update, context):
    """Handles 'chk:<token>' (✅ I've Joined) and 'chk:' (Try Again / Verify)."""
    q, user = update.callback_query, update.callback_query.from_user
    token = q.data.split(":", 1)[1]
    if not is_admin(user.id) and await db.is_banned(user.id):
        await q.answer("🚫 You are banned.", show_alert=True)
        return
    if not is_admin(user.id):
        missing = await get_missing(context.bot, user.id)
        if missing:
            names = ", ".join(c["title"] for c in missing)[:150]
            await q.answer(f"❌ You haven't joined: {names}", show_alert=True)
            return
    await q.answer()
    try:
        await q.message.delete()
    except TelegramError:
        pass
    if not token:
        await call(context.bot.send_message, q.message.chat_id,
                   "✅ Verified! Now open a file link to receive your files.")
        return
    link = await db.get_link(token)
    if not link:
        await call(context.bot.send_message, q.message.chat_id, "❌ This link is invalid or has been revoked.")
        return
    await deliver(context.bot, q.message.chat_id, link)


# ------------------------------------------------------------------ delivery
async def _copy_one(bot, chat_id, from_chat, mid, protect, caption):
    """copy_message with flood-wait retry; drops the caption override if Telegram rejects it."""
    kw = dict(chat_id=chat_id, from_chat_id=from_chat, message_id=mid, protect_content=protect)
    if caption is not None:
        kw.update(caption=caption, parse_mode=ParseMode.HTML)
    for _ in range(4):
        try:
            return await bot.copy_message(**kw)
        except RetryAfter as e:
            await asyncio.sleep(retry_seconds(e) + 1)
        except BadRequest as e:
            if "caption" in kw and "chat not found" not in str(e).lower() \
                    and "message to copy not found" not in str(e).lower():
                kw.pop("caption")
                kw.pop("parse_mode", None)
                continue
            raise
    return await bot.copy_message(**kw)


def _caption_override():
    """None = keep original caption, "" = remove it, text = custom HTML caption."""
    custom = S.get("custom_caption")
    if custom:
        return custom
    return "" if S.get("remove_caption") else None


async def deliver(bot, chat_id: int, link: dict):
    """One link, two behaviours - chosen by the admin (Delivery Mode): forward the files, or stream them."""
    channel = S.get("db_channel")
    if not channel:
        await call(bot.send_message, chat_id, "⚠️ The bot is not configured yet. Please try later.")
        return
    if S.get("delivery_mode") == "stream":
        if streamer.ready:
            if await deliver_stream(bot, chat_id, link, channel):
                return
        else:  # admin chose streaming but the engine is down: don't leave users empty-handed
            await notify_owner(bot, "stream_down",
                               f"⚠️ Delivery mode is Stream but the streaming engine is off ({streamer.error}). "
                               "Falling back to normal forwarding.")
    await deliver_forward(bot, chat_id, link, channel)


async def deliver_forward(bot, chat_id: int, link: dict, channel: dict):
    protect, caption = S.get("protect"), _caption_override()
    sent, skipped = [], 0

    for mid in link["ids"]:
        try:
            m = await _copy_one(bot, chat_id, channel["id"], mid, protect, caption)
            sent.append(m.message_id)
        except Forbidden as e:
            if any(w in str(e).lower() for w in USER_GONE):
                return  # the user blocked the bot - nothing more to do
            await _config_problem(bot, channel, e)
            await _unavailable(bot, chat_id)
            return
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                await _config_problem(bot, channel, e)
                await _unavailable(bot, chat_id)
                return
            skipped += 1  # message deleted from the DB channel -> skip it
            log.info("Skipping message %s: %s", mid, e)
        except TelegramError as e:
            skipped += 1
            log.warning("Delivery error: %s", e)
        await asyncio.sleep(0.05)  # gentle pacing against flood limits

    if not sent:
        await _unavailable(bot, chat_id)
        return

    await db.inc_views(link["_id"])
    minutes = S.get("autodelete")
    if minutes:
        try:
            notice = await call(
                bot.send_message, chat_id,
                f"⏳ These files will be deleted in <b>{minutes} minute{'s' if minutes != 1 else ''}</b>. "
                "Forward or save them now!", parse_mode=ParseMode.HTML)
            sent.append(notice.message_id)
        except TelegramError:
            pass
        await db.schedule_delete(chat_id, sent, minutes)
    if skipped:
        await call(bot.send_message, chat_id, f"ℹ️ {skipped} file(s) are no longer available.")


STREAM_PAGE = 10  # files per message (2 buttons each, Telegram allows 100 buttons per keyboard)


async def deliver_stream(bot, chat_id: int, link: dict, channel: dict) -> bool:
    """Send Stream / Download buttons instead of the files. Returns False to let the caller fall back."""
    token, ids = link["_id"], link["ids"]
    try:
        infos = await streamer.get_infos(channel["id"], ids)
    except TelegramError as e:
        await _config_problem(bot, channel, e)
        return False
    except Exception as e:  # MTProto hiccup (RPC error, timeout...) -> fall back to forwarding
        log.warning("Stream info failed, falling back to forward: %s", e)
        return False
    files = [infos[m] for m in ids if m in infos]
    if not files:
        await _unavailable(bot, chat_id)
        return True

    sent = []
    for p in range(0, len(files), STREAM_PAGE):
        page = files[p:p + STREAM_PAGE]
        lines, rows = [], []
        for n, f in enumerate(page, start=p + 1):
            url = f"{config.BASE_URL}/dl/{token}/{f.msg_id}"
            icon = "🎬" if f.mime.startswith("video/") else "🎵" if f.mime.startswith("audio/") else "📄"
            lines.append(f"{icon} <b>{html.escape(f.name[:80])}</b>\n💾 {human_size(f.size)}")
            row = []
            if f.streamable:
                row.append(B(f"▶️ Stream{f' {n}' if len(files) > 1 else ''}", url=url))
            row.append(B(f"⬇️ Download{f' {n}' if len(files) > 1 else ''}", url=url + "?d=1"))
            rows.append(row)
        head = "" if len(files) == 1 else f"📦 <b>{len(files)} files</b>\n\n"
        try:
            m = await call(bot.send_message, chat_id, head + "\n\n".join(lines),
                           reply_markup=M(rows), parse_mode=ParseMode.HTML)
            sent.append(m.message_id)
        except Forbidden as e:
            if any(w in str(e).lower() for w in USER_GONE):
                return True
            raise
        except TelegramError as e:
            log.warning("Stream message failed: %s", e)
            return False

    await db.inc_views(token)
    minutes = S.get("autodelete")
    if minutes:
        await db.schedule_delete(chat_id, sent, minutes)
    if len(files) < len(ids):
        await call(bot.send_message, chat_id, f"ℹ️ {len(ids) - len(files)} file(s) are no longer available.")
    return True


async def _unavailable(bot, chat_id):
    try:
        await bot.send_message(chat_id, "⚠️ Files are temporarily unavailable. Please try again later.")
    except TelegramError:
        pass


async def _config_problem(bot, channel, err):
    await notify_owner(
        bot, "dbch",
        f"⚠️ I can't read from the Database Channel \"{channel.get('title')}\" ({channel['id']}): {err}\n"
        "Make sure the bot is an admin there, or set the channel again from the panel.")
