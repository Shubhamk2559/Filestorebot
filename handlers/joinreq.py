"""
handlers/joinreq.py - Join Request Approval (channels + groups).

Owner / admin picks a channel or group in Settings -> Join Requests and can only switch it ON or OFF.
While ON, every join request is approved by the bot at once and SILENTLY: no message, no DM, nothing is sent.

Needs: the bot is admin in that chat with the "Invite Users" permission, and the chat's invite link /
join setting is "Request admin approval".

Setting: jr_chats = {"<chat_id>": {"title": str, "type": "channel|group|supergroup", "on": bool}}
"""
import logging

from telegram.error import BadRequest, Forbidden, TelegramError

from database.db import settings as S
from handlers import admin as A
from utils import keyboards as K
from utils.helpers import call
from utils.keyboards import grid, kb, nm, ub
from utils.ui import esc, head, note, onoff

log = logging.getLogger(__name__)

# errors that only mean "nothing to approve any more" - ignored quietly
_HARMLESS = ("user_already_participant", "hide_requester_missing", "chat_join_request_not_found",
             "user_channels_too_much", "user_is_blocked")


def _chats() -> dict:
    return S.get("jr_chats") or {}


def _label(c: dict) -> str:
    return f"{'📢' if c.get('type') == 'channel' else '👥'} {(c.get('title') or '')[:26]}"


# ===================================================================== the approver (no message is ever sent)

async def on_join_request(update, context):
    req = update.chat_join_request
    if not req:
        return
    cfg = _chats().get(str(req.chat.id))
    if not cfg or not cfg.get("on"):
        return                                     # OFF / not listed -> leave the request alone
    try:
        await call(context.bot.approve_chat_join_request, req.chat.id, req.from_user.id)
    except (BadRequest, Forbidden) as e:
        if not any(h in str(e).lower() for h in _HARMLESS):
            log.warning("Join request approve failed in %s: %s", req.chat.id, e)
    except TelegramError as e:
        log.warning("Join request approve error in %s: %s", req.chat.id, e)


# ===================================================================== panel

async def show_jr(update, msg=""):
    chats = _chats()
    rows = [[nm(f"{_label(c)} · {onoff(c.get('on'))}", f"adm:jrch:{cid}")] for cid, c in chats.items()]
    rows += grid([("➕ Add Chat", "adm:jradd")])
    on_n = sum(1 for c in chats.values() if c.get("on"))
    desc = (f"APPROVED SILENTLY, NO MESSAGE IS SENT.\nCHATS ▸ <b>{len(chats)}</b> · ON ▸ <b>{on_n}</b>\n\n"
            + ("TAP A CHAT TO SWITCH IT ON / OFF." if chats else "NO CHAT ADDED YET."))
    await A.show(update, note(msg) + head("✅", "Join Requests", desc), kb(rows, ub("🔙 Back", "adm:settings")))


async def show_jr_chat(update, cid: str, msg=""):
    c = _chats().get(cid)
    if not c:
        await show_jr(update)
        return
    on = bool(c.get("on"))
    await A.show(update,
                 note(msg) + f"{_label(c)}\n\nAUTO-APPROVE ▸ <b>{onoff(on)}</b>",
                 kb([[ub("🔕 Turn OFF" if on else "🔔 Turn ON", f"adm:jrtog:{cid}")],
                     [ub("🗑 Remove", f"adm:jrrm:{cid}")]], ub("🔙 Back", "adm:jr")))


@A.action("jr")
async def a_jr(update, context, args):
    await show_jr(update)


@A.action("jrch")
async def a_jrch(update, context, args):
    await show_jr_chat(update, args[0])


@A.action("jrtog")
async def a_jrtog(update, context, args):
    chats = dict(_chats())
    c = chats.get(args[0])
    if c:
        chats[args[0]] = {**c, "on": not c.get("on")}
        await S.set("jr_chats", chats)
    await show_jr_chat(update, args[0], "✅ UPDATED.")


@A.action("jrrm")
async def a_jrrm(update, context, args):
    chats = dict(_chats())
    chats.pop(args[0], None)
    await S.set("jr_chats", chats)
    await show_jr(update, "🗑 CHAT REMOVED.")


@A.action("jradd")
async def a_jradd(update, context, args):
    await A.ask_chat(update, context, "jradd",
                     head("➕", "Add Chat", "CHOOSE THE CHANNEL OR GROUP WHOSE JOIN REQUESTS THE BOT SHOULD APPROVE."),
                     channel=True, group=True)


@A.state("jradd")
async def st_jradd(update, context):
    chat = await A.resolve_channel(update, context, need_post=False)
    if not chat:
        return
    try:                                           # the bot must be allowed to approve requests
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        if getattr(me, "can_invite_users", None) is False:
            await A.warn(update, "GIVE THE BOT THE “INVITE USERS” PERMISSION THERE, THEN TRY AGAIN.")
            return
    except TelegramError:
        pass
    chats = dict(_chats())
    if str(chat.id) in chats:
        await A.done(update, context, "ℹ️ ALREADY IN THE LIST.", "jr")
        return
    chats[str(chat.id)] = {"title": chat.title or str(chat.id), "type": chat.type, "on": True}
    await S.set("jr_chats", chats)
    await A.done(update, context, f"✅ ADDED <b>{esc(chat.title or '')}</b> ▸ AUTO-APPROVE IS ON.", "jr")
