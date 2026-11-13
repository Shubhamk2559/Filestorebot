
"""
handlers/admin.py
The whole admin panel. Nothing here is typed as a command:

  callback_router  every inline-button press whose data starts with "adm:"
                   (format "adm:<action>:<arg>:<arg>"), dispatched through ACTIONS.
  message_router   every non-command private message. For an admin it is either
                   (a) the answer to a pending prompt -> STATES (conversation state machine), or
                   (b) a file upload -> handlers.upload.

State machine: context.user_data["state"] = name of the prompt being answered.
Every prompt has a "❌ Cancel" button; every result screen has a "🔙 Back" button.
"""
import html
import logging
import re
import time

import config

from telegram import (
    InlineKeyboardButton as _InlineKeyboardButton,
    InlineKeyboardMarkup as M,
    LinkPreviewOptions,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from database import db
from database.db import settings as S
from handlers import broadcast, upload
from utils import keyboards as K
from utils.helpers import (
    admin_only,
    owner_only,
    is_admin,
    is_owner,
    extract_chat_ref,
    extract_user_id,
    extract_post_id,
)
from utils import ads
from utils.streamer import streamer

log = logging.getLogger(__name__)


# FIX: Telegram's second positional argument is URL, not callback_data.
# All admin navigation buttons use callback_data.
def B(text, callback_data):
    return _InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
    )


NOPREVIEW = LinkPreviewOptions(is_disabled=True)
PAGE = 8
MAX_RANGE = 10000
TEMP_KEYS = ("state", "batch_ids", "batch_status", "batch_token", "restore_status", "restore_n", "range_first", "bc_msg", "short_tmp")

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

async def show(update, text, kb=None):
    """Edit the panel message in place or send a new one."""
    q = update.callback_query
    try:
        if q:
            await q.edit_message_text(
                text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                link_preview_options=NOPREVIEW,
            )
        else:
            await update.effective_message.reply_text(
                text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                link_preview_options=NOPREVIEW,
            )
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await update.effective_chat.send_message(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            link_preview_options=NOPREVIEW,
        )


async def ask(update, context, state_name, text):
    """Open a conversational prompt."""
    context.user_data["state"] = state_name
    await show(update, text, K.cancel())


def panel_text() -> str:
    ch, fs, ad = S.get("db_channel"), S.get("fsub_channels"), S.get("autodelete")
    db_txt = f"<code>{ch['id']}</code>" if ch else "❌ not set"

    return (
        "🛠 <b>Admin Panel</b>\n\n"
        f"📡 Database channel: {db_txt}\n"
        f"🔔 Force-sub: {'ON' if S.get('fsub_enabled') else 'OFF'} "
        f"({len(fs)} channel{'s' if len(fs) != 1 else ''})\n"
        f"⏱ Auto-delete: {f'{ad} min' if ad else 'off'}\n"
        f"🎬 Delivery: {'Stream 🌐' if S.get('delivery_mode') == 'stream' else 'Forward 📩'}\n"
        f"🛡 Protect content: {'ON' if S.get('protect') else 'OFF'}\n\n"
        "📤 <i>Send me any file to get a share link.</i>"
    )


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
        return False, "I'm not an admin in that chat. Add me as admin and try again."

    if need_post and getattr(member, "can_post_messages", None) is False:
        return False, "I need the “Post Messages” permission in that channel."

    return True, ""


async def resolve_channel(update, context, need_post: bool):
    """Resolve a channel from a forwarded message, ID, or username."""
    msg = update.effective_message
    ref = extract_chat_ref(msg)

    if ref is None:
        await msg.reply_text(
            "⚠️ Channel not found.\n"
            "Forward a message from the channel, or send its ID or @username.",
            reply_markup=K.cancel(),
        )
        return None

    try:
        chat = await context.bot.get_chat(ref)
        ok, why = await bot_admin_status(context.bot, chat.id, need_post)

    except TelegramError as e:
        await msg.reply_text(
            f"⚠️ Unable to access this chat (<code>{html.escape(str(e))}</code>).\n"
            "Add the bot as an admin and try again.",
            reply_markup=K.cancel(),
        )
        return None

    if not ok:
        await msg.reply_text(why, reply_markup=K.cancel())
        return None

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
    parts = q.data.split(":")[1:]

    name, args = parts[0], parts[1:]
    fn = ACTIONS.get(name)

    if not fn:
        await q.answer("Unknown action")
        return

    if name not in SELF_ANSWER:
        await q.answer()

    if name not in ("batchdone", "noop"):
        context.user_data.pop("state", None)

    try:
        await fn(update, context, args)
    except TelegramError as e:
        log.exception("Admin action %s failed", name)
        await show(
            update,
            f"⚠️ Telegram error: <code>{html.escape(str(e))}</code>",
            K.back(),
        )


