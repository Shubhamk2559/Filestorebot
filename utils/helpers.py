"""
utils/helpers.py
Shared helpers: admin decorators, flood-wait retry, forwarded-message parsing,
caption templating, throttled owner notices.
"""
import asyncio
import html
import logging
import re
import time
from functools import wraps

from telegram import (ChatAdministratorRights, KeyboardButton, KeyboardButtonRequestChat,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove)
from telegram.error import RetryAfter, TelegramError

import config
from database.db import settings as S

log = logging.getLogger(__name__)


# ------------------------------------------------------------ permissions
def is_owner(uid: int) -> bool:
    return uid == config.OWNER_ID


def is_admin(uid: int) -> bool:
    return uid == config.OWNER_ID or uid in S.get("admins")


async def _deny(update):
    """Callback queries get a popup; plain messages from non-admins are silently ignored."""
    q = update.callback_query
    if q:
        try:
            await q.answer("⛔ Not allowed", show_alert=True)
        except TelegramError:
            pass


def admin_only(func):
    """Decorator for every admin handler."""
    @wraps(func)
    async def wrapper(update, context, *a, **kw):
        user = update.effective_user
        if not user or not is_admin(user.id):
            return await _deny(update)
        return await func(update, context, *a, **kw)
    return wrapper


def owner_only(func):
    """Decorator for owner-only screens (admin management)."""
    @wraps(func)
    async def wrapper(update, context, *a, **kw):
        user = update.effective_user
        if not user or not is_owner(user.id):
            return await _deny(update)
        return await func(update, context, *a, **kw)
    return wrapper


# ------------------------------------------------------------ flood control
def retry_seconds(e: RetryAfter) -> float:
    ra = e.retry_after
    return float(ra.total_seconds() if hasattr(ra, "total_seconds") else ra)


async def call(fn, *args, **kwargs):
    """Await a Bot API call, sleeping and retrying when Telegram says RetryAfter."""
    for _ in range(4):
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as e:
            wait = retry_seconds(e) + 1
            log.warning("Flood wait: sleeping %.0fs", wait)
            await asyncio.sleep(wait)
    return await fn(*args, **kwargs)  # last attempt: let the error propagate


_last_notice: dict = {}


async def notify_owner(bot, key: str, text: str, every: int = 3600):
    """Tell the owner about a config problem, at most once per `every` seconds per key."""
    now = time.time()
    if now - _last_notice.get(key, 0) < every:
        return
    _last_notice[key] = now
    try:
        await bot.send_message(config.OWNER_ID, text)
    except TelegramError:
        pass


# ------------------------------------------------------------ templating
def render_caption(template: str, user) -> str:
    """Fill {first_name} and {mention} placeholders (HTML-safe)."""
    return (template
            .replace("{first_name}", html.escape(user.first_name or ""))
            .replace("{mention}", user.mention_html()))


# ------------------------------------------------------------ parsing admin input
CANCEL_TEXT = "❌ Cancel"
_PICK_CHANNEL, _PICK_GROUP = 101, 102


def picker_keyboard(channel: bool = True, group: bool = False):
    """
    Reply keyboard with Telegram's native "Choose a Channel / Choose a Group" picker. The user taps a chat
    from their own list (or creates a new one) - no IDs. If the bot is not yet admin there, Telegram offers
    to add it with these rights. The pick arrives as a `chat_shared` service message.
    """
    ch_rights = ChatAdministratorRights(
        is_anonymous=False, can_manage_chat=True, can_delete_messages=False, can_manage_video_chats=False,
        can_restrict_members=False, can_promote_members=False, can_change_info=False,
        can_invite_users=True, can_post_messages=True)
    gr_rights = ChatAdministratorRights(
        is_anonymous=False, can_manage_chat=True, can_delete_messages=False, can_manage_video_chats=False,
        can_restrict_members=False, can_promote_members=False, can_change_info=False, can_invite_users=True)
    rows = []
    if channel:
        rows.append([KeyboardButton("📢 Choose a Channel", request_chat=KeyboardButtonRequestChat(
            _PICK_CHANNEL, chat_is_channel=True, bot_administrator_rights=ch_rights))])
    if group:
        rows.append([KeyboardButton("👥 Choose a Group", request_chat=KeyboardButtonRequestChat(
            _PICK_GROUP, chat_is_channel=False, bot_administrator_rights=gr_rights))])
    rows.append([KeyboardButton(CANCEL_TEXT)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def clear_picker(msg):
    """Remove the picker keyboard (a message can only carry one markup, so send + delete a tiny one)."""
    try:
        m = await msg.reply_text("✅", reply_markup=ReplyKeyboardRemove())
        await m.delete()
    except TelegramError:
        pass


def extract_chat_ref(msg):
    """Channel from the picker, a forwarded message, a numeric ID, @username or t.me/username link."""
    shared = getattr(msg, "chat_shared", None)
    if shared is not None:
        return shared.chat_id
    origin = msg.forward_origin
    if origin is not None:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        return chat.id if chat else None
    if msg.text:
        t = msg.text.strip()
        if re.fullmatch(r"-?\d+", t):
            return int(t)
        m = re.fullmatch(r"(?:https?://t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,})/?", t)
        if m:
            return "@" + m.group(1)
    return None


def extract_user_id(msg):
    """User id from a forwarded message or typed digits (None if hidden / invalid)."""
    origin = msg.forward_origin
    if origin is not None:
        user = getattr(origin, "sender_user", None)
        return user.id if user else None
    if msg.text and re.fullmatch(r"\d{5,}", msg.text.strip()):
        return int(msg.text.strip())
    return None


def extract_post_id(msg, channel_id: int):
    """Message id of a DB-channel post: a forward from that channel, or a t.me post link."""
    origin = msg.forward_origin
    if origin is not None:
        chat = getattr(origin, "chat", None)
        if chat is not None and chat.id == channel_id:
            return origin.message_id
        return None
    if msg.text:
        m = re.search(r"t\.me/(?:c/)?[\w]+/(\d+)", msg.text)
        if m:
            return int(m.group(1))
    return None
