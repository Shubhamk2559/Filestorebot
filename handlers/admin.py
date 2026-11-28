"""
handlers/admin.py
The whole admin panel. Nothing here is typed as a command:

  callback_router  every inline-button press whose data starts with "adm:"
                   (format "adm:<action>:<arg>:<arg>"), dispatched through ACTIONS.
  message_router   every non-command private message. For an admin it is either
                   (a) the answer to a pending prompt -> STATES (conversation state machine), or
                   (b) a file upload -> handlers.upload.

State machine: context.user_data["state"] = name of the prompt being answered.
Every prompt has a CANCEL button; every result screen has a BACK button.
Design: utils/ui.py (text) + utils/keyboards.py (buttons).
"""
import logging
import re

import config

from telegram import LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from database import db
from database.db import settings as S
from handlers import broadcast, upload
from utils import ads
from utils import chats as CH
from utils import keyboards as K
from utils.helpers import (
    admin_only,
    owner_only,
    is_admin,
    is_owner,
    extract_chat_ref,
    extract_user_id,
    extract_post_id,
    drop_channel_msgs,
)
from utils.keyboards import grid, kb, nm, pager, ub
from utils.links import share_link
from utils.ui import ago, esc, head, hl, mb, note, onoff, styled

log = logging.getLogger(__name__)

NOPREVIEW = LinkPreviewOptions(is_disabled=True)
PAGE = 8
MAX_RANGE = 10000
TEMP_KEYS = ("state", "batch_ids", "batch_status", "batch_token", "restore_status", "restore_n", "range_first",
             "bc_msg", "ap_cid", "ap_items", "ap_status", "ap_src", "ap_ts", "tr_dl", "tr_src", "tr_dst", "tr_cl",
             "tr_wp", "pick_state", "pick_kind", "picked_chat", "ct_uid",
             "zv_cid", "zv_zips", "zv_bid", "zv_thumbs", "zv_skip", "zv_status", "zv_ts")

ACTIONS = {}
STATES = {}
SELF_ANSWER = {"batchdone"}


def action(name):
    def deco(fn):
        ACTIONS[name] = fn
        return fn
    return deco


def state(name):
    def deco(fn):
        STATES[name] = fn
        return fn
    return deco


# ===================================================================== helpers

async def show(update, text, markup=None):
    """Edit the panel message in place, or send a new one."""
    q = update.callback_query
    text = styled(text)     # title stays as is; everything below it -> quote + small font
    opts = dict(reply_markup=markup, parse_mode=ParseMode.HTML, link_preview_options=NOPREVIEW)
    try:
        if q:
            await q.edit_message_text(text, **opts)
        else:
            await update.effective_message.reply_text(text, **opts)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await update.effective_chat.send_message(text, **opts)


async def ask(update, context, state_name, text):
    """Open a conversational prompt."""
    context.user_data["state"] = state_name
    await show(update, text, K.cancel())


async def warn(update, text):
    """Typed answer was not usable: say why, keep the prompt open."""
    await update.effective_message.reply_text(f"⚠️ {text}", reply_markup=K.cancel())


async def done(update, context, text, to="home"):
    """Typed answer accepted: close the prompt and show the result with a BACK button."""
    context.user_data.pop("state", None)
    await update.effective_message.reply_text(text, reply_markup=K.back(to))


async def ask_chat(update, context, state_name, text, channel=True, group=False):
    """Prompt with the bot's known channels / groups as buttons right in the chat."""
    context.user_data["state"] = state_name
    context.user_data["pick_state"] = state_name
    context.user_data["pick_kind"] = (channel, group)
    items = await CH.listing(channel, group)
    rows = [[nm(f"{'📢' if d.get('type') == 'channel' else '👥'} {(d.get('title') or str(d['_id']))[:40]}",
                f"adm:pickch:{d['_id']}")] for d in items]
    hint = ("TAP A CHANNEL, OR FORWARD A POST / SEND ITS @USERNAME OR ID." if items else
            "NO CHANNEL SAVED YET. ADD THE BOT AS ADMIN, OR FORWARD A POST / SEND ITS @USERNAME OR ID.")
    await show(update, f"{text}\n\n{hint}", kb(rows, ub("❌ Cancel", "adm:cancel")))


@action("pickch")
async def a_pickch(update, context, args):
    st = context.user_data.get("pick_state")
    if st not in STATES or not args:
        await show(update, "⚠️ SESSION EXPIRED. START AGAIN.", K.back())
        return
    try:
        cid = int(args[0])
    except ValueError:
        await show(update, "⚠️ INVALID CHANNEL.", K.back())
        return
    context.user_data["state"] = st
    context.user_data["picked_chat"] = cid
    await STATES[st](update, context)


def panel_text() -> str:
    return head("⚙️", "Admin Control Panel", "SELECT AN OPTION TO MANAGE YOUR BOT.")


async def send_panel(update, context):
    """Used by /start for admins."""
    await update.effective_message.reply_text(
        panel_text(),
        reply_markup=K.main_panel(is_owner(update.effective_user.id)),
        parse_mode=ParseMode.HTML,
    )


async def bot_admin_status(bot, chat_id, need_post=False):
    me = await bot.get_me()
    member = await bot.get_chat_member(chat_id, me.id)
    if member.status not in ("administrator", "creator"):
        return False, "THE BOT IS NOT AN ADMIN THERE. ADD IT AS ADMIN AND TRY AGAIN."
    if need_post and getattr(member, "can_post_messages", None) is False:
        return False, "THE BOT NEEDS THE “POST MESSAGES” PERMISSION IN THAT CHANNEL."
    return True, ""


async def resolve_channel(update, context, need_post: bool):
    """Resolve a channel from an inline pick, a forwarded message, ID, or username."""
    msg = update.effective_message
    picked = context.user_data.pop("picked_chat", None)
    ref = picked if picked is not None else extract_chat_ref(msg)

    if ref is None:
        await warn(update, "TAP A CHANNEL ABOVE, OR FORWARD A POST / SEND ITS @USERNAME OR ID.")
        return None
    try:
        chat = await context.bot.get_chat(ref)
        ok, why = await bot_admin_status(context.bot, chat.id, need_post)
    except TelegramError as e:
        await warn(update, f"CANNOT ACCESS THIS CHAT ▸ <code>{esc(e)}</code>\nADD THE BOT AS ADMIN AND TRY AGAIN.")
        return None
    if not ok:
        await warn(update, why)
        return None
    await CH.remember(chat)
    return chat