async def message_router(update, context):
    """Every non-command private message lands here."""
    user, msg = update.effective_user, update.effective_message

    if not user or not msg or not is_admin(user.id):
        return

    st = context.user_data.get("state")

    if st == "batch":
        await upload.handle_upload(update, context)

    elif st in STATES:
        await STATES[st](update, context)

    elif upload.media_info(msg)[0]:
        await upload.handle_upload(update, context)

    else:
        await send_panel(update, context)


# ===================================================================== core

@action("home")
async def a_home(update, context, args):
    await show(
        update,
        panel_text(),
        K.main_panel(is_owner(update.effective_user.id)),
    )


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

    cur_txt = (
        f"<b>{html.escape(cur['title'] or '')}</b> "
        f"(<code>{cur['id']}</code>)"
        if cur else "❌ not set"
    )

    await ask(
        update,
        context,
        "dbch",
        f"📡 <b>Database Channel</b>\nCurrent: {cur_txt}\n\n"
        "Forward a message from the channel, or send its ID or @username.\n"
        "The bot must be an <b>admin</b> with permission to post.",
    )


@state("dbch")
async def st_dbch(update, context):
    chat = await resolve_channel(update, context, need_post=True)

    if not chat:
        return

    await S.set(
        "db_channel",
        {"id": chat.id, "title": chat.title or str(chat.id)},
    )

    context.user_data.pop("state", None)

    await update.effective_message.reply_text(
        f"✅ Database channel set to <b>{html.escape(chat.title or '')}</b> "
        f"(<code>{chat.id}</code>).\n"
        "Uploaded files will be stored in this channel.",
        reply_markup=K.back(),
    )


# ===================================================================== admins (owner only)

async def show_admins(update, note=""):
    admins = S.get("admins")

    rows = [
        [
            B(f"👤 {uid}", "adm:noop"),
            B("❌ Remove", f"adm:rmadmin:{uid}"),
        ]
        for uid in admins
    ]

    rows.append([B("➕ Add Admin", "adm:addadmin")])
    rows.append([B("🔙 Back", "adm:home")])

    body = "\n".join(f"• <code>{u}</code>" for u in admins) or "No extra admins yet."

    await show(
        update,
        f"{note}👮 <b>Admins</b>\n\nOwner: <code>{config.OWNER_ID}</code>\n{body}",
        M(rows),
    )


@action("admins")
@owner_only
async def a_admins(update, context, args):
    await show_admins(update)


@action("addadmin")
@owner_only
async def a_addadmin(update, context, args):
    await ask(
        update,
        context,
        "addadmin",
        "👮 <b>Add Admin</b>\n\n"
        "Forward a message from the user, or send their user ID.",
    )


@state("addadmin")
async def st_addadmin(update, context):
    msg = update.effective_message

    if not is_owner(update.effective_user.id):
        return

    uid = extract_user_id(msg)

    if uid is None:
        await msg.reply_text(
            "⚠️ Unable to read the user ID (the user's forwarding is hidden).\n"
            "Please send the numeric ID instead.",
            reply_markup=K.cancel(),
        )
        return

    admins = list(S.get("admins"))

    if uid not in admins and not is_owner(uid):
        admins.append(uid)
        await S.set("admins", admins)

    context.user_data.pop("state", None)

    await msg.reply_text(
        f"✅ <code>{uid}</code> is now an admin.",
        reply_markup=K.back("admins"),
    )


@action("rmadmin")
@owner_only
async def a_rmadmin(update, context, args):
    uid = int(args[0])

    await S.set(
        "admins",
        [a for a in S.get("admins") if a != uid],
    )

    await show_admins(update, f"✅ Removed <code>{uid}</code>.\n\n")


# ===================================================================== force-subscribe

async def show_fsub(update, note=""):
    chans, on = S.get("fsub_channels"), S.get("fsub_enabled")
    rows = []

    for c in chans:
        rows.append([
            B(c["title"][:22], "adm:noop"),
            B("⬆️", f"adm:fsubup:{c['id']}"),
            B("⬇️", f"adm:fsubdown:{c['id']}"),
            B("❌", f"adm:fsubrm:{c['id']}"),
        ])

    rows.append([B("➕ Add Channel", "adm:fsubadd")])

    rows.append([
        B(
            "🔕 Disable Force-Sub" if on else "🔔 Enable Force-Sub",
            "adm:fsubtoggle",
        )
    ])

    rows.append([B("🔙 Back", "adm:home")])

    await show(
        update,
        f"{note}🔔 <b>Force-Subscribe</b>: {'ON ✅' if on else 'OFF ⛔'}\n\n"
        "Users must join all channels listed below.\n"
        "Use ⬆️ ⬇️ to reorder and ❌ to remove.",
        M(rows),
    )


@action("fsub")
async def a_fsub(update, context, args):
    await show_fsub(update)


@action("fsubtoggle")
async def a_fsubtoggle(update, context, args):
    await S.set("fsub_enabled", not S.get("fsub_enabled"))
    await show_fsub(update, "✅ Updated.\n\n")


