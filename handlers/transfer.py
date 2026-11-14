"""
handlers/transfer.py
🔄 Transfer screen of the admin panel (owner only).

  Panel -> 🔄 Transfer
      🔐 Login           phone + code (+ 2-step password) of YOUR Telegram account, or paste a session string
      🚀 New transfer    pick source (private channel / group / chat) -> destination (channel, chat, Saved
                         Messages) -> everything is copied with captions, no "Forwarded from" tag
      ⏹ Stop             live progress, can be stopped and resumed later

The copy itself runs on Telegram's servers (see utils/transfer.py), so it is fast and uses almost no RAM.
"""
import asyncio
import html
import logging
import re

from telegram import InlineKeyboardMarkup as M

from handlers.admin import B, action, ask, show, state
from utils import keyboards as K
from utils import transfer as T
from utils.helpers import owner_only

log = logging.getLogger(__name__)
PER_PAGE = 8
_watchers: dict = {}


def _esc(s) -> str:
    return html.escape(str(s))


def _short(s: str, n: int = 34) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _bar(p: float, n: int = 12) -> str:
    f = int(round(p / 100 * n))
    return "█" * f + "░" * (n - f)


def _t(sec) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    return f"{sec // 3600}h {sec % 3600 // 60}m" if sec >= 3600 else f"{sec // 60}m {sec % 60}s"


async def _wipe(update):
    """Remove the owner's message that contains a phone / code / password."""
    try:
        await update.effective_message.delete()
    except Exception:
        pass


# ===================================================================== main screen
@action("tr")
@owner_only
async def a_tr(update, context, args):
    bad = T.available()
    if bad:
        await show(update, f"🔄 <b>Transfer</b>\n\n⚠️ {_esc(bad)}", K.back())
        return
    client = await T.session.ensure()
    job = T.CURRENT
    rows = []
    if client:
        me = T.session.me
        head = f"✅ Logged in as <b>{_esc(me.first_name or '')}</b> {('@' + _esc(me.username)) if me.username else ''}"
        rows.append([B("🚀 New transfer", "adm:trnew")])
        if job:
            rows.append([B("📊 Current / last job", "adm:trjob")])
        rows.append([B("🚪 Logout", "adm:trout")])
    else:
        head = "❌ Not logged in"
        rows.append([B("📱 Login with phone", "adm:trlogin")])
        rows.append([B("🔑 Paste session string", "adm:trsess")])
    rows.append([B("🔙 Back", "adm:home")])
    await show(
        update,
        "🔄 <b>Transfer</b>\n\n"
        "Copy everything from a private channel / chat into another channel, chat or Saved Messages - "
        "same files, same captions, <b>no forward tag</b>.\n\n"
        f"{head}\n\n"
        "<i>Runs on Telegram's servers: nothing is downloaded to the VPS, 3000 videos take only a few minutes.</i>",
        M(rows),
    )


# ===================================================================== login
@action("trlogin")
@owner_only
async def a_trlogin(update, context, args):
    await ask(update, context, "tr_phone",
              "📱 Send the phone number of the account that is a member of the source channel,\n"
              "with country code, e.g. <code>+919876543210</code>")


@state("tr_phone")
@owner_only
async def st_phone(update, context):
    phone = (update.effective_message.text or "").strip().replace(" ", "")
    await _wipe(update)
    if not re.fullmatch(r"\+?\d{8,15}", phone):
        await update.effective_chat.send_message("⚠️ Invalid number. Example: <code>+919876543210</code>",
                                                 reply_markup=K.cancel())
        return
    phone = phone if phone.startswith("+") else "+" + phone
    try:
        await T.session.send_code(phone)
    except Exception as e:
        log.exception("send_code failed")
        context.user_data.pop("state", None)
        await update.effective_chat.send_message(f"⚠️ Could not send the code: <code>{_esc(type(e).__name__)}</code>",
                                                 reply_markup=K.back("tr"))
        return
    context.user_data["state"] = "tr_code"
    await update.effective_chat.send_message(
        "💬 Telegram sent a login code to that account (check the Telegram app).\n\n"
        "Send it <b>with spaces between the digits</b>, e.g. <code>1 2 3 4 5</code>\n"
        "<i>(Telegram blocks the login if the code is sent as one plain number.)</i>",
        reply_markup=K.cancel())