async def make_invite_link(bot, chat):
    if chat.username:
        return f"https://t.me/{chat.username}"
    if chat.invite_link:
        return chat.invite_link
    try:
        return (await bot.create_chat_invite_link(chat.id)).invite_link
    except TelegramError:
        try:
            return await bot.export_chat_invite_link(chat.id)
        except TelegramError:
            return None


# ===================================================================== routers

@admin_only
async def callback_router(update, context):
    q = update.callback_query
    name, args = q.data.split(":")[1], q.data.split(":")[2:]
    fn = ACTIONS.get(name)
    if not fn:
        await q.answer("UNKNOWN ACTION")
        return
    if name not in SELF_ANSWER:
        await q.answer()
    if name not in ("batchdone", "noop"):
        context.user_data.pop("state", None)
    try:
        await fn(update, context, args)
    except TelegramError as e:
        log.exception("Admin action %s failed", name)
        await show(update, head("⚠️", "Telegram Error", f"<code>{esc(e)}</code>"), K.back())


async def message_router(update, context):
    """Every non-command private message lands here."""
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    if not is_admin(user.id):
        from utils import support as SUP
        if SUP.available():            # Support Bot connected -> send the user there (one topic per user, no mixing)
            await SUP.hint(update)
            return
        from handlers import contact   # no Support Bot yet -> old behaviour: forwarded to the owner
        await contact.relay(update, context)
        return

    st = context.user_data.get("state")

    # owner used Telegram's Reply on a forwarded user message -> deliver to that user
    if is_owner(user.id) and msg.reply_to_message and st != "batch":
        from handlers import contact
        if await contact.try_native_reply(update, context):
            return

    if st == "batch":
        await upload.handle_upload(update, context)
    elif st in STATES:
        await STATES[st](update, context)
    elif upload.media_info(msg)[0]:
        await upload.handle_upload(update, context)
    else:
        await send_panel(update, context)


# ===================================================================== menus

@action("home")
async def a_home(update, context, args):
    await show(update, panel_text(), K.main_panel(is_owner(update.effective_user.id)))


@action("settings")
async def a_settings(update, context, args):
    await show(update, head("⚙️", "Settings", "SELECT AN OPTION TO CONFIGURE THE BOT."), K.settings_panel(is_owner(update.effective_user.id)))


@action("users")
async def a_users(update, context, args):
    await show(update, head("👥", "Users", "BANS AND ADMINS."), K.users_panel(is_owner(update.effective_user.id)))


@action("monet")
async def a_monet(update, context, args):
    await show(update, head("💰", "Monetization", "ADS AND EARNINGS."),
               K.monetization_panel(is_owner(update.effective_user.id)))


@action("servers")
async def a_servers(update, context, args):
    if not is_owner(update.effective_user.id):
        return
    await show(update, head("🖥️", "Servers", "VPS USAGE AND TRANSFER."), K.servers_panel())


@action("close")
async def a_close(update, context, args):
    try:
        await update.callback_query.message.delete()
    except TelegramError:
        pass


@action("cancel")
async def a_cancel(update, context, args):
    for k in TEMP_KEYS:
        context.user_data.pop(k, None)
    await a_home(update, context, args)


@action("noop")
async def a_noop(update, context, args):
    pass


# ===================================================================== database channel

@action("dbch")
async def a_dbch(update, context, args):
    cur = S.get("db_channel")
    now = f"<b>{esc(cur['title'] or '')}</b>" if cur else "NOT SET"
    await ask_chat(update, context, "dbch",
                   head("📡", "Database Channel", f"CURRENT ▸ {now}\n\nCHOOSE WHERE FILES ARE STORED."))


@state("dbch")
async def st_dbch(update, context):
    chat = await resolve_channel(update, context, need_post=True)
    if not chat:
        return
    await S.set("db_channel", {"id": chat.id, "title": chat.title or str(chat.id)})
    await done(update, context, f"✅ DATABASE CHANNEL SET ▸ <b>{esc(chat.title or '')}</b>.", "settings")


# ===================================================================== admins (owner only)

async def show_admins(update, msg=""):
    admins = S.get("admins")
    rows = [[ub(f"👤 {uid}", "adm:noop"), ub("❌ Remove", f"adm:rmadmin:{uid}")] for uid in admins]
    rows.append([ub("➕ Add Admin", "adm:addadmin")])
    await show(update,
               note(msg) + head("👮", "Admins", f"OWNER ▸ <code>{config.OWNER_ID}</code>\n"
                                               f"ADMINS ▸ <b>{len(admins) or 'NONE'}</b>"),
               kb(rows, ub("🔙 Back", "adm:users")))


@action("admins")
@owner_only
async def a_admins(update, context, args):
    await show_admins(update)


@action("addadmin")
@owner_only
async def a_addadmin(update, context, args):
    await ask(update, context, "addadmin",
              head("👮", "Add Admin", "FORWARD A MESSAGE FROM THE USER, OR SEND THEIR USER ID."))


@state("addadmin")
async def st_addadmin(update, context):
    if not is_owner(update.effective_user.id):
        return
    uid = extract_user_id(update.effective_message)
    if uid is None:
        await warn(update, "COULD NOT READ THE USER ID. SEND IT AS DIGITS.")
        return
    admins = list(S.get("admins"))
    if uid not in admins and not is_owner(uid):
        admins.append(uid)
        await S.set("admins", admins)
    await done(update, context, f"✅ <code>{uid}</code> IS NOW AN ADMIN.", "admins")


@action("rmadmin")
@owner_only
async def a_rmadmin(update, context, args):
    uid = int(args[0])
    await S.set("admins", [a for a in S.get("admins") if a != uid])
    await show_admins(update, f"✅ REMOVED <code>{uid}</code>.")


# ===================================================================== force-subscribe