@action("fsubadd")
async def a_fsubadd(update, context, args):
    await ask(
        update,
        context,
        "fsubadd",
        "➕ <b>Add Force-Sub Channel</b>\n\n"
        "Forward a message from the channel, or send its ID or @username.\n"
        "The bot must be an <b>admin</b> there.",
    )


@state("fsubadd")
async def st_fsubadd(update, context):
    chat = await resolve_channel(update, context, need_post=False)

    if not chat:
        return

    msg = update.effective_message
    chans = list(S.get("fsub_channels"))

    if any(c["id"] == chat.id for c in chans):
        context.user_data.pop("state", None)
        await msg.reply_text(
            "ℹ️ That channel is already in the list.",
            reply_markup=K.back("fsub"),
        )
        return

    link = await make_invite_link(context.bot, chat)

    if not link:
        await msg.reply_text(
            "⚠️ Unable to create an invite link.\n"
            "Grant the bot the “Invite Users” permission and try again.",
            reply_markup=K.cancel(),
        )
        return

    chans.append({
        "id": chat.id,
        "title": chat.title or str(chat.id),
        "link": link,
    })

    await S.set("fsub_channels", chans)
    context.user_data.pop("state", None)

    await msg.reply_text(
        f"✅ Added <b>{html.escape(chat.title or '')}</b> to force-sub.",
        reply_markup=K.back("fsub"),
    )


async def _move(chat_id: int, delta: int):
    chans = list(S.get("fsub_channels"))

    idx = next(
        (i for i, c in enumerate(chans) if c["id"] == chat_id),
        None,
    )

    new = None if idx is None else idx + delta

    if new is not None and 0 <= new < len(chans):
        chans[idx], chans[new] = chans[new], chans[idx]
        await S.set("fsub_channels", chans)


@action("fsubup")
async def a_fsubup(update, context, args):
    await _move(int(args[0]), -1)
    await show_fsub(update)


@action("fsubdown")
async def a_fsubdown(update, context, args):
    await _move(int(args[0]), 1)
    await show_fsub(update)


@action("fsubrm")
async def a_fsubrm(update, context, args):
    cid = int(args[0])

    await S.set(
        "fsub_channels",
        [c for c in S.get("fsub_channels") if c["id"] != cid],
    )

    await show_fsub(update, "✅ Channel removed.\n\n")


# ===================================================================== start image / caption

@action("simg")
async def a_simg(update, context, args):
    has = "✅ set" if S.get("start_image") else "❌ not set"

    context.user_data["state"] = "simg"

    await show(
        update,
        f"🖼 <b>Start Image</b> ({has})\n\n"
        "Send a <b>photo</b> to use as the start image.",
        M([
            [B("🗑 Remove image", "adm:simgrm")],
            [B("❌ Cancel", "adm:cancel")],
        ]),
    )


@action("simgrm")
async def a_simgrm(update, context, args):
    await S.set("start_image", None)
    await show(update, "✅ Start image removed.", K.back())


@state("simg")
async def st_simg(update, context):
    msg = update.effective_message

    if not msg.photo:
        await msg.reply_text(
            "⚠️ Please send a photo, not a file.",
            reply_markup=K.cancel(),
        )
        return

    await S.set("start_image", msg.photo[-1].file_id)
    context.user_data.pop("state", None)

    await msg.reply_photo(
        msg.photo[-1].file_id,
        caption="✅ Start image saved.",
        reply_markup=K.back(),
    )


@action("scap")
async def a_scap(update, context, args):
    context.user_data["state"] = "scap"

    await show(
        update,
        "📝 <b>Start Caption</b>\n\n"
        "Send the new caption (HTML supported, max 1000 characters).\n"
        "Placeholders: <code>{first_name}</code>, <code>{mention}</code>\n\n"
        f"<b>Current:</b>\n{S.get('start_caption')}",
        M([
            [B("↩️ Reset to default", "adm:scaprs")],
            [B("❌ Cancel", "adm:cancel")],
        ]),
    )


@action("scaprs")
async def a_scaprs(update, context, args):
    await S.set("start_caption", db.DEFAULT_CAPTION)
    await show(update, "✅ Start caption reset to default.", K.back())


@state("scap")
async def st_scap(update, context):
    msg = update.effective_message
    text = msg.text_html if msg.text else None

    if not text or len(text) > 1000:
        await msg.reply_text(
            "⚠️ Send text up to 1000 characters (formatting included).",
            reply_markup=K.cancel(),
        )
        return

    await S.set("start_caption", text)
    context.user_data.pop("state", None)

    await msg.reply_text(
        "✅ Start caption saved. Preview:\n\n" +
        text.replace("{first_name}", "Friend").replace("{mention}", "Friend"),
        reply_markup=K.back(),
    )


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
                raise ValueError(f"Missing ' - ' in: {part}")

            label, url = (x.strip() for x in part.rsplit(" - ", 1))

            if not label or not re.match(r"^(https?://|tg://)\S+$", url):
                raise ValueError(f"Bad button or URL: {part}")

            row.append([label, url])

        if row:
            rows.append(row)

    if not rows:
        raise ValueError("No buttons found")

    return rows


