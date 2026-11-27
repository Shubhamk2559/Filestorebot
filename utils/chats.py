"""
utils/chats.py - remembers the channels / groups where the bot is admin, so the panel can show them as
inline buttons inside the chat (no reply-keyboard picker).

Filled automatically by:
  * my_chat_member updates  (bot added / promoted / removed in a channel or group)
  * channel posts           (any channel that already posts through the bot)
  * resolve_channel()       (a channel the owner typed / forwarded once)
"""
import logging
import time

from database.db import db

log = logging.getLogger(__name__)
col = db["known_chats"]          # _id = chat id, title, username, type, at


async def remember(chat) -> None:
    if chat is None or chat.type not in ("channel", "group", "supergroup"):
        return
    try:
        await col.update_one(
            {"_id": chat.id},
            {"$set": {"title": chat.title or str(chat.id), "username": chat.username,
                      "type": chat.type, "at": time.time()}},
            upsert=True)
    except Exception:
        log.exception("could not remember chat")


async def forget(chat_id: int) -> None:
    try:
        await col.delete_one({"_id": chat_id})
    except Exception:
        log.exception("could not forget chat")


async def listing(channel: bool = True, group: bool = False, limit: int = 30) -> list:
    types = []
    if channel:
        types.append("channel")
    if group:
        types += ["group", "supergroup"]
    try:
        return [d async for d in col.find({"type": {"$in": types}}).sort("at", -1).limit(limit)]
    except Exception:
        log.exception("could not list chats")
        return []


async def on_my_chat_member(update, context):
    u = update.my_chat_member
    if not u:
        return
    if u.new_chat_member.status in ("administrator", "creator"):
        await remember(u.chat)
    else:
        await forget(u.chat.id)


async def on_channel_post(update, context):
    msg = update.channel_post
    if msg:
        await remember(msg.chat)