async def show_fsub(update, msg=""):
    chans, on = S.get("fsub_channels"), S.get("fsub_enabled")
    rows = [[nm(f"📢 {c['title'][:30]}", f"adm:fsubch:{c['id']}")] for c in chans]
    rows += grid([("➕ Add Channel", "adm:fsubadd"),
                  ("🔕 Turn OFF" if on else "🔔 Turn ON", "adm:fsubtoggle")])
    await show(update,
               note(msg) + head("🔔", "Force-Sub", f"STATUS ▸ <b>{onoff(on)}</b> · CHANNELS ▸ <b>{len(chans)}</b>\n\n"
                                                  + ("TAP A CHANNEL TO MOVE OR REMOVE IT." if chans else "NO CHANNEL ADDED YET.")),
               kb(rows, ub("🔙 Back", "adm:settings")))


async def show_fsub_channel(update, cid: int, msg=""):
    chans = S.get("fsub_channels")
    idx = next((i for i, c in enumerate(chans) if c["id"] == cid), None)
    if idx is None:
        await show_fsub(update)
        return
    await show(update,
               note(msg) + f"📢 <b>{esc(chans[idx]['title'])}</b>\n\nPOSITION ▸ <b>{idx + 1}/{len(chans)}</b>",
               kb(grid([("⬆️ Up", f"adm:fsubup:{cid}"), ("⬇️ Down", f"adm:fsubdown:{cid}"),
                        ("🗑 Remove", f"adm:fsubrm:{cid}")]), ub("🔙 Back", "adm:fsub")))


@action("fsub")
async def a_fsub(update, context, args):
    await show_fsub(update)


@action("fsubch")
async def a_fsubch(update, context, args):
    await show_fsub_channel(update, int(args[0]))


@action("fsubtoggle")
async def a_fsubtoggle(update, context, args):
    await S.set("fsub_enabled", not S.get("fsub_enabled"))
    await show_fsub(update, "✅ UPDATED.")


@action("fsubadd")
async def a_fsubadd(update, context, args):
    await ask_chat(update, context, "fsubadd",
                   head("➕", "Add Channel", "CHOOSE THE CHANNEL USERS MUST JOIN."))


@state("fsubadd")
async def st_fsubadd(update, context):
    chat = await resolve_channel(update, context, need_post=False)
    if not chat:
        return
    chans = list(S.get("fsub_channels"))
    if any(c["id"] == chat.id for c in chans):
        await done(update, context, "ℹ️ ALREADY IN THE LIST.", "fsub")
        return
    link = await make_invite_link(context.bot, chat)
    if not link:
        await warn(update, "COULD NOT CREATE AN INVITE LINK. GIVE THE BOT THE “INVITE USERS” PERMISSION.")
        return
    chans.append({"id": chat.id, "title": chat.title or str(chat.id), "link": link})
    await S.set("fsub_channels", chans)
    await done(update, context, f"✅ ADDED <b>{esc(chat.title or '')}</b>.", "fsub")


async def _move(chat_id: int, delta: int):
    chans = list(S.get("fsub_channels"))
    idx = next((i for i, c in enumerate(chans) if c["id"] == chat_id), None)
    new = None if idx is None else idx + delta
    if new is not None and 0 <= new < len(chans):
        chans[idx], chans[new] = chans[new], chans[idx]
        await S.set("fsub_channels", chans)


@action("fsubup")
async def a_fsubup(update, context, args):
    await _move(int(args[0]), -1)
    await show_fsub_channel(update, int(args[0]))


@action("fsubdown")
async def a_fsubdown(update, context, args):
    await _move(int(args[0]), 1)
    await show_fsub_channel(update, int(args[0]))


@action("fsubrm")
async def a_fsubrm(update, context, args):
    cid = int(args[0])
    await S.set("fsub_channels", [c for c in S.get("fsub_channels") if c["id"] != cid])
    await show_fsub(update, "🗑 CHANNEL REMOVED.")


# ===================================================================== website channel

def _hch_text(msg: str = "") -> str:
    c = S.get("home_channel")
    cur = f"<b>{esc(c['title'])}</b>\n{esc(c['link'])}" if c else "NOT SET (THE ENV / FORCE-SUB CHANNEL IS USED, IF ANY)"
    return note(msg) + head("🌐", "Site Channel",
                            "THE CHANNEL SHOWN ON TOP OF THE WEBSITE MAIN PAGE.\n"
                            "THE BOT MUST BE AN ADMIN IN THAT CHANNEL.\n\n"
                            f"<b>CURRENT</b>\n{cur}")


@action("hch")
async def a_hch(update, context, args):
    rows = [[ub("✏️ Set / Change", "adm:hchset")]]
    if S.get("home_channel"):
        rows.append([ub("🗑 Remove", "adm:hchrm")])
    await show(update, _hch_text(), kb(rows, ub("🔙 Back", "adm:settings")))


@action("hchset")
async def a_hchset(update, context, args):
    await ask_chat(update, context, "hch",
                   head("🌐", "Site Channel", "CHOOSE THE CHANNEL FOR THE WEBSITE MAIN PAGE."))


@state("hch")
async def st_hch(update, context):
    chat = await resolve_channel(update, context, need_post=False)
    if not chat:
        return
    link = await make_invite_link(context.bot, chat)
    if not link:
        await warn(update, "COULD NOT GET A LINK. MAKE THE CHANNEL PUBLIC OR GIVE THE BOT THE “INVITE USERS” PERMISSION.")
        return
    await S.set("home_channel", {"id": chat.id, "title": chat.title or str(chat.id), "link": link})
    await done(update, context, f"✅ WEBSITE CHANNEL SET ▸ <b>{esc(chat.title or '')}</b>.", "hch")


@action("hchrm")
async def a_hchrm(update, context, args):
    await S.set("home_channel", None)
    await show(update, _hch_text("🗑 REMOVED."), kb([[ub("✏️ Set / Change", "adm:hchset")]], ub("🔙 Back", "adm:settings")))


# ===================================================================== start image / caption

