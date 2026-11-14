"""
utils/padding.py
Makes every text message that carries an inline keyboard as wide as its buttons.

Telegram sizes a message bubble by its text, but the inline buttons below it are usually wider, so short
messages ("Tap a link to view or revoke it.") look narrower than their keyboard. We append one line of
invisible Braille-blank characters (U+2800, not treated as whitespace by Telegram) so the bubble stretches.

install() patches ExtBot.send_message / edit_message_text once, so every call in the bot
(reply_text, edit_message_text, chat.send_message, bot.send_message...) is covered without touching
each call site. Media captions are not touched: a photo/video bubble is as wide as the media itself.

Tune with the PAD_WIDTH env var (0 turns it off):
  bubble still narrower than the buttons -> raise it
  empty extra line appears at the bottom  -> lower it
"""
import html
import logging
import re

from telegram import InlineKeyboardMarkup
from telegram.ext import ExtBot

import config

log = logging.getLogger(__name__)

BLANK = "⠀"
_TAG = re.compile(r"<[^>]+>")
_MAX_TEXT = 4096
_installed = False


def _longest_line(text: str) -> int:
    plain = html.unescape(_TAG.sub("", text))
    return max((len(line) for line in plain.split("\n")), default=0)


def pad(text, reply_markup=None):
    """Return `text` with an invisible padding line, only when it has an inline keyboard."""
    width = config.PAD_WIDTH
    if width <= 0 or not isinstance(text, str) or not isinstance(reply_markup, InlineKeyboardMarkup):
        return text
    if _longest_line(text) >= int(width * 1.3):        # already wide enough (or wrapping) - leave it
        return text
    if len(text) + 1 + width > _MAX_TEXT:
        return text
    return text + "\n" + BLANK * width


def install():
    """Patch ExtBot once. Safe to call repeatedly."""
    global _installed
    if _installed or config.PAD_WIDTH <= 0:
        return
    _installed = True

    orig_send = ExtBot.send_message
    orig_edit = ExtBot.edit_message_text

    async def send_message(self, chat_id, text, *args, **kwargs):
        text = pad(text, kwargs.get("reply_markup"))
        return await orig_send(self, chat_id, text, *args, **kwargs)

    async def edit_message_text(self, text, *args, **kwargs):
        text = pad(text, kwargs.get("reply_markup"))
        return await orig_edit(self, text, *args, **kwargs)

    ExtBot.send_message = send_message
    ExtBot.edit_message_text = edit_message_text
    log.info("Message width padding ON (PAD_WIDTH=%s)", config.PAD_WIDTH)
