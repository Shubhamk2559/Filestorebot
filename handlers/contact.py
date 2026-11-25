"""
handlers/contact.py - "message the owner" feature.

Any (non-admin) user can simply send a message to the bot: text, photo, video, file, voice ... anything.
The bot forwards it to the OWNER with the sender's name / @username / ID and two buttons:

    [↩️ Reply]  [🚫 Ban]

The owner answers in either way:
  1. tap ↩️ Reply, then send the answer (text or media) -> it is delivered to the user
  2. use Telegram's own "Reply" on the forwarded message (or on its header) -> delivered to the user

Banned users are ignored. A small per-user rate limit protects the owner from spam.
Owner message id -> user id is kept in MongoDB for 30 days (collection "contact_map").
"""
import datetime as dt
import html
import logging
import time

from telegram import InlineKeyboardButton as _Btn, InlineKeyboardMarkup as M, ReplyParameters
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError

import config
from database import db
from handlers.admin import state
from utils import keyboards as K
from utils.helpers import call, is_owner

log = logging.getLogger(__name__)

contact_map = db.db["contact_map"]       # _id = message id in the owner's chat, uid = the user, expire_at (TTL)
KEEP_DAYS = 30
RATE_N, RATE_SEC = 20, 60                # max 20 messages per user per minute are forwarded
ACK_EVERY = 30                           # "sent to owner" confirmation at most once per 30 s per user

_index_ready = False
_hits: dict = {}                         # uid -> [timestamps]
_last_ack: dict = {}                     # uid -> timestamp


def _kb(uid: int):
    return M([[_Btn("↩️ Reply", callback_data=f"ct:r:{uid}"),
               _Btn("🚫 Ban", callback_data=f"ct:b:{uid}")]])


def _allowed(uid: int) -> bool:
    now = time.time()
    hits = [t for t in _hits.get(uid, []) if now - t < RATE_SEC]
    if len(hits) >= RATE_N:
        _hits[uid] = hits
        return False
    hits.append(now)
    _hits[uid] = hits
    return True


async def _remember(uid: int, *msg_ids):
    global _index_ready
    if not _index_ready:
        await contact_map.create_index("expire_at", expireAfterSeconds=0)
        _index_ready = True
    exp = dt.datetime.utcnow() + dt.timedelta(days=KEEP_DAYS)
    for mid in msg_ids:
        await contact_map.update_one({"_id": mid}, {"$set": {"uid": uid, "expire_at": exp}}, upsert=True)


# ------------------------------------------------------------------ user -> owner
async def relay(update, context):
    """Called for every non-command private message of a non-admin user."""
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    if await db.touch_user(user.id):          # registers the user and tells if banned
        return
    if not _allowed(user.id):
        return

    name = html.escape(user.full_name or "User")
    uname = f" (@{html.escape(user.username)})" if user.username else ""
    header = (f"💬 <b>New message</b>\n"
              f"👤 <a href=\"tg://user?id={user.id}\">{name}</a>{uname}\n"
              f"🆔 <code>{user.id}</code>")
    try:
        head = await call(context.bot.send_message, config.OWNER_ID, header,
                          reply_markup=_kb(user.id), parse_mode=ParseMode.HTML)
        copy = await call(context.bot.copy_message, chat_id=config.OWNER_ID, from_chat_id=msg.chat_id,
                          message_id=msg.message_id,
                          reply_parameters=ReplyParameters(message_id=head.message_id),
                          reply_markup=_kb(user.id))
        await _remember(user.id, head.message_id, copy.message_id)
    except TelegramError as e:
        log.warning("Could not forward a user message to the owner: %s", e)
        return

    now = time.time()
    if now - _last_ack.get(user.id, 0) >= ACK_EVERY:
        _last_ack[user.id] = now
        await call(msg.reply_text, "✅ Your message has been sent to the owner. You will get the reply here.")


# ------------------------------------------------------------------ owner -> user
async def _deliver(context, uid: int, msg) -> bool:
    """Send the owner's message `msg` to the user. Returns True on success."""
    try:
        await call(context.bot.send_message, uid, "📩 <b>Reply from the owner</b>", parse_mode=ParseMode.HTML)
        await call(context.bot.copy_message, chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
        return True
    except Forbidden:
        await msg.reply_text("⚠️ This user has blocked the bot - message not delivered.")
    except TelegramError as e:
        await msg.reply_text(f"⚠️ Could not deliver: <code>{html.escape(str(e))}</code>")
    return False


async def try_native_reply(update, context) -> bool:
    """Owner used Telegram's Reply on a forwarded user message. True when it was handled here."""
    msg = update.effective_message
    doc = await contact_map.find_one({"_id": msg.reply_to_message.message_id})
    if not doc:
        return False
    if await _deliver(context, doc["uid"], msg):
        await msg.reply_text("✅ Reply sent.", reply_markup=_kb(doc["uid"]))
    return True


@state("ct_reply")
async def st_reply(update, context):
    msg = update.effective_message
    uid = context.user_data.get("ct_uid")
    if not uid:
        context.user_data.pop("state", None)
        return
    if await _deliver(context, uid, msg):
        context.user_data.pop("state", None)
        context.user_data.pop("ct_uid", None)
        await msg.reply_text("✅ Reply sent.", reply_markup=_kb(uid))


async def callback(update, context):
    """Inline buttons under forwarded messages: ct:r:<uid> (reply) / ct:b:<uid> (ban)."""
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("⛔ Not allowed", show_alert=True)
        return
    try:
        _, act, uid = q.data.split(":")
        uid = int(uid)
    except ValueError:
        await q.answer()
        return

    if act == "r":
        context.user_data["state"] = "ct_reply"
        context.user_data["ct_uid"] = uid
        await q.answer()
        await q.message.reply_text(f"↩️ Send your reply for <code>{uid}</code> (text or any media).",
                                   reply_markup=K.cancel(), parse_mode=ParseMode.HTML)
    elif act == "b":
        await db.set_banned(uid, True)
        await q.answer("🚫 User banned", show_alert=True)