@state("tr_code")
@owner_only
async def st_code(update, context):
    code = re.sub(r"\D", "", update.effective_message.text or "")
    await _wipe(update)
    try:
        await T.session.sign_in(code)
    except Exception as e:
        name = type(e).__name__
        if name == "SessionPasswordNeeded":
            context.user_data["state"] = "tr_pw"
            await update.effective_chat.send_message("🔒 This account has a 2-step password. Send it now.",
                                                     reply_markup=K.cancel())
            return
        if name in ("PhoneCodeInvalid", "PhoneCodeEmpty"):
            await update.effective_chat.send_message("⚠️ Wrong code, try again.", reply_markup=K.cancel())
            return
        context.user_data.pop("state", None)
        msg = "⚠️ Code expired. Start the login again." if name == "PhoneCodeExpired" else \
              f"⚠️ Login failed: <code>{_esc(name)}</code>"
        await update.effective_chat.send_message(msg, reply_markup=K.back("tr"))
        return
    context.user_data.pop("state", None)
    await update.effective_chat.send_message("✅ Logged in.", reply_markup=K.back("tr"))


@state("tr_pw")
@owner_only
async def st_pw(update, context):
    pw = update.effective_message.text or ""
    await _wipe(update)
    try:
        await T.session.check_password(pw)
    except Exception as e:
        if type(e).__name__ == "PasswordHashInvalid":
            await update.effective_chat.send_message("⚠️ Wrong password, try again.", reply_markup=K.cancel())
            return
        context.user_data.pop("state", None)
        await update.effective_chat.send_message(f"⚠️ Login failed: <code>{_esc(type(e).__name__)}</code>",
                                                 reply_markup=K.back("tr"))
        return
    context.user_data.pop("state", None)
    await update.effective_chat.send_message("✅ Logged in.", reply_markup=K.back("tr"))


@action("trsess")
@owner_only
async def a_trsess(update, context, args):
    await ask(update, context, "tr_sess",
              "🔑 Paste a <b>Pyrogram session string</b> of your account.\n"
              "<i>The message is deleted right after it is read.</i>")


@state("tr_sess")
@owner_only
async def st_sess(update, context):
    s = (update.effective_message.text or "").strip()
    await _wipe(update)
    try:
        await T.session.start(s)
    except Exception as e:
        await update.effective_chat.send_message(f"⚠️ Session rejected: <code>{_esc(type(e).__name__)}</code>",
                                                 reply_markup=K.cancel())
        return
    context.user_data.pop("state", None)
    await update.effective_chat.send_message("✅ Logged in.", reply_markup=K.back("tr"))


@action("trout")
@owner_only
async def a_trout(update, context, args):
    await show(update, "🚪 Logout this account from the bot?\n<i>The saved session is deleted.</i>",
               M([[B("✅ Yes, logout", "adm:trouty")], [B("🔙 Back", "adm:tr")]]))


@action("trouty")
@owner_only
async def a_trouty(update, context, args):
    if T.busy():
        T.CURRENT.stop = True
    await T.session.logout()
    await a_tr(update, context, args)


# ===================================================================== choose source / destination
def _page(items: list, page: int, pick: str, nav: str, extra: list):
    n = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, n - 1))
    rows = list(extra)
    for i in range(page * PER_PAGE, min(len(items), (page + 1) * PER_PAGE)):
        _id, title, icon = items[i]
        rows.append([B(f"{icon} {_short(title)}", f"adm:{pick}:{i}")])
    if n > 1:
        nav_row = []
        if page > 0:
            nav_row.append(B("⬅️ Prev", f"adm:{nav}:{page - 1}"))
        nav_row.append(B(f"{page + 1}/{n}", "adm:noop"))
        if page < n - 1:
            nav_row.append(B("Next ➡️", f"adm:{nav}:{page + 1}"))
        rows.append(nav_row)
    rows.append([B("🔙 Back", "adm:tr")])
    return M(rows)


async def _dialogs(context, refresh=False):
    if refresh or "tr_dl" not in context.user_data:
        client = await T.session.ensure()
        context.user_data["tr_dl"] = await T.list_dialogs(client) if client else []
    return context.user_data["tr_dl"]


