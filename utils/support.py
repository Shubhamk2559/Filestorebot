"""
utils/support.py - tiny helpers every screen uses to point users to the Support Bot.

Nothing here talks to Telegram except hint(). All functions are cheap and safe to call anywhere:
when no Support Bot is connected (or it is switched OFF) they return nothing, so no button appears.

    SUP.available()            True when a Support Bot is connected and ON
    SUP.url(src)               https://t.me/<supportbot>[?start=l_<src>]    (src = link/post token -> context for you)
    SUP.row(src)               [[support button]] or []      -> keyboard.extend(SUP.row(token))
    SUP.markup(src)            a keyboard with only the support button (or None)
    SUP.attach(markup, src)    existing keyboard + support row (or None / unchanged when unavailable)
    SUP.attach_post(...)       same, but only when "Posts button" is ON (channel posts)
"""
import logging
import re
import time

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.error import TelegramError

from database.db import settings as S

log = logging.getLogger(__name__)

DEFAULT_BTN = "🛟 Video not playing? Contact Support"
_SRC_OK = re.compile(r"^[A-Za-z0-9_-]{4,60}$")        # start payload: 64 chars max, we add the "l_" prefix


def username() -> str:
    return (S.get("support_username") or "").lstrip("@")


def available() -> bool:
    return bool(S.get("support_enabled") and S.get("support_token") and username())


def url(src: str = "") -> str | None:
    if not available():
        return None
    base = f"https://t.me/{username()}"
    return f"{base}?start=l_{src}" if src and _SRC_OK.match(str(src)) else base


def button_text() -> str:
    return (S.get("support_btn") or "").strip() or DEFAULT_BTN


def button(src: str = "", text: str = None):
    u = url(src)
    return B(text or button_text(), url=u) if u else None


def row(src: str = "", text: str = None) -> list:
    b = button(src, text)
    return [[b]] if b else []


def markup(src: str = "", text: str = None):
    r = row(src, text)
    return M(r) if r else None


def attach(existing, src: str = "", text: str = None):
    """`existing` keyboard (InlineKeyboardMarkup or None) with the support row added at the bottom."""
    r = row(src, text)
    if not r:
        return existing
    rows = [list(x) for x in existing.inline_keyboard] if existing is not None else []
    return M(rows + r)


def posts_on() -> bool:
    return available() and bool(S.get("support_in_posts"))


def attach_post(existing, src: str = ""):
    """For channel posts (autoposter): only when the owner left "Posts button" ON."""
    return attach(existing, src) if posts_on() else existing


# ---------------------------------------------------------------- typed message in the main bot
_hint_at: dict = {}


async def hint(update) -> None:
    """A normal user typed something to the file bot while the Support Bot is connected:
    point them to the Support Bot (at most once per 20 s per user)."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    now = time.time()
    if now - _hint_at.get(user.id, 0) < 20:
        return
    if len(_hint_at) > 20000:
        _hint_at.clear()
    _hint_at[user.id] = now
    try:
        await msg.reply_text("🛟 <b>NEED HELP?</b>\n\nTAP THE BUTTON TO TALK TO SUPPORT.",
                             reply_markup=markup(), parse_mode="HTML")
    except TelegramError as e:
        log.debug("support hint failed: %s", e)
