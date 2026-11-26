"""
handlers/start.py
The ONLY typed command: /start.
  admin, no token  -> admin panel
  user, no token   -> start image + caption + join buttons + "Try Again / Verify"
  with token       -> force-sub check -> deliver files (or show the join screen)
Also: the "✅ I've Joined" / "Verify" callback and the file delivery routine.
"""
import asyncio
import logging
import time

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from database import db
from database.db import settings as S
from handlers import admin
from utils.helpers import call, is_admin, notify_owner, render_caption, retry_seconds
from utils import ads
from utils import support as SUP
from utils.keyboards import grid, kb, link as url_btn, ub
from utils.ui import esc, head, hl
from utils.membership import get_missing

log = logging.getLogger(__name__)
USER_GONE = ("blocked", "deactivated", "can't initiate", "user is deleted")

COOLDOWN_SECONDS = 5          # gap between two link opens per user (admins exempt)
_last_open: dict = {}         # user_id -> monotonic time of the last accepted link open


# ------------------------------------------------------------------ /start
async def start_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = update.effective_user, update.effective_message
    banned = await db.touch_user(user.id)      # register + ban check in ONE database round trip
    context.user_data.clear()  # drop any half-finished admin prompt

    if banned and not is_admin(user.id):
        await msg.reply_text(head("🚫", "Banned", hl("YOU CANNOT USE THIS BOT.")))
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
    if is_admin(user.id):  # admins bypass force-subscribe
        link, missing = await db.get_link(token), []
    else:                  # link lookup and channel-membership check run in parallel (faster first response)
        link, missing = await asyncio.gather(db.get_link(token), get_missing(context.bot, user.id))
    if not link:
        await call(context.bot.send_message, chat_id, head("❌", "Invalid Link", "THIS LINK IS WRONG OR REVOKED."),
                   reply_markup=SUP.markup())
        return
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
    rows = grid([url_btn(f"📢 {ch['title'][:28]}", ch["link"], raw=True) for ch in channels])
    rows += [[url_btn(t, u, raw=True) for t, u in custom_row] for custom_row in S.get("custom_buttons")]
    rows.append([ub("✅ Verify" if token else "🔄 Verify", f"chk:{token}")])
    rows += SUP.row(token)

    caption = render_caption(S.get("start_caption"), user)
    markup = kb(rows)
    image = S.get("start_image")
    if image:
        try:
            await call(context.bot.send_photo, chat_id, image, caption=caption,
                       reply_markup=markup, parse_mode=ParseMode.HTML)
            return
        except BadRequest as e:  # bad file_id / caption too long -> fall back to text
            log.warning("Start image failed: %s", e)
    await call(context.bot.send_message, chat_id, caption, reply_markup=markup,
               parse_mode=ParseMode.HTML)


async def check_callback(update, context):
    """Handles 'chk:<token>' (✅ I've Joined) and 'chk:' (Try Again / Verify)."""
    q, user = update.callback_query, update.callback_query.from_user
    token = q.data.split(":", 1)[1]
    link = None
    if not is_admin(user.id):
        banned, missing = await asyncio.gather(db.is_banned(user.id), get_missing(context.bot, user.id))
        if banned:
            await q.answer("🚫 YOU ARE BANNED.", show_alert=True)
            return
        if missing:
            names = ", ".join(c["title"] for c in missing)[:150]
            await q.answer(f"❌ JOIN FIRST ▸ {names}", show_alert=True)
            return
    await q.answer()
    if token:
        link_task = asyncio.ensure_future(db.get_link(token))      # fetch while the old message is being deleted
    try:
        await q.message.delete()
    except TelegramError:
        pass
    if not token:
        await call(context.bot.send_message, q.message.chat_id,
                   head("✅", "Verified", f"OPEN A FILE LINK TO GET YOUR FILES.\n\n📊 LIMIT ▸ <b>{db.limit_value()}</b> LINKS PER DAY"),
                   reply_markup=SUP.markup())
        return
    link = await link_task
    if not link:
        await call(context.bot.send_message, q.message.chat_id, head("❌", "Invalid Link", "THIS LINK IS WRONG OR REVOKED."),
                   reply_markup=SUP.markup())
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
               head("🚫", "Daily Limit Reached", f"ONLY <b>{db.limit_value()}</b> LINKS PER DAY.\n" + hl("COME BACK TOMORROW (RESETS 12:00 AM IST).")),
               parse_mode=ParseMode.HTML, reply_markup=SUP.markup())