@action("btns")
async def a_btns(update, context, args):
    rows = S.get("custom_buttons")

    cur = "\n".join(
        " | ".join(f"{t} - {u}" for t, u in r)
        for r in rows
    ) or "none"

    await show(
        update,
        "🔘 <b>Join Buttons</b>\n\n"
        "Channel buttons are created from the Force-Sub list.\n"
        "You can add custom buttons below them.\n\n"
        f"<b>Current custom buttons:</b>\n{html.escape(cur)}",
        M([
            [B("✏️ Set custom buttons", "adm:btnset")],
            [B("🗑 Clear custom buttons", "adm:btnclr")],
            [B("🔙 Back", "adm:home")],
        ]),
    )


@action("btnset")
async def a_btnset(update, context, args):
    await ask(
        update,
        context,
        "btns",
        "✏️ <b>Custom Buttons</b>\n\n"
        "Send one button per line:\n"
        "<code>Text - https://example.com</code>\n"
        "Multiple buttons in a row: <code>Text1 - URL1 | Text2 - URL2</code>",
    )


@state("btns")
async def st_btns(update, context):
    msg = update.effective_message

    try:
        rows = parse_buttons(msg.text or "")
    except ValueError as e:
        await msg.reply_text(
            f"⚠️ {html.escape(str(e))}\nFormat: <code>Text - URL</code>",
            reply_markup=K.cancel(),
        )
        return

    await S.set("custom_buttons", rows)
    context.user_data.pop("state", None)

    await msg.reply_text(
        f"✅ Saved {sum(len(r) for r in rows)} custom button(s).",
        reply_markup=K.back("btns"),
    )


@action("btnclr")
async def a_btnclr(update, context, args):
    await S.set("custom_buttons", [])
    await show(update, "✅ Custom buttons cleared.", K.back("btns"))


# ===================================================================== delivery mode (forward / stream)

@action("mode")
async def a_mode(update, context, args):
    note = ""

    if args and args[0] in ("forward", "stream"):
        if args[0] == "stream" and not streamer.ready:
            note = (
                f"⚠️ Streaming is unavailable: "
                f"<code>{html.escape(streamer.error or 'engine not running')}</code>\n\n"
            )
        else:
            await S.set("delivery_mode", args[0])
            note = "✅ Saved.\n\n"

    cur = S.get("delivery_mode")

    def b(m, label):
        return B(
            ("✅ " if cur == m else "") + label,
            f"adm:mode:{m}",
        )

    status = (
        "✅ ready"
        if streamer.ready
        else f"❌ off ({html.escape(streamer.error or 'not started')})"
    )

    await show(
        update,
        f"{note}🎬 <b>Delivery Mode</b>\n\n"
        "Choose what users receive when they open a share link.\n\n"
        "📩 <b>Forward</b>: files are sent in the chat.\n"
        "🌐 <b>Stream</b>: users get Stream and Download buttons (supports large files).\n\n"
        f"Current: <b>{'Stream' if cur == 'stream' else 'Forward'}</b>\n"
        f"Streaming engine: {status}",
        M([
            [b("forward", "📩 Forward"), b("stream", "🌐 Stream")],
            [B("🔙 Back", "adm:home")],
        ]),
    )


# ===================================================================== auto-delete / protect

@action("autodel")
async def a_autodel(update, context, args):
    note = ""

    if args and args[0] in ("0", "5", "10", "30"):
        await S.set("autodelete", int(args[0]))
        note = "✅ Saved.\n\n"

    cur = S.get("autodelete")

    def b(m, label):
        return B(
            ("✅ " if cur == m else "") + label,
            f"adm:autodel:{m}",
        )

    await show(
        update,
        f"{note}⏱ <b>Auto-Delete</b>\n\n"
        "Delivered files are deleted from the user's chat after this time.",
        M([
            [b(0, "Off"), b(5, "5 min"), b(10, "10 min"), b(30, "30 min")],
            [B("🔙 Back", "adm:home")],
        ]),
    )


@action("protect")
async def a_protect(update, context, args):
    note = ""

    if args and args[0] == "t":
        await S.set("protect", not S.get("protect"))
        note = "✅ Updated.\n\n"

    on = S.get("protect")

    await show(
        update,
        f"{note}🛡 <b>Protect Content</b>: {'ON 🔒' if on else 'OFF 🔓'}\n\n"
        "When ON, users cannot forward or save delivered files.",
        M([
            [B("🔓 Turn OFF" if on else "🔒 Turn ON", "adm:protect:t")],
            [B("🔙 Back", "adm:home")],
        ]),
    )


# ===================================================================== file caption

