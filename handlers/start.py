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
from utils import ads
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
    await request_access(context.bot, chat_id, link)


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
        await q.answer("🚫 You are banned from using this bot.", show_alert=True)
        return
    if not is_admin(user.id):
        missing = await get_missing(context.bot, user.id)
        if missing:
            names = ", ".join(c["title"] for c in missing)[:150]
            await q.answer(f"❌ Please join first: {names}", show_alert=True)
            return
    await q.answer()
    try:
        await q.message.delete()
    except TelegramError:
        pass
    if not token:
        await call(context.bot.send_message, q.message.chat_id,
                   f"✅ Verified! Now open a file link to receive your files.\n\n📊 Daily limit: {db.limit_value()} files per user per day.")
        return
    link = await db.get_link(token)
    if not link:
        await call(context.bot.send_message, q.message.chat_id, "❌ This link is invalid or has been revoked.")
        return
    await request_access(context.bot, q.message.chat_id, link)


# ------------------------------------------------------------------ delivery
async def _copy_one(bot, chat_id, from_chat, mid, protect, caption, safe=False):
    """copy_message with flood-wait retry; drops the caption override if Telegram rejects it.
    safe=True: the original caption holds the hidden backup code, so on failure send NO caption instead."""
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
                if safe and kw["caption"] == "":
                    raise
                if safe:
                    kw["caption"] = ""
                    kw.pop("parse_mode", None)
                else:
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


async def _limit_reached(bot, chat_id: int):
    await call(bot.send_message, chat_id,
               f"🚫 <b>Daily limit reached</b>\n\nYou can get only <b>{db.limit_value()}</b> files per day. "
               "Please come back tomorrow (resets at 12:00 AM IST).", parse_mode=ParseMode.HTML)


async def request_access(bot, chat_id: int, link: dict):
    """Entry point for a user who passed force-subscribe: daily limit -> Monetag ad gate -> files."""
    if not is_admin(chat_id):
        if await db.used_today(chat_id) >= db.limit_value():   # don't make them watch an ad for nothing
            await _limit_reached(bot, chat_id)
            return
        if await ads.gate(bot, chat_id, link):                  # Mini App button sent; files follow after the ad
            return
    await deliver(bot, chat_id, link)


async def deliver(bot, chat_id: int, link: dict):
    """Daily-limit gate (10 files per user per day, admins exempt), then deliver."""
    ids = [i for i in link["ids"] if i]          # slots not restored yet are None
    token = link["_id"]
    if not ids:
        await _unavailable(bot, chat_id)
        return
    if is_admin(chat_id):
        await _deliver(bot, chat_id, {**link, "ids": ids})
        return

    limit = db.limit_value()
    start = await db.get_progress(chat_id, token)      # a big link is continued where the user stopped
    if start >= len(ids):
        start = 0
    granted = await db.reserve_quota(chat_id, len(ids) - start)
    if granted <= 0:
        await _limit_reached(bot, chat_id)
        return

    part = ids[start:start + granted]
    delivered = await _deliver(bot, chat_id, {**link, "ids": part})
    await db.refund_quota(chat_id, granted - delivered)

    end = start + delivered
    await db.set_progress(chat_id, token, end if end < len(ids) else 0)

    used = await db.used_today(chat_id)
    text = f"📊 <b>Daily limit:</b> {used}/{limit} files used today · {max(0, limit - used)} left"
    if end < len(ids):
        text += (f"\n\nℹ️ Limit reached. Sent {end}/{len(ids)} files of this link. "
                 "Open the same link tomorrow (after 12:00 AM IST) to get the rest.")
    await call(bot.send_message, chat_id, text, parse_mode=ParseMode.HTML)


async def _deliver(bot, chat_id: int, link: dict) -> int:
    """One link, two behaviours - chosen by the admin (Delivery Mode): forward the files, or stream them.
    Returns how many files the user actually received."""
    channel = S.get("db_channel")
    if not channel:
        await call(bot.send_message, chat_id, "⚠️ The bot is not set up yet. Please try again later.")
        return 0
    if S.get("delivery_mode") == "stream":
        if streamer.ready:
            n = await deliver_stream(bot, chat_id, link, channel)
            if n is not False:
                return n
        else:  # admin chose streaming but the engine is down: don't leave users empty-handed
            await notify_owner(bot, "stream_down",
                               f"⚠️ Streaming engine is off ({streamer.error}). "
                               "Files are being forwarded instead.")
    return await deliver_forward(bot, chat_id, link, channel)


async def deliver_forward(bot, chat_id: int, link: dict, channel: dict) -> int:
    protect, base_caption = S.get("protect"), _caption_override()
    caps = await db.caption_overrides(link["ids"])   # original captions; the hidden backup code is never sent
    sent, skipped, files = [], 0, 0

    for mid in link["ids"]:
        caption = base_caption if base_caption is not None else caps.get(mid)
        try:
            m = await _copy_one(bot, chat_id, channel["id"], mid, protect, caption,
                                safe=base_caption is None and mid in caps)
            sent.append(m.message_id)
            files += 1
        except Forbidden as e:
            if any(w in str(e).lower() for w in USER_GONE):
                return files  # the user blocked the bot - nothing more to do
            await _config_problem(bot, channel, e)
            await _unavailable(bot, chat_id)
            return files
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                await _config_problem(bot, channel, e)
                await _unavailable(bot, chat_id)
                return files
            skipped += 1  # message deleted from the DB channel -> skip it
            log.info("Skipping message %s: %s", mid, e)
        except TelegramError as e:
            skipped += 1
            log.warning("Delivery error: %s", e)
        await asyncio.sleep(0.05)  # gentle pacing against flood limits

    if not sent:
        await _unavailable(bot, chat_id)
        return 0

    await db.inc_views(link["_id"])
    minutes = S.get("autodelete")
    if minutes:
        try:
            notice = await call(
                bot.send_message, chat_id,
                f"⏳ These files will be deleted in <b>{minutes} minute{'s' if minutes != 1 else ''}</b>. "
                "Please save them before then.", parse_mode=ParseMode.HTML)
            sent.append(notice.message_id)
        except TelegramError:
            pass
        await db.schedule_delete(chat_id, sent, minutes)
    if skipped:
        await call(bot.send_message, chat_id, f"ℹ️ {skipped} file(s) are no longer available.")
    return files


STREAM_PAGE = 10  # files per message (2 buttons each, Telegram allows 100 buttons per keyboard)


async def deliver_stream(bot, chat_id: int, link: dict, channel: dict):
    """Send Stream / Download buttons instead of the files.
    Returns the number of files shown, or False to let the caller fall back to forwarding."""
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
        return 0

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
                return len(sent)
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
    return len(files)


async def _unavailable(bot, chat_id):
    try:
        await bot.send_message(chat_id, "⚠️ Files are temporarily unavailable. Please try again later.")
    except TelegramError:
        pass


async def _config_problem(bot, channel, err):
    await notify_owner(
        bot, "dbch",
        f"⚠️ Unable to read the Database Channel \"{channel.get('title')}\" ({channel['id']}): {err}\n"
        "Make sure the bot is an admin there, or set the channel again.")