@action("simg")
async def a_simg(update, context, args):
    context.user_data["state"] = "simg"
    await show(update,
               head("🖼", "Start Image", f"STATUS ▸ <b>{'SET' if S.get('start_image') else 'NOT SET'}</b>\n\n"
                                         "SEND A PHOTO TO REPLACE IT."),
               kb([[ub("🗑 Remove", "adm:simgrm")]], ub("❌ Cancel", "adm:cancel")))


@action("simgrm")
async def a_simgrm(update, context, args):
    await S.set("start_image", None)
    await show(update, "🗑 START IMAGE REMOVED.", K.back("settings"))


@state("simg")
async def st_simg(update, context):
    msg = update.effective_message
    if not msg.photo:
        await warn(update, "SEND A PHOTO, NOT A FILE.")
        return
    await S.set("start_image", msg.photo[-1].file_id)
    context.user_data.pop("state", None)
    await msg.reply_photo(msg.photo[-1].file_id, caption="✅ START IMAGE SAVED.", reply_markup=K.back("settings"))


@action("scap")
async def a_scap(update, context, args):
    context.user_data["state"] = "scap"
    await show(update,
               head("📝", "Start Caption",
                    "SEND A NEW CAPTION (HTML, MAX 1000 CHARACTERS).\n"
                    "TAGS ▸ <code>{first_name}</code> <code>{mention}</code>\n\n"
                    f"<b>CURRENT</b>\n{S.get('start_caption')}"),
               kb([[ub("↩️ Reset", "adm:scaprs")]], ub("❌ Cancel", "adm:cancel")))


@action("scaprs")
async def a_scaprs(update, context, args):
    await S.set("start_caption", db.DEFAULT_CAPTION)
    await show(update, "↩️ START CAPTION RESET.", K.back("settings"))


@state("scap")
async def st_scap(update, context):
    msg = update.effective_message
    text = msg.text_html if msg.text else None
    if not text or len(text) > 1000:
        await warn(update, "SEND TEXT UP TO 1000 CHARACTERS.")
        return
    await S.set("start_caption", text)
    await done(update, context,
               "✅ START CAPTION SAVED.\n\n" + text.replace("{first_name}", "Friend").replace("{mention}", "Friend"),
               "settings")


# ===================================================================== join buttons

def parse_buttons(text: str):
    """Lines of 'Text - URL'; use ' | ' to put several buttons in one row."""
    rows = []
    for line in text.splitlines():
        row = []
        for part in line.split("|"):
            part = part.strip()
            if not part:
                continue
            if " - " not in part:
                raise ValueError(f"MISSING ' - ' IN: {part}")
            label, url = (x.strip() for x in part.rsplit(" - ", 1))
            if not label or not re.match(r"^(https?://|tg://)\S+$", url):
                raise ValueError(f"BAD BUTTON OR URL: {part}")
            row.append([label, url])
        if row:
            rows.append(row)
    if not rows:
        raise ValueError("NO BUTTONS FOUND")
    return rows


@action("btns")
async def a_btns(update, context, args):
    cur = "\n".join(" | ".join(f"{t} - {u}" for t, u in r) for r in S.get("custom_buttons"))
    await show(update,
               head("🔘", "Join Buttons", "CHANNEL BUTTONS COME FROM FORCE-SUB.\n\n"
                                          f"<b>CUSTOM</b>\n{esc(cur) if cur else 'NONE'}"),
               kb(grid([("✏️ Set", "adm:btnset"), ("🗑 Clear", "adm:btnclr")]), ub("🔙 Back", "adm:settings")))


@action("btnset")
async def a_btnset(update, context, args):
    await ask(update, context, "btns",
              head("✏️", "Custom Buttons", "ONE BUTTON PER LINE:\n<code>Text - https://example.com</code>\n"
                                           "SAME ROW:\n<code>Text1 - URL1 | Text2 - URL2</code>"))


@state("btns")
async def st_btns(update, context):
    try:
        rows = parse_buttons(update.effective_message.text or "")
    except ValueError as e:
        await warn(update, f"{esc(e)}\nFORMAT ▸ <code>Text - URL</code>")
        return
    await S.set("custom_buttons", rows)
    await done(update, context, f"✅ SAVED <b>{sum(len(r) for r in rows)}</b> BUTTON(S).", "btns")


@action("btnclr")
async def a_btnclr(update, context, args):
    await S.set("custom_buttons", [])
    await show(update, "🗑 CUSTOM BUTTONS CLEARED.", K.back("btns"))


# ===================================================================== auto-delete / protect

@action("autodel")
async def a_autodel(update, context, args):
    msg = ""
    if args and args[0] in ("0", "5", "10", "30"):
        await S.set("autodelete", int(args[0]))
        msg = "✅ SAVED."
    cur = S.get("autodelete")

    def b(m, label):
        return ub(("✅ " if cur == m else "") + label, f"adm:autodel:{m}")

    await show(update,
               note(msg) + head("⏱", "Auto-Delete", f"CURRENT ▸ <b>{f'{cur} MIN' if cur else 'OFF'}</b>\n\n"
                                                  "FILES VANISH FROM THE USER'S CHAT AFTER THIS TIME."),
               kb([[b(0, "Off"), b(5, "5 min")], [b(10, "10 min"), b(30, "30 min")]], ub("🔙 Back", "adm:settings")))


@action("dlimit")
async def a_dlimit(update, context, args):
    """dlimit:<-1|0|1> - global daily limit (links per user per day), changed one by one (1..1000, 0 = just refresh)."""
    step = int(args[0]) if args else 0
    cur = db.limit_value()
    msg = ""
    if step:
        new = min(1000, max(1, cur + step))
        if new != cur:
            await S.set("daily_limit", new)
            cur, msg = new, "✅ SAVED."
    await show(update,
               note(msg) + head("📊", "Daily Limit", f"CURRENT ▸ <b>{cur}</b> LINKS PER USER PER DAY\n\n"
                                                    "EACH LINK USES ITS CHANNEL'S LIMIT COUNT (DEFAULT 1). ADMINS ARE EXEMPT."),
               kb([[ub("➖", "adm:dlimit:-1"), ub(f"📊 {cur}", "adm:dlimit:0"), ub("➕", "adm:dlimit:1")]],
                  ub("🔙 Back", "adm:settings")))