@action("fcap")
async def a_fcap(update, context, args):
    note = ""

    if args and args[0] == "t":
        await S.set("remove_caption", not S.get("remove_caption"))
        note = "✅ Updated.\n\n"

    elif args and args[0] == "clr":
        await S.set("custom_caption", "")
        note = "✅ Custom caption cleared.\n\n"

    elif args and args[0] == "set":
        await ask(
            update,
            context,
            "fcap",
            "✏️ <b>Custom File Caption</b>\n\n"
            "Send the caption (HTML allowed, max 1000 characters). "
            "It replaces the original caption on every delivered file.",
        )
        return

    custom, rem = S.get("custom_caption"), S.get("remove_caption")

    await show(
        update,
        f"{note}🏷 <b>File Caption</b>\n\n"
        f"Custom caption: {custom or '— none —'}\n"
        f"Remove original caption: {'ON' if rem else 'OFF'}\n\n"
        "Order: custom caption, then remove original, then keep original.",
        M([
            [
                B("✏️ Set custom caption", "adm:fcap:set"),
                B("🗑 Clear", "adm:fcap:clr"),
            ],
            [
                B(
                    "🚫 Remove original: " + ("ON ✅" if rem else "OFF"),
                    "adm:fcap:t",
                )
            ],
            [B("🔙 Back", "adm:home")],
        ]),
    )


@state("fcap")
async def st_fcap(update, context):
    msg = update.effective_message
    text = msg.text_html if msg.text else None

    if not text or len(text) > 1000:
        await msg.reply_text(
            "⚠️ Send text up to 1000 characters.",
            reply_markup=K.cancel(),
        )
        return

    await S.set("custom_caption", text)
    context.user_data.pop("state", None)

    await msg.reply_text(
        "✅ Custom caption saved.",
        reply_markup=K.back("fcap"),
    )


# ===================================================================== shortener

@action("short")
async def a_short(update, context, args):
    note = ""

    if args and args[0] == "t":
        await S.set("shortener_enabled", not S.get("shortener_enabled"))
        note = "✅ Updated.\n\n"

    elif args and args[0] == "rm":
        await S.set("shortener_url", "")
        await S.set("shortener_key", "")
        await S.set("shortener_enabled", False)
        note = "✅ Shortener removed.\n\n"

    elif args and args[0] == "set":
        await ask(
            update,
            context,
            "short_url",
            "💸 <b>Shortener API URL</b>\n\n"
            "Send the API URL, e.g. <code>https://gplinks.in/api</code>\n"
            "Request format: <code>URL?api=KEY&amp;url=LINK&amp;format=text</code>",
        )
        return

    url, on = S.get("shortener_url"), S.get("shortener_enabled")

    await show(
        update,
        f"{note}💸 <b>Shortener</b> (optional)\n\n"
        f"API URL: {html.escape(url) or '— not set —'}\n"
        f"API key: {'••••' + S.get('shortener_key')[-3:] if S.get('shortener_key') else '— not set —'}\n"
        f"Status: {'ON ✅' if on else 'OFF'}",
        M([
            [B("✏️ Set API", "adm:short:set")],
            [
                B("🔕 Turn OFF" if on else "🔔 Turn ON", "adm:short:t"),
                B("🗑 Remove", "adm:short:rm"),
            ],
            [B("🔙 Back", "adm:home")],
        ]),
    )


@state("short_url")
async def st_short_url(update, context):
    msg = update.effective_message
    url = (msg.text or "").strip()

    if not re.match(r"^https?://\S+$", url):
        await msg.reply_text(
            "⚠️ Send a valid http(s) URL.",
            reply_markup=K.cancel(),
        )
        return

    context.user_data["short_tmp"] = url
    context.user_data["state"] = "short_key"

    await msg.reply_text(
        "🔑 Send your <b>API key</b>. The message will be deleted afterwards.",
        reply_markup=K.cancel(),
    )


@state("short_key")
async def st_short_key(update, context):
    msg = update.effective_message
    key = (msg.text or "").strip()

    if not key or " " in key:
        await msg.reply_text(
            "⚠️ Send the API key as plain text.",
            reply_markup=K.cancel(),
        )
        return

    await S.set("shortener_url", context.user_data.pop("short_tmp", ""))
    await S.set("shortener_key", key)
    await S.set("shortener_enabled", True)

    context.user_data.pop("state", None)

    try:
        await msg.delete()
    except TelegramError:
        pass

    await update.effective_chat.send_message(
        "✅ Shortener saved and enabled.",
        reply_markup=K.back("short"),
    )


# ===================================================================== broadcast

@action("bc")
async def a_bc(update, context, args):
    await ask(
        update,
        context,
        "bc",
        "📢 <b>Broadcast</b>\n\n"
        "Send or forward the message you want to broadcast to all users.",
    )


