"""
handlers/support.py - the SUPPORT BOT.

A second Telegram bot (its own token) that runs inside this same process. The owner connects it from the panel
(Settings -> Support -> Connect Bot: just send the token). Users reach it from every "Support" button of the file bot.

WHY IT FIXES THE "EVERYTHING IS MIXED" PROBLEM
  * Every user gets his OWN TOPIC in a support group (a Telegram forum). All of that user's messages and your answers
    stay inside that one topic, so the old chat never scrolls away and nobody is mixed with anybody.
  * The first message of each topic is an info card: name, @username, ID, ticket number, and - when the user came
    from a "Video not playing" button - WHICH link/post he was on.
  * Topic title shows the state at a glance:   🔴 waiting for your answer   🟢 you answered   ✅ closed
  * You answer by simply typing inside the topic. Text starting with "#" is a private note (not sent to the user).

SETUP (owner):  1) Settings -> Support -> Connect Bot (send the token from @BotFather)
                2) make a group, turn ON Topics, add the Support Bot as admin with "Manage Topics"
                   -> it is detected automatically (or pick it under Support -> Group)

Without a group the bot still works in "DM mode": messages come to the owner's private chat with the bot
(each one with a name header). That mode is only a fallback - the topic group is the real fix.
"""
import asyncio
import datetime as dt
import logging
import re
import time

from pymongo import ReturnDocument
from telegram import LinkPreviewOptions, ReactionTypeEmoji, ReplyParameters
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, TelegramError
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, ChatMemberHandler, CommandHandler, Defaults,
                          MessageHandler, filters)

import config
from database import db
from database.db import settings as S
from handlers.admin import action, show, state, warn
from utils import keyboards as K
from utils import support as SUP
from utils.helpers import call, is_admin, is_owner, notify_owner
from utils.keyboards import grid, kb, nm, ub
from utils.links import share_link
from utils.ui import ago, esc, head, note, onoff

log = logging.getLogger(__name__)

users_c = db.db["support_users"]     # _id=user id: name, username, chat, thread, state, waiting, banned, tickets, src ...
chats_c = db.db["support_chats"]     # groups the Support Bot was added to (for the picker)
dmmap_c = db.db["support_dmmap"]     # DM mode only: owner's message id -> user id (TTL)

KEEP_DAYS = 30
RATE_N, RATE_SEC = 20, 60            # max 20 messages per user per minute are forwarded
ACK_EVERY = 30                       # "sent" confirmation at most once per 30 s per user
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
ALLOWED = ["message", "callback_query", "my_chat_member"]

_index_ready = False
_hits: dict = {}
_last_ack: dict = {}
_locks: dict = {}


class TopicError(Exception):
    """The Support Bot could not create / use a topic (missing right, topics turned off ...)."""


# ===================================================================== small helpers
def _lock(uid: int) -> asyncio.Lock:
    if len(_locks) > 10000:
        for k, v in list(_locks.items()):
            if not v.locked():
                _locks.pop(k, None)
    return _locks.setdefault(uid, asyncio.Lock())


def _allowed(uid: int) -> bool:
    now = time.time()
    hits = [t for t in _hits.get(uid, []) if now - t < RATE_SEC]
    ok = len(hits) < RATE_N
    if ok:
        hits.append(now)
    _hits[uid] = hits
    if len(_hits) > 20000:
        _hits.clear()
    return ok


async def _ensure_indexes():
    global _index_ready
    if _index_ready:
        return
    await users_c.create_index([("chat", 1), ("thread", 1)])
    await users_c.create_index("waiting")
    await dmmap_c.create_index("expire_at", expireAfterSeconds=0)
    _index_ready = True


def _gid():
    g = S.get("support_group")
    return g["id"] if g else None


def _title(doc: dict, icon: str) -> str:
    name = re.sub(r"\s+", " ", doc.get("name") or "User").strip()[:70]
    return f"{icon} {name} · {doc['_id']}"[:128]