@action("protect")
async def a_protect(update, context, args):
    msg = ""
    if args and args[0] == "t":
        await S.set("protect", not S.get("protect"))
        msg = "✅ UPDATED."
    on = S.get("protect")
    await show(update,
               note(msg) + head("🛡", "Protect Content", f"STATUS ▸ <b>{'ON 🔒' if on else 'OFF 🔓'}</b>\n\n"
                                                         "ON BLOCKS FORWARDING AND SAVING OF DELIVERED FILES."),
               kb([[ub("🔓 Turn OFF" if on else "🔒 Turn ON", "adm:protect:t")]], ub("🔙 Back", "adm:settings")))


# ===================================================================== file caption

@action("fcap")
async def a_fcap(update, context, args):
    msg = ""
    if args and args[0] == "t":
        await S.set("remove_caption", not S.get("remove_caption"))
        msg = "✅ UPDATED."
    elif args and args[0] == "clr":
        await S.set("custom_caption", "")
        msg = "🗑 CUSTOM CAPTION CLEARED."
    elif args and args[0] == "set":
        await ask(update, context, "fcap",
                  head("✏️", "Custom Caption", "SEND THE CAPTION (HTML, MAX 1000 CHARACTERS).\n"
                                               "IT REPLACES THE ORIGINAL CAPTION ON EVERY DELIVERED FILE."))
        return
    custom, rem = S.get("custom_caption"), S.get("remove_caption")
    await show(update,
               note(msg) + head("🏷", "File Caption",
                                (f"<b>CUSTOM</b>\n{custom}" if custom else "CUSTOM ▸ <b>NONE</b>")
                                + f"\n\nREMOVE ORIGINAL ▸ <b>{onoff(rem)}</b>\n"
                                  "PRIORITY ▸ CUSTOM → REMOVE → ORIGINAL"),
               kb(grid([("✏️ Set", "adm:fcap:set"), ("🗑 Clear", "adm:fcap:clr")])
                  + [[ub("🚫 Remove Original: " + onoff(rem), "adm:fcap:t")]], ub("🔙 Back", "adm:settings")))


@state("fcap")
async def st_fcap(update, context):
    msg = update.effective_message
    text = msg.text_html if msg.text else None
    if not text or len(text) > 1000:
        await warn(update, "SEND TEXT UP TO 1000 CHARACTERS.")
        return
    await S.set("custom_caption", text)
    await done(update, context, "✅ CUSTOM CAPTION SAVED.", "fcap")


# ===================================================================== broadcast

@action("bc")
async def a_bc(update, context, args):
    await ask(update, context, "bc", head("📢", "Broadcast", "SEND OR FORWARD THE MESSAGE FOR ALL USERS."))


@state("bc")
async def st_bc(update, context):
    msg = update.effective_message
    total = await db.count_broadcastable()
    context.user_data["bc_msg"] = (msg.chat_id, msg.message_id)
    context.user_data.pop("state", None)
    await msg.reply_text(head("📢", "Broadcast", f"SEND THIS MESSAGE TO <b>{total}</b> USERS?"),
                         reply_to_message_id=msg.message_id,
                         reply_markup=kb([[ub("✅ Send Now", "adm:bcgo")]], ub("❌ Cancel", "adm:cancel")))


@action("bcgo")
async def a_bcgo(update, context, args):
    data = context.user_data.pop("bc_msg", None)
    if not data:
        await show(update, "⚠️ NOTHING TO BROADCAST.", K.back())
        return
    q = update.callback_query
    await show(update, head("📢", "Broadcast", "STARTING…"))
    context.application.create_task(
        broadcast.run_broadcast(context.bot, data[0], data[1], q.message.chat_id, q.message.message_id))


# ===================================================================== stats / mongo

def _speed(ms) -> str:
    if ms is None:
        return "—"
    return f"{'🟢' if ms < 150 else '🟡' if ms < 500 else '🔴'} {ms:.0f} ms"


def mongo_text(m: dict, detail: bool = False) -> str:
    if not m["ok"]:
        return ("🍃 <b>MONGODB</b> ▸ 🔴 <b>NOT REACHABLE</b>\n"
                f"<code>{esc(m['error'] or 'unknown error')}</code>")
    limit = m["limit_mb"] * 1048576
    pct = min(100.0, m["used"] / limit * 100) if limit else 0
    left = max(0, limit - m["used"])
    bar = "🟩" * round(pct / 10) + "⬜" * (10 - round(pct / 10))
    health = "🔴 ALMOST FULL" if pct >= 90 else "🟡 GETTING FULL" if pct >= 80 else "🟢 HEALTHY"
    t = (f"🍃 <b>MONGODB</b> ▸ {health}\n"
         f"{bar} <b>{pct:.1f}%</b>\n"
         f"💾 USED ▸ <b>{mb(m['used'])}</b> / {m['limit_mb']:g} MB\n"
         f"🆓 LEFT ▸ <b>{mb(left)}</b>")
    if detail:
        t += (f"\n\n⚡ PING ▸ {_speed(m['ping_ms'])} · READ ▸ {_speed(m['read_ms'])}\n"
              f"📄 DOCS ▸ <b>{m['objects']}</b> · 🗂 COLLECTIONS ▸ <b>{m['collections']}</b>")
        if m.get("conn"):
            t += f"\n🔌 CONNECTIONS ▸ <b>{m['conn'].get('current')}</b> USED · {m['conn'].get('available')} FREE"
        t += f"\n\n📦 DATA {mb(m['data'])} · DISK {mb(m['storage'])} · INDEX {mb(m['index'])}\n"
        t += "\n".join(f"• <code>{esc(n)}</code> ▸ {c} · {mb(sz)}" for n, c, sz in m.get("cols", []))
    return t