@state("bc")
async def st_bc(update, context):
    msg = update.effective_message
    total = await db.count_broadcastable()

    context.user_data["bc_msg"] = (msg.chat_id, msg.message_id)
    context.user_data.pop("state", None)

    await msg.reply_text(
        f"📢 Send this message to <b>{total}</b> users?",
        reply_to_message_id=msg.message_id,
        reply_markup=M([
            [B("✅ Send now", "adm:bcgo")],
            [B("❌ Cancel", "adm:cancel")],
        ]),
    )


@action("bcgo")
async def a_bcgo(update, context, args):
    data = context.user_data.pop("bc_msg", None)

    if not data:
        await show(update, "⚠️ Nothing to broadcast.", K.back())
        return

    q = update.callback_query

    await show(update, "📢 <b>Broadcasting…</b>\n\nStarting…")

    context.application.create_task(
        broadcast.run_broadcast(
            context.bot,
            data[0],
            data[1],
            q.message.chat_id,
            q.message.message_id,
        )
    )


# ===================================================================== stats / ban / reset

@action("stats")
async def a_stats(update, context, args):
    s = await db.stats()

    await show(
        update,
        "📊 <b>Statistics</b>\n\n"
        f"👥 Users: <b>{s['users']}</b>\n"
        f"📁 Files: <b>{s['files']}</b>\n"
        f"🔗 Links created: <b>{s['links']}</b>\n"
        f"🚫 Banned: <b>{s['banned']}</b>\n"
        f"💤 Blocked the bot: <b>{s['blocked']}</b>",
        M([
            [B("🔄 Refresh", "adm:stats")],
            [B("🔙 Back", "adm:home")],
        ]),
    )


@action("ban")
async def a_ban(update, context, args):
    s = await db.stats()

    await show(
        update,
        f"🚫 <b>Ban / Unban</b>\n\nCurrently banned: <b>{s['banned']}</b>",
        M([
            [
                B("🚫 Ban user", "adm:banask"),
                B("✅ Unban user", "adm:unbanask"),
            ],
            [B("🔙 Back", "adm:home")],
        ]),
    )


@action("banask")
async def a_banask(update, context, args):
    await ask(
        update,
        context,
        "ban",
        "🚫 <b>Ban user</b>\n\nForward a message from the user or send their ID.",
    )


@action("unbanask")
async def a_unbanask(update, context, args):
    await ask(
        update,
        context,
        "unban",
        "✅ <b>Unban user</b>\n\nForward a message from the user or send their ID.",
    )


async def _ban_flow(update, context, flag: bool):
    msg = update.effective_message
    uid = extract_user_id(msg)

    if uid is None:
        await msg.reply_text(
            "⚠️ Couldn't read a user ID (hidden forward?). Send the numeric ID.",
            reply_markup=K.cancel(),
        )
        return

    if flag and is_admin(uid):
        await msg.reply_text(
            "⚠️ You can't ban an admin.",
            reply_markup=K.cancel(),
        )
        return

    await db.set_banned(uid, flag)
    context.user_data.pop("state", None)

    await msg.reply_text(
        f"{'🚫 Banned' if flag else '✅ Unbanned'} <code>{uid}</code>.",
        reply_markup=K.back("ban"),
    )


@state("ban")
async def st_ban(update, context):
    await _ban_flow(update, context, True)


@state("unban")
async def st_unban(update, context):
    await _ban_flow(update, context, False)


@action("reset")
async def a_reset(update, context, args):
    await show(
        update,
        "♻️ <b>Reset Settings</b>\n\n"
        "All settings will be restored to default.\n"
        "Admins and the Database Channel are kept.\n\n"
        "<b>Do you want to continue?</b>",
        M([
            [
                B("✅ Yes, reset", "adm:resetyes"),
                B("❌ No", "adm:home"),
            ]
        ]),
    )


@action("resetyes")
async def a_resetyes(update, context, args):
    await S.reset()
    await show(update, "✅ Settings have been reset.", K.back())


# ===================================================================== ads (Monetag Mini App)

def _ago(ts) -> str:
    if not ts:
        return "never"
    d = int(time.time() - ts)
    return f"{d}s ago" if d < 90 else f"{d // 60} min ago" if d < 5400 else f"{d // 3600} h ago"