def _card_kb(uid: int, banned: bool = False):
    return kb(grid([("✅ Unban" if banned else "🚫 Ban", f"s:{'u' if banned else 'b'}:{uid}"),
                    ("✔️ Close Ticket", f"s:c:{uid}")]))


def _info(doc: dict, ticket: int) -> str:
    uid = doc["_id"]
    un = f" (@{esc(doc['username'])})" if doc.get("username") else ""
    lines = [f"👤 <a href=\"tg://user?id={uid}\">{esc(doc.get('name') or 'User')}</a>{un}",
             f"🆔 <code>{uid}</code>"]
    if doc.get("lang"):
        lines.append(f"🌐 LANGUAGE ▸ <b>{esc(doc['lang']).upper()}</b>")
    lines.append(f"🕒 FIRST SEEN ▸ <b>{ago(doc.get('first'), 'NOW')}</b>" if ticket > 1 else "🕒 FIRST SEEN ▸ <b>NEW USER</b>")
    return "\n".join(lines)


def _card_text(doc: dict, ticket: int, reopened: bool = False) -> str:
    title = f"Ticket #{ticket}" + (" — Reopened" if reopened else "")
    return head("🔁" if reopened else "🎫", title,
                _info(doc, ticket) + "\n\n<i>TYPE HERE TO ANSWER. START WITH # FOR A PRIVATE NOTE.</i>")


async def _touch(user) -> dict:
    """Register / refresh the user and return his document."""
    await _ensure_indexes()
    now = time.time()
    return await users_c.find_one_and_update(
        {"_id": user.id},
        {"$set": {"name": user.full_name or "User", "username": user.username or "",
                  "lang": user.language_code or "", "last": now},
         "$setOnInsert": {"first": now, "banned": False, "tickets": 0, "waiting": False}},
        upsert=True, return_document=ReturnDocument.AFTER)


async def _alert(text: str, key: str = "sup"):
    """Tell the owner (through the MAIN bot) about a support-bot problem, throttled."""
    if runtime.main_bot is not None:
        await notify_owner(runtime.main_bot, key, head("⚠️", "Support Bot", text), every=900)


# ===================================================================== topics
async def _rename(bot, gid: int, thread: int, doc: dict, icon: str):
    try:
        await bot.edit_forum_topic(gid, thread, name=_title(doc, icon))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.debug("topic rename failed: %s", e)
    except TelegramError as e:
        log.debug("topic rename failed: %s", e)