@action("stats")
async def a_stats(update, context, args):
    s = await db.stats()
    m = await db.mongo_status()
    await show(update,
               head("📊", "Statistics",
                    f"👥 USERS ▸ <b>{s['users']:,}</b>\n"
                    f"📁 FILES ▸ <b>{s['files']:,}</b>\n"
                    f"🔗 LINKS ▸ <b>{s['links']:,}</b>\n"
                    f"🚫 BANNED ▸ <b>{s['banned']:,}</b>\n"
                    f"💤 BLOCKED ▸ <b>{s['blocked']:,}</b>\n\n") + mongo_text(m),
               kb(grid([("🍃 MongoDB", "adm:mongo"), ("🔄 Refresh", "adm:stats")]), ub("🔙 Back", "adm:home")))


@action("mongo")
async def a_mongo(update, context, args):
    await show(update, mongo_text(await db.mongo_status(), detail=True),
               kb(grid([("🔄 Refresh", "adm:mongo"), ("🔙 Back", "adm:stats")])))


# ===================================================================== ban / reset

@action("ban")
async def a_ban(update, context, args):
    s = await db.stats()
    await show(update, head("🚫", "Ban / Unban", f"BANNED ▸ <b>{s['banned']}</b>"),
               kb(grid([("🚫 Ban", "adm:banask"), ("✅ Unban", "adm:unbanask")]), ub("🔙 Back", "adm:users")))


@action("banask")
async def a_banask(update, context, args):
    await ask(update, context, "ban", head("🚫", "Ban User", "FORWARD A MESSAGE FROM THE USER, OR SEND THEIR ID."))


@action("unbanask")
async def a_unbanask(update, context, args):
    await ask(update, context, "unban", head("✅", "Unban User", "FORWARD A MESSAGE FROM THE USER, OR SEND THEIR ID."))


async def _ban_flow(update, context, flag: bool):
    uid = extract_user_id(update.effective_message)
    if uid is None:
        await warn(update, "COULD NOT READ THE USER ID. SEND IT AS DIGITS.")
        return
    if flag and is_admin(uid):
        await warn(update, "ADMINS CANNOT BE BANNED.")
        return
    await db.set_banned(uid, flag)
    await done(update, context, f"{'🚫 BANNED' if flag else '✅ UNBANNED'} ▸ <code>{uid}</code>", "ban")


@state("ban")
async def st_ban(update, context):
    await _ban_flow(update, context, True)


@state("unban")
async def st_unban(update, context):
    await _ban_flow(update, context, False)


@action("reset")
async def a_reset(update, context, args):
    await show(update,
               head("♻️", "Reset Settings", "ADMINS AND DATABASE CHANNEL ARE KEPT.\n\n"
                                            + hl("ALL OTHER SETTINGS RETURN TO DEFAULT. <b>CONTINUE?</b>")),
               K.confirm("adm:resetyes", "adm:settings", "✅ Yes, Reset"))


@action("resetyes")
async def a_resetyes(update, context, args):
    await S.reset()
    await show(update, "✅ SETTINGS RESET.", K.back("settings"))


# ===================================================================== ads (Monetag + Adsgram Mini App)

@action("ads")
async def a_ads(update, context, args):
    msg = ""
    a = args[0] if args else ""

    if a == "t":
        if not S.get("ads_enabled") and not ads.providers():
            msg = "⚠️ SET THE MONETAG ZONE ID OR THE ADSGRAM BLOCK ID FIRST."
        else:
            await S.set("ads_enabled", not S.get("ads_enabled"))
    elif a == "agt":
        if not S.get("ads_ag_enabled") and not ads.ag_block():
            msg = "⚠️ SET THE ADSGRAM BLOCK ID FIRST."
        else:
            await S.set("ads_ag_enabled", not S.get("ads_ag_enabled"))
    elif a == "agpb":
        await S.set("ads_ag_postback", not S.get("ads_ag_postback"))
    elif a == "nf":
        await S.set("ads_nofill_free", not S.get("ads_nofill_free"))
    elif a == "mix":                                         # cycle the Adsgram share: 50 -> 70 -> 30 -> 100 -> 0 -> 50
        steps = [50, 70, 30, 100, 0]
        cur = ads.ag_share()
        await S.set("ads_ag_share", steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else 50)
    elif a == "agb":
        await ask(update, context, "ads_ag_block",
                  head("🧩", "Adsgram Block ID", f"NOW ▸ <code>{esc(ads.ag_block()) or 'NOT SET'}</code>\n\n"
                                                 "SEND YOUR ADSGRAM <b>REWARD BLOCK ID</b> (DIGITS, E.G. <code>12345</code>)."))
        return
    elif a == "agurl":
        await show_ag_url(update)
        return
    elif a == "pb":
        await S.set("ads_postback", not S.get("ads_postback"))
    elif a == "ab":
        await S.set("ads_block_adblock", not S.get("ads_block_adblock"))
    elif a == "tst":
        await S.set("ads_admin_test", not S.get("ads_admin_test"))
    elif a == "newsec":
        await S.set("ads_secret", "")
        await ads.ensure_secret()
        await show_postback(update, "🔑 NEW SECRET CREATED. PASTE THE NEW URLS IN MONETAG AND ADSGRAM (📡 ADSGRAM URL).")
        return
    elif a == "pburl":
        await show_postback(update)
        return
    elif a == "stat":
        await show_ads_stats(update)
        return
    elif a == "zone":
        await ask(update, context, "ads_zone",
                  head("✏️", "Zone ID", f"NOW ▸ <code>{esc(ads.zone()) or 'NOT SET'}</code>\n\n"
                                        "SEND YOUR MAIN <b>SDK ZONE ID</b> (DIGITS ONLY)."))
        return
    elif a == "sdk":
        await ask(update, context, "ads_sdk",
                  head("🔗", "SDK URL", f"NOW ▸ <code>{esc(ads.sdk_url())}</code>\n\n"
                                        "SEND THE <code>src</code> OF THE SCRIPT TAG FROM YOUR MONETAG CODE."))
        return

    on = S.get("ads_enabled")
    warns = []
    if not ads.mini_app_ready():
        warns.append("BASE_URL MUST BE A PUBLIC HTTPS URL.")
    if not ads.zone():
        warns.append("MONETAG ZONE ID IS NOT SET.")
    if S.get("ads_ag_enabled") and not ads.ag_block():
        warns.append("ADSGRAM BLOCK ID IS NOT SET.")
    if on and S.get("ads_ag_enabled") and S.get("ads_ag_postback") and not S.get("ads_ag_last"):
        warns.append("NO ADSGRAM REWARD YET. PASTE THE REWARD URL IN ADSGRAM.")
    if on and S.get("ads_ag_enabled") and not S.get("ads_ag_postback"):
        warns.append("ADSGRAM SERVER VERIFY IS OFF. TURN IT ON AS SOON AS ADSGRAM ENABLES THE REWARD URL.")
    if S.get("ads_nofill_free"):
        warns.append("FREE FILES ON NO-FILL ARE ON (USERS CAN SKIP ADS WHEN NO AD LOADS). TURN OFF FOR STRICT MODE.")
    if on and S.get("ads_postback") and not S.get("ads_last_postback"):
        warns.append("NO POSTBACK YET. PASTE THE URL IN MONETAG.")
    if not S.get("ads_postback"):
        warns.append("SERVER VERIFY IS OFF. KEEP IT ON.")

    def st(key):
        return onoff(S.get(key))

    await show(update,
               note(msg) + head("📺", "Ads", "SELECT AN OPTION TO MANAGE ADS.")
               + "".join(f"\n⚠️ {w}" for w in warns),
               kb([[ub("🔕 Turn OFF" if on else "🔔 Turn ON", "adm:ads:t")]]
                  + grid([(f"🅰️ Adsgram: {st('ads_ag_enabled')}", "adm:ads:agt"), ("🧩 Adsgram Block", "adm:ads:agb"),
                          (f"🔁 Mix: M{100 - ads.ag_share()}/A{ads.ag_share()}", "adm:ads:mix"),
                          (f"🛰 AG Verify: {st('ads_ag_postback')}", "adm:ads:agpb"),
                          (f"🎁 No-fill free: {st('ads_nofill_free')}", "adm:ads:nf"), ("📡 Adsgram URL", "adm:ads:agurl")])
                  + grid([("✏️ Zone ID", "adm:ads:zone"), ("🔗 SDK URL", "adm:ads:sdk"),
                          (f"🛰 Verify: {st('ads_postback')}", "adm:ads:pb"),
                          (f"🛡 AdBlock: {st('ads_block_adblock')}", "adm:ads:ab"),
                          (f"🧪 Test: {st('ads_admin_test')}", "adm:ads:tst"), ("🔑 New Secret", "adm:ads:newsec"),
                          ("📡 Postback URL", "adm:ads:pburl"), ("📈 Ad Stats", "adm:ads:stat")]),
                  ub("🔙 Back", "adm:monet")))