@action("ads")
async def a_ads(update, context, args):
    note = ""
    a = args[0] if args else ""

    if a == "t":
        if not S.get("ads_enabled") and not ads.zone():
            note = "⚠️ Set the Zone ID first.\n\n"
        else:
            await S.set("ads_enabled", not S.get("ads_enabled"))
            note = "✅ Updated.\n\n"
    elif a == "pb":
        await S.set("ads_postback", not S.get("ads_postback"))
        note = "✅ Updated.\n\n"
    elif a == "ab":
        await S.set("ads_block_adblock", not S.get("ads_block_adblock"))
        note = "✅ Updated.\n\n"
    elif a == "newsec":
        await S.set("ads_secret", "")
        await ads.ensure_secret()
        note = "🔑 New secret created. Paste the NEW postback URL in Monetag.\n\n"
    elif a == "zone":
        await ask(update, context, "ads_zone",
                  "📺 <b>Monetag Zone ID</b>\n\nSend your main <b>SDK zone ID</b> (digits only), "
                  "the number in <code>data-zone=\"123456\"</code> of your Monetag Telegram Mini App code.")
        return
    elif a == "sdk":
        await ask(update, context, "ads_sdk",
                  "🔗 <b>SDK script URL</b>\n\nSend the <code>src</code> of the script tag from your Monetag code, "
                  f"e.g. <code>{html.escape(S.get('ads_sdk_url'))}</code>")
        return

    on = S.get("ads_enabled")
    st = await ads.get_stats()
    pb_url = await ads.postback_url()
    warn = ""
    if not ads.mini_app_ready():
        warn += "⚠️ <b>BASE_URL</b> must be a public https URL, otherwise ads are skipped.\n"
    if not ads.zone():
        warn += "⚠️ Zone ID is not set.\n"
    if on and S.get("ads_postback") and not S.get("ads_last_postback"):
        warn += "⚠️ No postback received yet: paste the Postback URL below in Monetag.\n"
    if not S.get("ads_postback"):
        warn += "⚠️ Server verification is OFF: an ad can be faked from the client. Keep it ON.\n"

    await show(
        update,
        f"{note}📺 <b>Ads (Monetag Mini App)</b>\n\n"
        f"Status: {'ON ✅' if on else 'OFF'}\n"
        f"Zone ID: <code>{html.escape(ads.zone()) or '— not set —'}</code>\n"
        f"SDK URL: <code>{html.escape(ads.sdk_url())}</code>\n"
        f"Server verification (postback): {'ON ✅' if S.get('ads_postback') else 'OFF ⚠️'} "
        f"· last postback: {_ago(S.get('ads_last_postback'))}\n"
        f"AdBlock / DNS blocker check: {'ON ✅' if S.get('ads_block_adblock') else 'OFF'}\n"
        f"No-fill tries before free files: {S.get('ads_tries')} "
        f"(max {ads.FALLBACK_DAILY} such fallbacks per user per day)\n\n"
        f"📈 Ads verified: {st.get('verified', 0)} · Fallback: {st.get('fallback', 0)} · "
        f"Blocked: {st.get('blocked', 0)}\n\n"
        f"{warn}\n"
        "<b>Postback URL</b> (Monetag → your SDK zone → Postback URL):\n"
        f"<code>{html.escape(pb_url)}</code>\n\n"
        "Also add this app URL in Monetag → Telegram Mini App:\n"
        f"<code>{html.escape(config.BASE_URL or '— BASE_URL not set —')}</code>",
        M([
            [B("🔕 Turn OFF" if on else "🔔 Turn ON", "adm:ads:t")],
            [B("✏️ Zone ID", "adm:ads:zone"), B("🔗 SDK URL", "adm:ads:sdk")],
            [B("🛰 Server verify: " + ("ON" if S.get("ads_postback") else "OFF"), "adm:ads:pb")],
            [B("🛡 AdBlock check: " + ("ON" if S.get("ads_block_adblock") else "OFF"), "adm:ads:ab")],
            [B("🔑 New postback secret", "adm:ads:newsec")],
            [B("🔙 Back", "adm:home")],
        ]),
    )


@state("ads_zone")
async def st_ads_zone(update, context):
    msg = update.effective_message
    z = (msg.text or "").strip()
    if not re.fullmatch(r"\d{3,14}", z):
        await msg.reply_text("⚠️ Send the zone ID as digits only.", reply_markup=K.cancel())
        return
    await S.set("ads_zone", z)
    context.user_data.pop("state", None)
    await msg.reply_text(f"✅ Zone ID saved: <code>{z}</code>", reply_markup=K.back("ads"))