async def _src_card(bot, gid: int, thread: int, token: str):
    """Tell the owner WHICH link / post the user came from."""
    link = await db.get_link(token)
    label = (link or {}).get("label") or "UNKNOWN LINK"
    main = runtime.main_bot.username if runtime.main_bot else ""
    url = share_link(main, token) if main else ""
    body = f"<b>{esc(label)}</b>" + (f"\n{esc(url)}" if url else "")
    try:
        await call(bot.send_message, gid, head("📎", "Came From", body), message_thread_id=thread,
                   parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramError as e:
        log.debug("source card failed: %s", e)


async def _open_topic(bot, gid: int, doc: dict) -> int:
    """Thread id of the user's topic - created (with the info card) or reopened when needed. Call under the user's lock."""
    uid = doc["_id"]
    fresh = await users_c.find_one({"_id": uid}) or doc
    thread = fresh.get("thread") if fresh.get("chat") == gid else None

    if thread and fresh.get("state") == "closed":                      # user writes again -> reopen
        try:
            await bot.reopen_forum_topic(gid, thread)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                thread = None                                          # topic was deleted -> make a new one
        if thread:
            n = int(fresh.get("tickets") or 0) + 1
            await users_c.update_one({"_id": uid}, {"$set": {"state": "open", "waiting": True}, "$inc": {"tickets": 1}})
            await _rename(bot, gid, thread, fresh, "🔴")
            await call(bot.send_message, gid, _card_text(fresh, n, reopened=True), message_thread_id=thread,
                       reply_markup=_card_kb(uid, bool(fresh.get("banned"))), parse_mode=ParseMode.HTML)
            return thread
    if thread:
        return thread

    n = int(fresh.get("tickets") or 0) + 1
    try:
        topic = await call(bot.create_forum_topic, gid, _title(fresh, "🔴"))
    except (BadRequest, Forbidden) as e:
        raise TopicError(str(e)) from e
    thread = topic.message_thread_id
    await users_c.update_one({"_id": uid}, {"$set": {"chat": gid, "thread": thread, "state": "open", "waiting": True},
                                            "$inc": {"tickets": 1}})
    card = await call(bot.send_message, gid, _card_text(fresh, n), message_thread_id=thread,
                      reply_markup=_card_kb(uid, bool(fresh.get("banned"))), parse_mode=ParseMode.HTML)
    try:
        await bot.pin_chat_message(gid, card.message_id, disable_notification=True)
    except TelegramError:
        pass                                                           # no "pin" right: not important
    return thread


async def _set_waiting(bot, gid: int, uid: int, waiting: bool):
    """Flip the 🔴 / 🟢 state of a topic (only when it really changes -> few Telegram calls)."""
    doc = await users_c.find_one_and_update(
        {"_id": uid, "waiting": {"$ne": waiting}, "state": {"$ne": "closed"}}, {"$set": {"waiting": waiting}},
        return_document=ReturnDocument.AFTER)
    if doc and doc.get("thread") and doc.get("chat") == gid:
        await _rename(bot, gid, doc["thread"], doc, "🔴" if waiting else "🟢")


# ===================================================================== user -> owner
async def _relay_group(bot, gid: int, doc: dict, msg, src_pending: str = ""):
    for attempt in range(2):
        thread = await _open_topic(bot, gid, doc)
        try:
            await call(bot.copy_message, chat_id=gid, from_chat_id=msg.chat_id, message_id=msg.message_id,
                       message_thread_id=thread)
            break
        except BadRequest as e:
            if "thread" in str(e).lower() and attempt == 0:           # topic was deleted by hand -> recreate
                await users_c.update_one({"_id": doc["_id"]}, {"$unset": {"thread": "", "chat": ""}})
                doc = await users_c.find_one({"_id": doc["_id"]}) or doc
                continue
            raise
    if src_pending:
        await _src_card(bot, gid, thread, src_pending)
        await users_c.update_one({"_id": doc["_id"]}, {"$unset": {"src": ""}})
    await _set_waiting(bot, gid, doc["_id"], True)


async def _relay_dm(bot, doc: dict, msg):
    """Fallback without a group: header + the message in the owner's private chat with this bot."""
    uid = doc["_id"]
    header = head("💬", "New Message", _info(doc, int(doc.get("tickets") or 1)))
    top = await call(bot.send_message, config.OWNER_ID, header, reply_markup=_card_kb(uid, False), parse_mode=ParseMode.HTML)
    cp = await call(bot.copy_message, chat_id=config.OWNER_ID, from_chat_id=msg.chat_id, message_id=msg.message_id,
                    reply_parameters=ReplyParameters(message_id=top.message_id))
    exp = dt.datetime.utcnow() + dt.timedelta(days=KEEP_DAYS)
    for mid in (top.message_id, cp.message_id):
        await dmmap_c.update_one({"_id": mid}, {"$set": {"uid": uid, "expire_at": exp}}, upsert=True)


async def on_user_message(update, context):
    user, msg = update.effective_user, update.effective_message
    if not user or not msg or user.is_bot:
        return
    if is_admin(user.id):
        await _admin_private(update, context)
        return
    if not S.get("support_enabled"):
        await msg.reply_text(head("🛟", "Support Offline", "SUPPORT IS CLOSED RIGHT NOW. PLEASE TRY LATER."))
        return

    async with _lock(user.id):                                         # keeps one user's messages in order
        doc = await _touch(user)
        if doc.get("banned") or not _allowed(user.id):
            return
        src, gid, sent = doc.get("src") or "", _gid(), False
        if gid:
            try:
                await _relay_group(context.bot, gid, doc, msg, src)
                sent = True
            except TopicError as e:
                log.warning("Support topic problem: %s", e)
                await _alert(f"<code>{esc(e)}</code>\n\nMAKE THE SUPPORT BOT <b>ADMIN WITH MANAGE TOPICS</b> IN THE "
                             "GROUP AND TURN <b>TOPICS</b> ON. MESSAGES ARE COMING TO YOUR PRIVATE CHAT UNTIL THEN.")
            except TelegramError as e:
                log.warning("Support relay to group failed: %s", e)
        if not sent:
            try:
                await _relay_dm(context.bot, doc, msg)
                sent = True
            except TelegramError as e:
                log.warning("Support relay to owner failed: %s", e)
                await _alert(f"<code>{esc(e)}</code>\n\nOPEN THE SUPPORT BOT ONCE AND TAP START, "
                             "OR FIX THE GROUP (Settings -> Support).")
        if not sent:
            await msg.reply_text("⚠️ <b>COULD NOT SEND.</b> PLEASE TRY AGAIN LATER.", parse_mode=ParseMode.HTML)
            return

    now = time.time()
    if now - _last_ack.get(user.id, 0) >= ACK_EVERY:
        _last_ack[user.id] = now
        await call(msg.reply_text, "✅ <b>SENT TO SUPPORT</b>\n\nTHE REPLY ARRIVES HERE.", parse_mode=ParseMode.HTML)


# ===================================================================== owner -> user
async def _deliver(bot, uid: int, msg):
    """Copy the owner's message to the user. Returns None on success, else a short error text."""
    try:
        await call(bot.copy_message, chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
        return None
    except Forbidden:
        return "USER BLOCKED THE BOT"
    except TelegramError as e:
        return str(e)


async def on_group_message(update, context):
    msg, user = update.effective_message, update.effective_user
    gid = _gid()
    if not msg or not gid or msg.chat_id != gid or not user or user.is_bot or not is_admin(user.id):
        return
    thread = msg.message_thread_id
    if not thread:
        return                                                         # "General" topic: not a ticket
    doc = await users_c.find_one({"chat": gid, "thread": thread})
    if not doc:
        return
    if (msg.text or msg.caption or "").lstrip().startswith("#"):       # private note for the team
        return
    err = await _deliver(context.bot, doc["_id"], msg)
    if err:
        await call(context.bot.send_message, gid, f"⚠️ <b>NOT DELIVERED</b> ▸ <code>{esc(err)}</code>",
                   message_thread_id=thread, parse_mode=ParseMode.HTML)
        return
    try:
        await context.bot.set_message_reaction(gid, msg.message_id, reaction=[ReactionTypeEmoji("👍")])
    except TelegramError:
        pass                                                           # reactions are only a nicety
    await _set_waiting(context.bot, gid, doc["_id"], False)


async def _admin_private(update, context):
    """An admin writes to the Support Bot in private: DM-mode reply, otherwise a short help text."""
    msg = update.effective_message
    if msg.reply_to_message:
        row = await dmmap_c.find_one({"_id": msg.reply_to_message.message_id})
        if row:
            err = await _deliver(context.bot, row["uid"], msg)
            await msg.reply_text("✅ <b>REPLY SENT.</b>" if not err else f"⚠️ <b>NOT DELIVERED</b> ▸ <code>{esc(err)}</code>",
                                 parse_mode=ParseMode.HTML)
            return
    await msg.reply_text(_admin_help(), parse_mode=ParseMode.HTML)


def _admin_help() -> str:
    g = S.get("support_group")
    if g:
        return head("🛟", "Support Bot", f"GROUP ▸ <b>{esc(g['title'])}</b>\n\nANSWER USERS INSIDE THEIR TOPIC IN THAT GROUP.")
    return head("🛟", "Support Bot", "NO GROUP SET - MESSAGES COME HERE (DM MODE).\n\n"
                "TO GET ONE TOPIC PER USER: CREATE A GROUP, TURN ON <b>TOPICS</b>, ADD ME AS ADMIN "
                "WITH <b>MANAGE TOPICS</b>.")


# ===================================================================== /start and buttons
async def on_start(update, context):
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    if is_admin(user.id):
        await msg.reply_text(_admin_help(), parse_mode=ParseMode.HTML)
        return
    doc = await _touch(user)
    if doc.get("banned"):
        await msg.reply_text(head("🚫", "Banned", "YOU CANNOT USE SUPPORT."))
        return
    src = context.args[0][2:] if context.args and context.args[0].startswith("l_") else ""
    about = ""
    if src:
        link = await db.get_link(src)
        if link:
            await users_c.update_one({"_id": user.id}, {"$set": {"src": src}})
            about = f"\n\n📎 ABOUT ▸ <b>{esc(link.get('label') or 'YOUR LINK')}</b>"
    await msg.reply_text(head("🛟", "Support", "TELL US YOUR PROBLEM IN A MESSAGE.\n"
                                               "A SCREENSHOT OR SCREEN RECORDING HELPS A LOT.\n"
                                               "THE REPLY ARRIVES RIGHT HERE." + about), parse_mode=ParseMode.HTML)


async def on_button(update, context):
    """s:b:<uid> ban · s:u:<uid> unban · s:c:<uid> close ticket (admins only)."""
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ NOT ALLOWED", show_alert=True)
        return
    try:
        _, act, uid = q.data.split(":")
        uid = int(uid)
    except ValueError:
        await q.answer()
        return
    if act in ("b", "u"):
        banned = act == "b"
        await users_c.update_one({"_id": uid}, {"$set": {"banned": banned}}, upsert=True)
        await q.answer("🚫 USER BANNED" if banned else "✅ USER UNBANNED", show_alert=True)
        try:
            await q.edit_message_reply_markup(_card_kb(uid, banned))
        except TelegramError:
            pass
    elif act == "c":
        doc = await users_c.find_one_and_update({"_id": uid}, {"$set": {"state": "closed", "waiting": False}},
                                                return_document=ReturnDocument.AFTER)
        if doc and doc.get("thread") and doc.get("chat"):
            await _rename(context.bot, doc["chat"], doc["thread"], doc, "✅")
            try:
                await context.bot.close_forum_topic(doc["chat"], doc["thread"])
            except TelegramError as e:
                log.debug("close topic failed: %s", e)
        await q.answer("✅ TICKET CLOSED")
        try:
            await context.bot.send_message(uid, head("✅", "Ticket Closed", "YOUR ISSUE WAS MARKED AS SOLVED.\n"
                                                                         "MESSAGE US AGAIN ANYTIME."), parse_mode=ParseMode.HTML)
        except TelegramError:
            pass


# ===================================================================== groups
async def _check_group(bot, gid: int):
    """-> (title, None) when the group is usable, else (None, reason)."""
    try:
        chat = await bot.get_chat(gid)
    except TelegramError as e:
        return None, f"CAN'T OPEN THAT GROUP ▸ <code>{esc(e)}</code>"
    if chat.type != "supergroup" or not getattr(chat, "is_forum", False):
        return None, "TOPICS ARE OFF. OPEN THE GROUP -> EDIT -> TURN ON <b>TOPICS</b>, THEN TRY AGAIN."
    try:
        me = await bot.get_chat_member(gid, bot.id)
    except TelegramError as e:
        return None, f"ADD THE SUPPORT BOT TO THE GROUP FIRST ▸ <code>{esc(e)}</code>"
    if me.status != "administrator" or not getattr(me, "can_manage_topics", False):
        return None, "MAKE THE SUPPORT BOT <b>ADMIN</b> WITH THE <b>MANAGE TOPICS</b> RIGHT."
    return chat.title or str(gid), None


async def on_my_chat_member(update, context):
    u = update.my_chat_member
    if not u or u.chat.type not in ("group", "supergroup"):
        return
    chat = u.chat
    if u.new_chat_member.status not in ("member", "administrator"):
        await chats_c.delete_one({"_id": chat.id})
        return
    await chats_c.update_one({"_id": chat.id}, {"$set": {"title": chat.title or str(chat.id), "type": chat.type,
                                                         "at": time.time()}}, upsert=True)
    # Added by an admin and ready (forum + Manage Topics) while no group is set -> use it automatically.
    if u.new_chat_member.status == "administrator" and not _gid() and is_admin(u.from_user.id):
        title, err = await _check_group(context.bot, chat.id)
        if not err:
            await S.set("support_group", {"id": chat.id, "title": title})
            if runtime.main_bot is not None:
                try:
                    await runtime.main_bot.send_message(
                        config.OWNER_ID, head("✅", "Support Group Set", f"<b>{esc(title)}</b>\n\nEACH USER NOW GETS HIS OWN TOPIC THERE."))
                except TelegramError:
                    pass


async def on_migrate(update, context):
    """Turning on Topics converts a basic group to a supergroup with a NEW id - follow it."""
    msg = update.effective_message
    old, new = msg.chat_id, msg.migrate_to_chat_id
    if not new:
        return
    await chats_c.delete_one({"_id": old})
    g = S.get("support_group")
    if g and g["id"] == old:
        await S.set("support_group", {"id": new, "title": g["title"]})


# ===================================================================== the second bot (runtime)
class Runtime:
    """Starts / stops the Support Bot application next to the main bot."""

    def __init__(self):
        self.app = None
        self.main_bot = None
        self.error = ""

    @property
    def running(self) -> bool:
        return self.app is not None

    def _build(self, token: str):
        app = (ApplicationBuilder().token(token)
               .defaults(Defaults(parse_mode=ParseMode.HTML))
               .concurrent_updates(64)
               .connection_pool_size(64).pool_timeout(15).connect_timeout(10).read_timeout(30).write_timeout(30)
               .get_updates_connection_pool_size(2).get_updates_read_timeout(40).get_updates_connect_timeout(10)
               .build())
        private = filters.ChatType.PRIVATE
        groups = filters.ChatType.GROUPS
        app.add_handler(CommandHandler("start", on_start, filters=private))
        app.add_handler(CallbackQueryHandler(on_button, pattern=r"^s:"))
        app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(groups & filters.StatusUpdate.MIGRATE, on_migrate))
        app.add_handler(MessageHandler(private & ~filters.COMMAND, on_user_message))
        app.add_handler(MessageHandler(groups & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_group_message))
        app.add_error_handler(self._on_error)
        return app

    async def _on_error(self, update, context):
        if isinstance(context.error, Conflict):
            log.warning("Support bot: another instance uses this token - only one may run")
        elif isinstance(context.error, NetworkError):
            log.warning("Support bot network error: %s", context.error)
        else:
            log.error("Support bot error", exc_info=context.error)

    async def start(self, token: str) -> str:
        """Start polling with `token`; returns the bot's username. Raises on a bad token."""
        await self.stop()
        app = self._build(token)
        try:
            await app.initialize()                      # get_me -> InvalidToken for a wrong token
            await app.start()
            await app.updater.start_polling(allowed_updates=ALLOWED, timeout=30)
        except Exception:
            await self._teardown(app)
            raise
        self.app, self.error = app, ""
        log.info("Support bot @%s started", app.bot.username)
        return app.bot.username

    @staticmethod
    async def _teardown(app):
        for step in (app.updater.stop, app.stop, app.shutdown):      # each one is allowed to fail (not started yet)
            try:
                await step()
            except Exception as e:
                log.debug("support bot stop: %s", e)

    async def stop(self):
        app, self.app = self.app, None
        if app:
            await self._teardown(app)

    async def boot(self, main_bot):
        """Called at startup: reconnect the saved Support Bot (never raises)."""
        self.main_bot = main_bot
        token = S.get("support_token")
        if not token:
            return
        try:
            name = await self.start(token)
            if name != S.get("support_username"):
                await S.set("support_username", name)
        except Exception as e:
            self.error = str(e)
            log.warning("Support bot could not start: %s", e)
            await _alert(f"<code>{esc(e)}</code>\n\nCONNECT THE BOT AGAIN: Settings -> Support.", key="sup_boot")


runtime = Runtime()


# ===================================================================== admin panel: Settings -> Support
async def _panel(update, msg: str = ""):
    waiting = await users_c.count_documents({"waiting": True, "state": {"$ne": "closed"}})
    g = S.get("support_group")
    uname = S.get("support_username")
    lines = [
        f"STATUS ▸ <b>{onoff(S.get('support_enabled'))}</b>" + ("" if runtime.running or not S.get("support_token") else " (BOT NOT RUNNING)"),
        f"BOT ▸ <b>@{esc(uname)}</b>" if uname else "BOT ▸ <b>NOT CONNECTED</b>",
        f"GROUP ▸ <b>{esc(g['title'])}</b>" if g else "GROUP ▸ <b>NOT SET</b> (DM MODE)",
        f"POST BUTTON ▸ <b>{onoff(S.get('support_in_posts'))}</b>",
        f"WAITING FOR YOU ▸ <b>{waiting}</b>",
    ]
    rows = [("🔑 Change Bot" if S.get("support_token") else "🔑 Connect Bot", "adm:supbot"),
            ("👥 Group", "adm:supgrp"),
            ("⏸ Turn Off" if S.get("support_enabled") else "▶️ Turn On", "adm:suptog"),
            (f"📌 Posts: {onoff(S.get('support_in_posts'))}", "adm:suppost"),
            ("✏️ Button Text", "adm:supbtn")]
    if S.get("support_token"):
        rows.append(("🔌 Disconnect", "adm:supdis"))
    await show(update, head("🛟", "Support", note(msg) + "\n".join(lines)), kb(grid(rows), ub("🔙 Back", "adm:settings")))


@action("sup")
async def a_sup(update, context, args):
    if not is_owner(update.effective_user.id):
        return
    await _panel(update)


@action("supbot")
async def a_supbot(update, context, args):
    if not is_owner(update.effective_user.id):
        return
    context.user_data["state"] = "sup_token"
    await show(update, head("🔑", "Support Bot", "1. OPEN @BOTFATHER -> /newbot (A NEW, SEPARATE BOT).\n"
                                                 "2. SEND ME ITS TOKEN HERE.\n\n"
                                                 "<i>I DELETE YOUR MESSAGE RIGHT AFTER READING THE TOKEN.</i>"), K.cancel())


@state("sup_token")
async def st_token(update, context):
    msg = update.effective_message
    if not is_owner(update.effective_user.id):
        return
    token = (msg.text or "").strip()
    try:
        if not TOKEN_RE.match(token):
            await warn(update, "THAT DOES NOT LOOK LIKE A BOT TOKEN. SEND IT AS COPIED FROM @BOTFATHER.")
            return
        if token == config.BOT_TOKEN:
            await warn(update, "THAT IS THE FILE BOT'S OWN TOKEN. THE SUPPORT BOT MUST BE A SEPARATE BOT.")
            return
        wait = await msg.reply_text("⏳ CONNECTING…")
        try:
            name = await runtime.start(token)
        except Exception as e:
            await wait.edit_text(f"⚠️ <b>TOKEN NOT ACCEPTED</b>\n\n<code>{esc(e)}</code>", parse_mode=ParseMode.HTML,
                                 reply_markup=K.cancel())
            return
        runtime.main_bot = context.bot
        await S.set("support_token", token)
        await S.set("support_username", name)
        await S.set("support_enabled", True)
        context.user_data.pop("state", None)
        todo = "" if S.get("support_group") else (
            "\n\nNEXT: MAKE A GROUP, TURN ON <b>TOPICS</b>, ADD @" + esc(name) + " AS ADMIN WITH <b>MANAGE TOPICS</b>. "
            "I DETECT IT AUTOMATICALLY.")
        await wait.edit_text(head("✅", "Support Bot Connected", f"@{esc(name)} IS LIVE. SUPPORT BUTTONS ARE ON." + todo),
                             parse_mode=ParseMode.HTML, reply_markup=K.back("sup"))
    finally:
        try:
            await msg.delete()                          # never leave the token in the chat
        except TelegramError:
            pass


@action("supdis")
async def a_supdis(update, context, args):
    if not is_owner(update.effective_user.id):
        return
    await runtime.stop()
    for k in ("support_token", "support_username"):
        await S.set(k, "")
    await S.set("support_enabled", False)
    await _panel(update, "🔌 DISCONNECTED. NO SUPPORT BUTTONS ARE SHOWN NOW.")


@action("suptog")
async def a_suptog(update, context, args):
    if not is_owner(update.effective_user.id):
        return
    if not S.get("support_token"):
        await _panel(update, "⚠️ CONNECT A BOT FIRST.")
        return
    await S.set("support_enabled", not S.get("support_enabled"))
    await _panel(update, "✅ SAVED.")


@action("suppost")
async def a_suppost(update, context, args):
    if not is_owner(update.effective_user.id):
        return
    await S.set("support_in_posts", not S.get("support_in_posts"))
    await _panel(update, "✅ SAVED.")


@action("supbtn")
async def a_supbtn(update, context, args):
    if not is_owner(update.effective_user.id):
        return
    context.user_data["state"] = "sup_btn"
    await show(update, head("✏️", "Button Text", f"CURRENT ▸ <b>{esc(SUP.button_text())}</b>\n\n"
                                                  "SEND THE NEW TEXT FOR THE SUPPORT BUTTON (MAX 50 CHARACTERS).\n"
                                                  "SEND <code>default</code> TO RESET."), K.cancel())


@state("sup_btn")
async def st_btn(update, context):
    msg = update.effective_message
    if not is_owner(update.effective_user.id):
        return
    text = (msg.text or "").strip()
    if not text or len(text) > 50:
        await warn(update, "SEND A TEXT OF 1-50 CHARACTERS.")
        return
    await S.set("support_btn", "" if text.lower() == "default" else text)
    context.user_data.pop("state", None)
    await msg.reply_text("✅ SAVED.", reply_markup=K.back("sup"))


@action("supgrp")
async def a_supgrp(update, context, args):
    if not is_owner(update.effective_user.id):
        return
    if not runtime.running:
        await _panel(update, "⚠️ CONNECT THE SUPPORT BOT FIRST.")
        return
    context.user_data["state"] = "sup_group"
    cur = S.get("support_group")
    items = [d async for d in chats_c.find({}).sort("at", -1).limit(20)]
    rows = [[nm(f"👥 {(d.get('title') or str(d['_id']))[:40]}", f"adm:supsel:{d['_id']}")] for d in items]
    hint = ("TAP YOUR SUPPORT GROUP, OR SEND ITS ID." if items else
            f"NO GROUP YET. ADD @{esc(S.get('support_username'))} TO A GROUP (TOPICS ON) AS ADMIN WITH MANAGE TOPICS, "
            "THEN OPEN THIS SCREEN AGAIN.")
    now = f"<b>{esc(cur['title'])}</b>" if cur else "NOT SET"
    await show(update, head("👥", "Support Group", f"CURRENT ▸ {now}\n\n{hint}"), kb(rows, ub("❌ Cancel", "adm:cancel")))


async def _use_group(update, context, gid: int):
    title, err = await _check_group(runtime.app.bot, gid)
    if err:
        await show(update, f"⚠️ {err}", K.cancel())
        return
    await S.set("support_group", {"id": gid, "title": title})
    context.user_data.pop("state", None)
    await show(update, head("✅", "Support Group Set", f"<b>{esc(title)}</b>\n\nEACH USER NOW GETS HIS OWN TOPIC THERE."),
               K.back("sup"))


@action("supsel")
async def a_supsel(update, context, args):
    if not is_owner(update.effective_user.id) or not runtime.running:
        return
    try:
        gid = int(args[0])
    except (IndexError, ValueError):
        return
    await _use_group(update, context, gid)


@state("sup_group")
async def st_group(update, context):
    if not is_owner(update.effective_user.id) or not runtime.running:
        return
    text = (update.effective_message.text or "").strip()
    if not re.fullmatch(r"-?\d{5,}", text):
        await warn(update, "SEND THE GROUP ID (A NUMBER LIKE -1001234567890) OR TAP A GROUP ABOVE.")
        return
    await _use_group(update, context, int(text))