@action("trnew")
@owner_only
async def a_trnew(update, context, args):
    if T.busy():
        await show(update, "⚠️ A transfer is already running.", M([[B("📊 Open it", "adm:trjob")], [B("🔙 Back", "adm:tr")]]))
        return
    if not await T.session.ensure():
        await a_tr(update, context, args)
        return
    for k in ("tr_src", "tr_dst", "tr_from"):
        context.user_data.pop(k, None)
    await show(update, "⏳ Loading your chats…")
    await a_trsp(update, context, ["0", "1"])


@action("trsp")
@owner_only
async def a_trsp(update, context, args):
    page = int(args[0]) if args else 0
    items = await _dialogs(context, refresh=bool(len(args) > 1))
    context.user_data["state"] = "tr_src_in"
    await show(update,
               "📤 <b>Step 1/2 - SOURCE</b>\n\nTap the channel / chat to copy FROM, or send its link, @username or ID.\n"
               "<i>Your account must be a member.</i>",
               _page(items, page, "trsrc", "trsp", []))


async def _set_chat(update, context, key: str, chat_id, title: str):
    context.user_data[key] = (chat_id, title)


@action("trsrc")
@owner_only
async def a_trsrc(update, context, args):
    items = await _dialogs(context)
    cid, title, icon = items[int(args[0])]
    await _set_chat(update, context, "tr_src", cid, title)
    await _ask_dst(update, context, 0)


@state("tr_src_in")
@owner_only
async def st_src_in(update, context):
    try:
        chat = await T.resolve_chat(await T.session.ensure(), update.effective_message.text)
    except Exception as e:
        await update.effective_chat.send_message(
            f"⚠️ Cannot open that chat (<code>{_esc(type(e).__name__)}</code>). Is your account a member?",
            reply_markup=K.cancel())
        return
    await _set_chat(update, context, "tr_src", chat.id, T.chat_title(chat))
    await _ask_dst(update, context, 0)


async def _ask_dst(update, context, page: int):
    items = await _dialogs(context)
    context.user_data["state"] = "tr_dst_in"
    src = context.user_data["tr_src"][1]
    extra = [[B("💾 Saved Messages", "adm:trdst:me")]]
    await show(update,
               f"📥 <b>Step 2/2 - DESTINATION</b>\n\nFrom: <b>{_esc(src)}</b>\n\n"
               "Tap where to copy TO (your account needs permission to post), or send its link, @username or ID.",
               _page(items, page, "trdst", "trdp", extra))


@action("trdp")
@owner_only
async def a_trdp(update, context, args):
    await _ask_dst(update, context, int(args[0]) if args else 0)


@action("trdst")
@owner_only
async def a_trdst(update, context, args):
    if args and args[0] == "me":
        me = T.session.me
        await _set_chat(update, context, "tr_dst", me.id, "Saved Messages")
    else:
        cid, title, icon = (await _dialogs(context))[int(args[0])]
        await _set_chat(update, context, "tr_dst", cid, title)
    await _confirm(update, context)


@state("tr_dst_in")
@owner_only
async def st_dst_in(update, context):
    try:
        chat = await T.resolve_chat(await T.session.ensure(), update.effective_message.text)
    except Exception as e:
        await update.effective_chat.send_message(
            f"⚠️ Cannot open that chat (<code>{_esc(type(e).__name__)}</code>).", reply_markup=K.cancel())
        return
    title = "Saved Messages" if chat.id == T.session.me.id else T.chat_title(chat)
    await _set_chat(update, context, "tr_dst", chat.id, title)
    await _confirm(update, context)


async def _confirm(update, context):
    context.user_data.pop("state", None)
    (sid, stitle), (did, dtitle) = context.user_data["tr_src"], context.user_data["tr_dst"]
    if sid == did:
        await show(update, "⚠️ Source and destination are the same chat.", K.back("trnew"))
        return
    last = await T.last_progress(sid, did)
    rows = [[B("🚀 Start - copy everything", "adm:trgo:0")]]
    note = ""
    if last:
        rows.insert(0, [B(f"⏯ Resume after message #{last}", f"adm:trgo:{last + 1}")])
        note = f"\n\n⏯ An earlier run reached message <b>#{last}</b>."
    rows.append([B("🔢 Start from message ID…", "adm:trfrom")])
    rows.append([B("❌ Cancel", "adm:cancel")])
    await show(update,
               "🔄 <b>Ready</b>\n\n"
               f"📤 From: <b>{_esc(stitle)}</b>\n📥 To: <b>{_esc(dtitle)}</b>\n\n"
               "Everything is copied in order with captions, without the forward tag." + note,
               M(rows))