@state("ads_sdk")
async def st_ads_sdk(update, context):
    msg = update.effective_message
    u = (msg.text or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    if not re.fullmatch(r"https://[^\s'\"<>]+", u):
        await msg.reply_text("⚠️ Send a valid https URL.", reply_markup=K.cancel())
        return
    await S.set("ads_sdk_url", u)
    context.user_data.pop("state", None)
    await msg.reply_text("✅ SDK URL saved.", reply_markup=K.back("ads"))


# ===================================================================== batch + links

@action("batch")
async def a_batch(update, context, args):
    await show(
        update,
        "📦 <b>Create Batch</b>\n\nOne link for many files. Choose how:",
        M([
            [B("📥 Send files, then tap Done", "adm:batchstart")],
            [B("🔢 First + last post from DB channel", "adm:range")],
            [B("🔙 Back", "adm:home")],
        ]),
    )


@action("batchstart")
async def a_batchstart(update, context, args):
    if not S.get("db_channel"):
        await show(
            update,
            "⚠️ Set the Database Channel first.",
            M([[B("📡 Set Database Channel", "adm:dbch")]]),
        )
        return

    q = update.callback_query

    context.user_data.update(
        state="batch",
        batch_ids=[],
        batch_token=db.new_token(),   # same token is written in every file's backup caption code
        batch_status=(q.message.chat_id, q.message.message_id),
    )

    await show(
        update,
        "📦 <b>Batch mode</b>\n\n"
        "Send your files, then tap <b>Done</b>.",
        K.batch_kb(),
    )


@action("batchdone")
async def a_batchdone(update, context, args):
    q = update.callback_query
    ids = sorted(set(context.user_data.get("batch_ids", [])))

    if not ids:
        await q.answer("Please send at least one file first.", show_alert=True)
        return

    await q.answer()

    token = await db.create_link(
        ids,
        update.effective_user.id,
        f"Batch ({len(ids)} files)",
        token=context.user_data.get("batch_token"),
    )

    for k in TEMP_KEYS:
        context.user_data.pop(k, None)

    await show(
        update,
        f"📦 Batch closed with <b>{len(ids)}</b> files.",
        K.back(),
    )

    await upload.announce_link(
        context,
        q.message.chat_id,
        token,
        len(ids),
        f"Batch ({len(ids)} files)",
    )


@action("range")
async def a_range(update, context, args):
    if not S.get("db_channel"):
        await show(
            update,
            "⚠️ Set the Database Channel first.",
            M([[B("📡 Set Database Channel", "adm:dbch")]]),
        )
        return

    await ask(
        update,
        context,
        "range_first",
        "🔢 <b>Batch by range</b>\n\n"
        "Step 1/2: forward the <b>first</b> post from the Database Channel, or send its link.",
    )


@state("range_first")
async def st_range_first(update, context):
    msg = update.effective_message
    pid = extract_post_id(msg, S.get("db_channel")["id"])

    if pid is None:
        await msg.reply_text(
            "⚠️ This is not a post from the Database Channel.\n"
            "Forward the post or send its link.",
            reply_markup=K.cancel(),
        )
        return

    context.user_data["range_first"] = pid
    context.user_data["state"] = "range_last"

    await msg.reply_text(
        "Step 2/2: now forward the <b>LAST</b> message of the range.",
        reply_markup=K.cancel(),
    )


@state("range_last")
async def st_range_last(update, context):
    msg = update.effective_message
    last = extract_post_id(msg, S.get("db_channel")["id"])
    first = context.user_data.get("range_first")

    if (
        last is None
        or first is None
        or last < first
        or last - first + 1 > MAX_RANGE
    ):
        await msg.reply_text(
            f"⚠️ Send a last post that comes after the first one (max {MAX_RANGE} posts).",
            reply_markup=K.cancel(),
        )
        return

    ids = list(range(first, last + 1))
    label = f"Range {first}-{last}"

    token = await db.create_link(
        ids,
        update.effective_user.id,
        label,
    )

    for k in TEMP_KEYS:
        context.user_data.pop(k, None)

    await upload.announce_link(
        context,
        msg.chat_id,
        token,
        len(ids),
        label,
    )


@action("links")
async def a_links(update, context, args):
    page = int(args[0]) if args else 0
    uid = update.effective_user.id
    creator = None if is_owner(uid) else uid

    total = await db.count_links(creator)
    rows = await db.list_links(page * PAGE, PAGE, creator)

    kb = [
        [
            B(
                f"🔗 {r['label'][:28]} · {r['views']}👁",
                f"adm:lk:{r['_id']}",
            )
        ]
        for r in rows
    ]

    nav = []

    if page > 0:
        nav.append(B("⬅️ Prev", f"adm:links:{page - 1}"))

    if (page + 1) * PAGE < total:
        nav.append(B("Next ➡️", f"adm:links:{page + 1}"))

    if nav:
        kb.append(nav)

    kb.append([B("🔙 Back", "adm:home")])

    await show(
        update,
        (
            f"🔗 <b>My Links</b> ({total})\n\nTap a link to view or revoke it."
            if total
            else "🔗 <b>My Links</b>\n\n"
                 "No links yet. Send a file to create one."
        ),
        M(kb),
    )


@action("lk")
async def a_lk(update, context, args):
    link = await db.get_link(args[0])

    if not link:
        await show(
            update,
            "⚠️ Link not found (already revoked?).",
            K.back("links"),
        )
        return

    url = f"https://t.me/{context.bot.username}?start={link['_id']}"

    await show(
        update,
        f"🔗 <b>{html.escape(link['label'])}</b>\n"
        f"📁 Files: {len(link['ids'])}\n"
        f"👁 Views: {link['views']}\n\n"
        f"<code>{url}</code>",
        M([
            [B("🗑 Revoke link", f"adm:lkdel:{link['_id']}")],
            [B("🔙 Back", "adm:links")],
        ]),
    )


@action("lkdel")
async def a_lkdel(update, context, args):
    await db.delete_link(args[0])
    await show(
        update,
        "🗑 Link revoked. It no longer works.",
        K.back("links"),
    )