async def show_postback(update, msg: str = ""):
    await show(update,
               note(msg) + head("📡", "Postback URL",
                                "MONETAG → SDK ZONE → POSTBACK URL:\n"
                                f"<code>{esc(await ads.postback_url())}</code>\n\n"
                                "MONETAG → TELEGRAM MINI APP URL:\n"
                                f"<code>{esc(config.BASE_URL or 'BASE_URL NOT SET')}</code>"),
               kb(grid([("🔑 New Secret", "adm:ads:newsec"), ("🔙 Back", "adm:ads")])))


async def show_ag_url(update, msg: str = ""):
    await show(update,
               note(msg) + head("📡", "Adsgram Setup",
                                "1) partner.adsgram.ai → CREATE PLATFORM (TELEGRAM MINI-APP):\n"
                                f"WEB APP URL ▸ <code>{esc(config.BASE_URL or 'BASE_URL NOT SET')}</code>\n"
                                "BOT ID ▸ THE ID OF THIS BOT (BOTFATHER → /mybots → API TOKEN, NUMBER BEFORE THE :)\n\n"
                                "2) CREATE A <b>REWARD</b> BLOCK AND PASTE THIS AS <b>REWARD URL</b>:\n"
                                f"<code>{esc(await ads.ag_reward_url())}</code>\n\n"
                                "3) SEND THE BLOCK ID WITH 🧩 ADSGRAM BLOCK, THEN TURN 🅰️ ADSGRAM ON.\n\n"
                                "REWARD URL IS OPTIONAL FOR SMALL APPS: ASK @adsgram_supportbot TO ENABLE IT, THEN TURN 🛰 AG VERIFY ON."),
               kb(grid([("🔑 New Secret", "adm:ads:newsec"), ("🔙 Back", "adm:ads")])))


async def show_ads_stats(update):
    st = await ads.get_stats()
    await show(update,
               head("📈", "Ad Stats",
                    f"✅ VERIFIED ▸ <b>{st.get('verified', 0)}</b>\n"
                    f"🎁 FREE FILES ▸ <b>{st.get('fallback', 0)}</b>\n"
                    f"🚫 BLOCKED ▸ <b>{st.get('blocked', 0)}</b>\n"
                    f"🅼 MONETAG ADS ▸ <b>{st.get('mt_ok', 0)}</b> · 🅰️ ADSGRAM ADS ▸ <b>{st.get('ag_ok', 0)}</b>\n\n"
                    f"🛰 LAST POSTBACK ▸ <b>{ago(S.get('ads_last_postback'))}</b>\n"
                    f"🛰 LAST ADSGRAM REWARD ▸ <b>{ago(S.get('ads_ag_last'))}</b>\n"
                    f"🔁 NO-FILL TRIES ▸ <b>{S.get('ads_tries')}</b> (MAX {ads.FALLBACK_DAILY} / USER / DAY)"),
               kb(grid([("🔄 Refresh", "adm:ads:stat"), ("🔙 Back", "adm:ads")])))


@state("ads_zone")
async def st_ads_zone(update, context):
    z = (update.effective_message.text or "").strip()
    if not re.fullmatch(r"\d{3,14}", z):
        await warn(update, "SEND THE ZONE ID AS DIGITS ONLY.")
        return
    await S.set("ads_zone", z)
    await done(update, context, f"✅ ZONE ID SAVED ▸ <code>{z}</code>", "ads")