async def request_access(bot, chat_id: int, link: dict):
    """Entry point for a user who passed force-subscribe: cooldown -> daily limit -> Monetag ad gate -> files."""
    free = bool(link.get("no_ads"))              # Duo Reel post: the image link is a free preview (no ad, no limit)
    if not is_admin(chat_id):
        wait = COOLDOWN_SECONDS - (time.monotonic() - _last_open.get(chat_id, 0.0))
        if wait > 0:
            await call(bot.send_message, chat_id,
                       head("⏳", "Please Wait", f"NEXT LINK IN <b>{int(wait) + 1}</b> SEC."),
                       parse_mode=ParseMode.HTML)
            return
        _last_open[chat_id] = time.monotonic()
        if len(_last_open) > 20000:                              # keep memory bounded
            _last_open.clear()
        cost = await db.link_cost(link)                          # channel-wise (live) setting
        if not free and cost and S.get("ads_enabled") and await db.used_today(chat_id) + cost > db.limit_value():
            await _limit_reached(bot, chat_id)                  # don't make them watch an ad for nothing
            return
    if not free and await ads.gate(bot, chat_id, link):         # Mini App button sent; files follow after the ad
        return
    await deliver(bot, chat_id, link)


async def deliver(bot, chat_id: int, link: dict):
    """Daily-limit gate (admins exempt). A link uses `cost` counts (default 1), however many files it has."""
    ids = [i for i in link["ids"] if i]          # slots not restored yet are None
    if not ids:
        await _unavailable(bot, chat_id)
        return
    if is_admin(chat_id) or link.get("no_ads"):      # admins and free preview links: no daily-limit count
        await _deliver(bot, chat_id, {**link, "ids": ids})
        return

    limit = db.limit_value()
    cost = await db.link_cost(link)                    # channel-wise (live) setting, default 1 (0 = free)
    if cost <= 0:
        await _deliver(bot, chat_id, {**link, "ids": ids})
        return
    granted, used = await db.claim_quota(chat_id, cost)   # atomic, one DB call
    if granted <= 0:
        await _limit_reached(bot, chat_id)
        return

    delivered = await _deliver(bot, chat_id, {**link, "ids": ids})   # ALL files of the link
    if delivered <= 0:
        await db.refund_quota(chat_id, cost)           # nothing was received -> don't burn the counts
        used = max(0, used - cost)
    _last_open[chat_id] = time.monotonic()             # cooldown starts after delivery finishes

    text = f"📊 LIMIT ▸ <b>{used}/{limit}</b> USED · <b>{max(0, limit - used)}</b> LEFT"
    if used >= limit:
        text += "\n🚫 LIMIT REACHED. RESETS 12:00 AM IST."
    if SUP.available():
        text += "\n🛟 VIDEO NOT PLAYING OR ANY PROBLEM? TAP BELOW."
    await call(bot.send_message, chat_id, text, parse_mode=ParseMode.HTML, reply_markup=SUP.markup(link["_id"]))


async def _deliver(bot, chat_id: int, link: dict) -> int:
    """Forward the link's files into the chat. Returns how many files the user actually received."""
    channel = S.get("db_channel")
    if not channel:
        await call(bot.send_message, chat_id, head("⚠️", "Not Ready", "THE BOT IS NOT SET UP YET. TRY AGAIN LATER."),
                   reply_markup=SUP.markup())
        return 0
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
                head("⏳", "Auto-Delete", hl(f"FILES VANISH IN <b>{minutes} MIN</b>. SAVE THEM BEFORE THEN.")), parse_mode=ParseMode.HTML)
            sent.append(notice.message_id)
        except TelegramError:
            pass
        await db.schedule_delete(chat_id, sent, minutes)
    if skipped:
        await call(bot.send_message, chat_id, f"ℹ️ <b>{skipped}</b> FILE(S) NO LONGER AVAILABLE.")
    return files


async def _unavailable(bot, chat_id):
    try:
        await bot.send_message(chat_id, head("⚠️", "Files Unavailable", "TRY AGAIN LATER."), reply_markup=SUP.markup())
    except TelegramError:
        pass


async def _config_problem(bot, channel, err):
    await notify_owner(
        bot, "dbch",
        head("⚠️", "Database Channel Error", f"<b>{esc(channel.get('title'))}</b>\n<code>{esc(err)}</code>\n\n"
             + hl("MAKE SURE THE BOT IS ADMIN THERE, OR SET THE CHANNEL AGAIN.")))