@action("trfrom")
@owner_only
async def a_trfrom(update, context, args):
    await ask(update, context, "tr_from", "🔢 Send the message ID to start from (e.g. <code>1250</code>).")


@state("tr_from")
@owner_only
async def st_from(update, context):
    txt = (update.effective_message.text or "").strip()
    m = re.search(r"(\d+)\s*$", txt)
    if not m:
        await update.effective_chat.send_message("⚠️ Send a number.", reply_markup=K.cancel())
        return
    await _go(update, context, int(m.group(1)))


# ===================================================================== run + live progress
@action("trgo")
@owner_only
async def a_trgo(update, context, args):
    await _go(update, context, int(args[0]) if args and args[0].isdigit() and int(args[0]) > 0 else 1)


async def _go(update, context, start_id: int):
    context.user_data.pop("state", None)
    if T.busy():
        await show(update, "⚠️ A transfer is already running.", M([[B("📊 Open it", "adm:trjob")]]))
        return
    if "tr_src" not in context.user_data or "tr_dst" not in context.user_data:
        await show(update, "⚠️ Session expired, start again.", K.back("tr"))
        return
    (sid, stitle), (did, dtitle) = context.user_data["tr_src"], context.user_data["tr_dst"]
    job = T.start_job(T.Job(src=sid, dst=did, src_title=stitle, dst_title=dtitle, start_id=max(1, start_id)))
    await _watch(update, context, job)


@action("trjob")
@owner_only
async def a_trjob(update, context, args):
    job = T.CURRENT
    if not job:
        await show(update, "No transfer yet.", K.back("tr"))
        return
    await _watch(update, context, job)


def _status(job) -> str:
    icon = {"done": "✅", "stopped": "⏸", "error": "⚠️"}.get(job.state, "🔄")
    txt = (f"{icon} <b>Transfer</b> - {_esc(job.state)}\n\n"
           f"📤 {_esc(job.src_title)}\n📥 {_esc(job.dst_title)}\n\n"
           f"<code>{_bar(job.percent)}</code> {job.percent:.0f}%\n"
           f"✅ Copied: <b>{job.copied}</b>   ⚠️ Failed: <b>{job.failed}</b>   ⏭ Skipped: {job.skipped}\n"
           f"⚡ {job.speed:.1f} msg/s   ⏱ left: {_t(job.eta) if job.running else '-'}")
    if job.error:
        txt += f"\n\n⚠️ {_esc(job.error)}"
    if job.state == "done":
        txt += "\n\nAll done 🎉"
    return txt


async def _watch(update, context, job):
    """Edit one message with live numbers until the job ends."""
    q = update.callback_query
    chat_id = update.effective_chat.id
    kb0 = M([[B("⏹ Stop", "adm:trstop")]]) if job.running else K.back("tr")
    if q:
        await show(update, _status(job), kb0)
        msg = q.message
    else:                                   # started from a typed answer (message ID): send a fresh message
        msg = await update.effective_chat.send_message(_status(job), reply_markup=kb0)
    old = _watchers.pop(chat_id, None)
    if old:
        old.cancel()

    async def loop():
        last = ""
        while True:
            await asyncio.sleep(3)
            txt = _status(job)
            kb = M([[B("⏹ Stop", "adm:trstop")]]) if job.running else K.back("tr")
            if txt != last:
                try:
                    await msg.edit_text(txt, reply_markup=kb)
                    last = txt
                except Exception as e:
                    if "not modified" not in str(e).lower():
                        log.debug("progress edit: %s", e)
            if not job.running:
                return

    _watchers[chat_id] = asyncio.create_task(loop())


@action("trstop")
@owner_only
async def a_trstop(update, context, args):
    if T.busy():
        T.CURRENT.stop = True
    await asyncio.sleep(1.5)
    if T.CURRENT:
        await show(update, _status(T.CURRENT), K.back("tr"))