@state("ads_ag_block")
async def st_ads_ag_block(update, context):
    b = (update.effective_message.text or "").strip()
    if not re.fullmatch(r"(int-)?\d{1,12}", b):
        await warn(update, "SEND THE BLOCK ID LIKE <code>12345</code> (REWARD BLOCK).")
        return
    await S.set("ads_ag_block", b)
    await done(update, context, f"✅ ADSGRAM BLOCK ID SAVED ▸ <code>{b}</code>", "ads")


@state("ads_sdk")
async def st_ads_sdk(update, context):
    u = (update.effective_message.text or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    if not re.fullmatch(r"https://[^\s'\"<>]+", u):
        await warn(update, "SEND A VALID HTTPS URL.")
        return
    await S.set("ads_sdk_url", u)
    await done(update, context, "✅ SDK URL SAVED.", "ads")


# ===================================================================== batch + links

async def _need_db_channel(update):
    await show(update, "⚠️ SET THE DATABASE CHANNEL FIRST.", kb([[ub("📡 DB Channel", "adm:dbch")]], ub("🔙 Back", "adm:batch")))


@action("batch")
async def a_batch(update, context, args):
    await show(update, head("📦", "Create Batch", "ONE LINK FOR MANY FILES."),
               kb(grid([("📥 Send Files", "adm:batchstart"), ("🔢 Post Range", "adm:range")]), ub("🔙 Back", "adm:home")))


@action("batchstart")
async def a_batchstart(update, context, args):
    if not S.get("db_channel"):
        await _need_db_channel(update)
        return
    q = update.callback_query
    context.user_data.update(
        state="batch",
        batch_ids=[],
        batch_token=db.new_token(),   # same token is written in every file's backup caption code
        batch_status=(q.message.chat_id, q.message.message_id),
    )
    await show(update, head("📦", "Batch Mode", "SEND YOUR FILES, THEN TAP <b>DONE</b>."), K.batch_kb())


@action("batchdone")
async def a_batchdone(update, context, args):
    q = update.callback_query
    # wait until every file that was sent before "Done" has finished uploading
    async with upload.batch_lock(update.effective_user.id):
        ids = sorted(set(context.user_data.get("batch_ids", [])))
        if not ids:
            await q.answer("SEND AT LEAST ONE FILE FIRST.", show_alert=True)
            return
        await q.answer()
        token = await db.create_link(ids, update.effective_user.id, f"Batch ({len(ids)} files)",
                                     token=context.user_data.get("batch_token"))
        for k in TEMP_KEYS:
            context.user_data.pop(k, None)
    await show(update, f"✅ BATCH CLOSED ▸ <b>{len(ids)}</b> FILES.", K.back())
    await upload.announce_link(context, q.message.chat_id, token, len(ids), f"Batch ({len(ids)} files)")


@action("range")
async def a_range(update, context, args):
    if not S.get("db_channel"):
        await _need_db_channel(update)
        return
    await ask(update, context, "range_first",
              head("🔢", "Post Range", "STEP 1/2 ▸ FORWARD THE <b>FIRST</b> POST FROM THE DATABASE CHANNEL, OR SEND ITS LINK."))


@state("range_first")
async def st_range_first(update, context):
    pid = extract_post_id(update.effective_message, S.get("db_channel")["id"])
    if pid is None:
        await warn(update, "NOT A DATABASE CHANNEL POST. FORWARD IT OR SEND ITS LINK.")
        return
    context.user_data["range_first"] = pid
    context.user_data["state"] = "range_last"
    await update.effective_message.reply_text("STEP 2/2 ▸ FORWARD THE <b>LAST</b> POST OF THE RANGE.",
                                              reply_markup=K.cancel())


@state("range_last")
async def st_range_last(update, context):
    msg = update.effective_message
    last = extract_post_id(msg, S.get("db_channel")["id"])
    first = context.user_data.get("range_first")
    if last is None or first is None or last < first or last - first + 1 > MAX_RANGE:
        await warn(update, f"SEND A LAST POST AFTER THE FIRST ONE (MAX {MAX_RANGE} POSTS).")
        return
    ids = list(range(first, last + 1))
    label = f"Range {first}-{last}"
    token = await db.create_link(ids, update.effective_user.id, label)
    for k in TEMP_KEYS:
        context.user_data.pop(k, None)
    await upload.announce_link(context, msg.chat_id, token, len(ids), label)


@action("links")
async def a_links(update, context, args):
    page = int(args[0]) if args else 0
    uid = update.effective_user.id
    creator = None if is_owner(uid) else uid
    total = await db.count_links(creator)
    rows = [[nm(f"🔗 {r['label'][:28]} · {r['views']}👁", f"adm:lk:{r['_id']}")]
            for r in await db.list_links(page * PAGE, PAGE, creator)]
    rows.append(pager(page, -(-total // PAGE), "adm:links"))
    await show(update,
               head("🔗", "My Links", f"TOTAL ▸ <b>{total}</b>\n\nTAP A LINK TO VIEW OR DELETE IT." if total
                    else "NO LINKS YET. SEND A FILE TO CREATE ONE."),
               kb(rows, ub("🔙 Back", "adm:home")))


@action("lk")
async def a_lk(update, context, args):
    link = await db.get_link(args[0])
    if not link:
        await show(update, "⚠️ LINK NOT FOUND. IT MAY BE DELETED.", K.back("links"))
        return
    await show(update,
               f"🔗 <b>{esc(link['label'])}</b>\n\n"
               f"📁 FILES ▸ <b>{len(link['ids'])}</b> · 👁 VIEWS ▸ <b>{link['views']}</b>\n\n"
               f"<code>{share_link(context.bot.username, link['_id'])}</code>",
               kb([[ub("🗑 Delete Link", f"adm:lkdel:{link['_id']}")]], ub("🔙 Back", "adm:links")))


@action("lkdel")
async def a_lkdel(update, context, args):
    ids = await db.purge_link(args[0])               # link + files + posts + progress, all gone from MongoDB
    await drop_channel_msgs(context.bot, ids)        # and its copies in the Database Channel
    await show(update, "🗑 <b>LINK DELETED</b>\n\n" + hl("REMOVED FROM THE DATABASE FOR GOOD. IT CANNOT BE RESTORED."),
               K.back("links"))
