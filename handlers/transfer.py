"""
handlers/transfer.py
🔄 Transfer screen of the admin panel (owner only).

  Panel -> 🔄 Transfer
      🔐 Login           phone + code (+ 2-step password) of YOUR Telegram account, or paste a session string
      🚀 New transfer    pick source (private channel / group / chat) -> destination (channel, chat, Saved
                         Messages) -> everything is copied with captions, no "Forwarded from" tag
      ⏸ Pause / ▶️ Resume / ⏹ Stop / ❌ Cancel, live speed, per-type counts (videos, photos, files, text ...),
                         "not forwarded" list + retry
      🗑 Delete          remove everything that was copied into a chat (or wipe a whole chat) in one go

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
        rows.append([B("🗑 Delete copied files / wipe a chat", "adm:trcl")])
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
        T.CURRENT.request_stop()
    if T.clean_busy():
        T.CLEAN.stop = True
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
    if T.clean_busy():
        await show(update, "⚠️ A delete job is running.", M([[B("📊 Open it", "adm:trclj")], [B("🔙 Back", "adm:tr")]]))
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
    if T.clean_busy():
        await show(update, "⚠️ A delete job is running.", M([[B("📊 Open it", "adm:trclj")]]))
        return
    if "tr_src" not in context.user_data or "tr_dst" not in context.user_data:
        await show(update, "⚠️ Session expired, start again.", K.back("tr"))
        return
    (sid, stitle), (did, dtitle) = context.user_data["tr_src"], context.user_data["tr_dst"]
    job = T.start_job(T.Job(src=sid, dst=did, src_title=stitle, dst_title=dtitle, start_id=max(1, start_id)))
    await _watch(update, context, job, _status, _kb)


@action("trjob")
@owner_only
async def a_trjob(update, context, args):
    job = T.CURRENT
    if not job:
        await show(update, "No transfer yet.", K.back("tr"))
        return
    await _watch(update, context, job, _status, _kb)


def _link(src: int, mid: int) -> str:
    s = str(src)
    return f"https://t.me/c/{s[4:]}/{mid}" if s.startswith("-100") else ""


def _status(job) -> str:
    if job.paused:
        icon, label = "⏸", "paused"
    else:
        icon = {"done": "✅", "stopped": "⏹", "cancelled": "❌", "error": "⚠️"}.get(job.state, "🔄")
        label = {"starting": "starting", "scanning": "reading source", "running": "copying", "done": "done",
                 "stopped": "stopped (can be resumed)", "cancelled": "cancelled", "error": "error"}.get(job.state, job.state)
    txt = (f"{icon} <b>{'Retry' if job.only_items is not None else 'Transfer'}</b> - {label}\n\n"
           f"📤 {_esc(job.src_title)}\n📥 {_esc(job.dst_title)}\n\n"
           f"<code>{_bar(job.percent)}</code> {job.percent:.0f}%\n")

    if job.phase == "scan" and job.running:
        txt += f"🔍 Counting what is in the source… <b>{job.total}</b> messages so far\n"
    rows = []
    for k, ic, name in T.KINDS:
        seen = job.seen.get(k, 0)
        if not seen:
            continue
        line = f"{ic} {name}: <b>{job.copied_by.get(k, 0)}</b> / {seen}"
        if job.failed_by.get(k):
            line += f"   ⚠️ {job.failed_by[k]} not forwarded"
        rows.append(line)
    if rows:
        txt += "\n<b>Source → transferred</b>\n" + "\n".join(rows) + "\n"

    txt += (f"\n📦 In source: <b>{job.total}</b>\n"
            f"✅ Transferred: <b>{job.copied}</b>\n"
            f"🚫 Not forwarded: <b>{job.failed}</b>\n"
            f"⏳ Left: <b>{job.left}</b>   ⏭ Skipped (deleted/service): {job.skipped}\n")
    if job.fail_reasons:
        why = ", ".join(f"{_esc(r)} ×{n}" for r, n in job.fail_reasons.most_common(3))
        txt += f"❓ Why: {why}\n"
    if job.running:
        txt += f"⚡ {job.speed:.1f} msg/s   ⏱ left: {_t(job.eta)}   🎚 {T.SPEEDS[job.speed_key][0]}"
        if job.flood_left and not job.paused:
            txt += f"\n⏳ Telegram limit - waiting {job.flood_left}s"
    else:
        txt += f"⏱ Took {_t(job.elapsed())}"
    if job.sent_n:
        txt += f"\n🗑 {job.sent_n} copied messages are tracked and can be deleted in one tap."
    if job.error:
        txt += f"\n\n⚠️ {_esc(job.error)}"
    if job.state == "done":
        txt += "\n\nAll done 🎉" if not job.failed else "\n\nDone - some messages could not be forwarded."
    if job.state == "stopped":
        txt += "\n\nProgress is saved. Start a new transfer with the same chats to resume."
    return txt


def _kb(job):
    if not job.running:
        rows = []
        if job.failed_items:
            rows.append([B(f"📋 Not forwarded ({job.failed})", "adm:trfail"), B("🔁 Retry failed", "adm:trretry")])
        if job.sent_n:
            rows.append([B(f"🗑 Delete the {job.sent_n} copied files", "adm:trdelj")])
        rows.append([B("🔙 Back", "adm:tr")])
        return M(rows)
    main = [B("▶️ Resume", "adm:trresume") if job.paused else B("⏸ Pause", "adm:trpause"),
            B("⏹ Stop", "adm:trstop")]
    speed = [B(("✅ " if k == job.speed_key else "") + name, f"adm:trspd:{k}") for k, (name, _d) in T.SPEEDS.items()]
    rows = [main, [B("❌ Cancel", "adm:trcancel")], speed]
    if job.failed_items:
        rows.append([B(f"📋 Not forwarded ({job.failed})", "adm:trfail")])
    return M(rows)


async def _watch(update, context, obj, render, kbf):
    """Edit one message with live numbers until the job ends."""
    q = update.callback_query
    chat_id = update.effective_chat.id
    if q:
        await show(update, render(obj), kbf(obj))
        msg = q.message
    else:                                   # started from a typed answer (message ID): send a fresh message
        msg = await update.effective_chat.send_message(render(obj), reply_markup=kbf(obj))
    old = _watchers.pop(chat_id, None)
    if old:
        old.cancel()

    async def loop():
        last = ""
        while True:
            await asyncio.sleep(3)
            txt, kb = render(obj), kbf(obj)
            sig = txt + "|" + ",".join(b.callback_data for r in kb.inline_keyboard for b in r)
            if sig != last:
                try:
                    await msg.edit_text(txt, reply_markup=kb)
                    last = sig
                except Exception as e:
                    if "not modified" in str(e).lower():
                        last = sig
                    else:
                        log.debug("progress edit: %s", e)
            if not obj.running:
                return

    _watchers[chat_id] = asyncio.create_task(loop())


async def _refresh(update):
    if T.CURRENT:
        await show(update, _status(T.CURRENT), _kb(T.CURRENT))


async def _settle(job, secs: float = 4.0):
    """Wait (briefly) until a stopping job has really ended."""
    end = asyncio.get_event_loop().time() + secs
    while job.running and asyncio.get_event_loop().time() < end:
        await asyncio.sleep(0.4)


@action("trpause")
@owner_only
async def a_trpause(update, context, args):
    if T.busy():
        T.CURRENT.pause()
    await _refresh(update)


@action("trresume")
@owner_only
async def a_trresume(update, context, args):
    if T.busy():
        T.CURRENT.resume()
    await _refresh(update)


@action("trstop")
@owner_only
async def a_trstop(update, context, args):
    """Stop = end the job but keep the saved resume point."""
    if T.busy():
        T.CURRENT.request_stop()
        await _settle(T.CURRENT)
    await _refresh(update)


@action("trcancel")
@owner_only
async def a_trcancel(update, context, args):
    if not T.busy():
        await _refresh(update)
        return
    await show(update,
               "❌ <b>Cancel this transfer?</b>\n\n"
               "The saved resume point is discarded.\n"
               "Files that were already copied stay in the destination - you can delete them afterwards in one tap.",
               M([[B("✅ Yes, cancel", "adm:trcancely")], [B("↩️ No, keep going", "adm:trjob")]]))


@action("trcancely")
@owner_only
async def a_trcancely(update, context, args):
    if T.busy():
        T.CURRENT.request_stop(cancel=True)
        await _settle(T.CURRENT)
    if T.CURRENT:
        await _watch(update, context, T.CURRENT, _status, _kb)


@action("trspd")
@owner_only
async def a_trspd(update, context, args):
    if T.busy() and args:
        T.CURRENT.set_speed(args[0])
    await _refresh(update)


@action("trfail")
@owner_only
async def a_trfail(update, context, args):
    job = T.CURRENT
    if not job or not job.failed:
        await show(update, "✅ Nothing failed.", K.back("trjob"))
        return
    lines = []
    for mid, kind, why in job.failed_items[:25]:
        ic = T.KIND_INFO.get(kind, ("📎", ""))[0]
        url = _link(job.src, mid)
        ref = f'<a href="{url}">#{mid}</a>' if url else f"#{mid}"
        lines.append(f"{ic} {ref} - {_esc(why)}")
    more = job.failed - min(len(job.failed_items), 25)
    why = ", ".join(f"{_esc(r)} ×{n}" for r, n in job.fail_reasons.most_common(5))
    rows = [[B("🔁 Retry failed", "adm:trretry")]] if (job.failed_items and not job.running) else []
    rows.append([B("🔙 Back", "adm:trjob")])
    await show(update,
               f"🚫 <b>Not forwarded: {job.failed}</b>\n\n{why}\n\n" + "\n".join(lines)
               + (f"\n… and {more} more" if more > 0 else ""),
               M(rows))


@action("trretry")
@owner_only
async def a_trretry(update, context, args):
    if T.busy() or T.clean_busy():
        await show(update, "⚠️ Another job is running.", M([[B("📊 Open it", "adm:trjob")]]))
        return
    nj = T.retry_job(T.CURRENT) if T.CURRENT else None
    if not nj:
        await show(update, "Nothing to retry.", K.back("tr"))
        return
    await _watch(update, context, T.start_job(nj), _status, _kb)


# ===================================================================== one-shot delete
def _cstatus(cj) -> str:
    icon = {"done": "✅", "stopped": "⏹", "error": "⚠️"}.get(cj.state, "🗑")
    label = {"starting": "starting", "scanning": "finding messages", "running": "deleting",
             "done": "done", "stopped": "stopped", "error": "error"}.get(cj.state, cj.state)
    what = "everything in the chat" if cj.mode == "all" else "files copied by the transfer"
    txt = (f"{icon} <b>Delete</b> - {label}\n\n📥 {_esc(cj.title)}\n<i>{what}</i>\n\n"
           f"<code>{_bar(cj.percent)}</code> {cj.percent:.0f}%\n"
           f"🗑 Deleted: <b>{cj.deleted}</b> / {cj.total}")
    if cj.missing:
        txt += f"\n👻 Already gone: {cj.missing}"
    if cj.failed:
        txt += f"\n⚠️ Failed: {cj.failed}"
    if cj.error:
        txt += f"\n\n⚠️ {_esc(cj.error)}"
    if cj.state == "done":
        txt += "\n\nAll deleted 🎉"
    return txt


def _ckb(cj):
    return M([[B("⏹ Stop", "adm:trclstop")]]) if cj.running else K.back("tr")


@action("trclj")
@owner_only
async def a_trclj(update, context, args):
    if not T.CLEAN:
        await show(update, "No delete job yet.", K.back("tr"))
        return
    await _watch(update, context, T.CLEAN, _cstatus, _ckb)


@action("trclstop")
@owner_only
async def a_trclstop(update, context, args):
    if T.clean_busy():
        T.CLEAN.stop = True
        for _ in range(10):
            if not T.CLEAN.running:
                break
            await asyncio.sleep(0.4)
    if T.CLEAN:
        await show(update, _cstatus(T.CLEAN), _ckb(T.CLEAN))


@action("trcl")
@owner_only
async def a_trcl(update, context, args):
    if T.busy():
        await show(update, "⚠️ A transfer is running. Stop or cancel it first.", M([[B("📊 Open it", "adm:trjob")]]))
        return
    if T.clean_busy():
        await a_trclj(update, context, args)
        return
    if not await T.session.ensure():
        await a_tr(update, context, args)
        return
    items = await T.tracked()
    context.user_data["tr_cl"] = items
    rows = []
    for i, d in enumerate(items):
        rows.append([B(f"🗑 {_short(d['dst_title'], 24)} · {d['n']} files", f"adm:trclsel:{i}")])
    rows.append([B("🧹 Wipe a whole chat…", "adm:trwp")])
    rows.append([B("🔙 Back", "adm:tr")])
    await show(update,
               "🗑 <b>Delete in one go</b>\n\n"
               + ("Pick a destination to delete <b>everything this bot copied into it</b>:" if items
                  else "Nothing copied by the bot is tracked yet.\n")
               + "\n\n🧹 <b>Wipe a whole chat</b> removes ALL messages in a chat (e.g. Saved Messages).",
               M(rows))


def _tracked_item(context, args):
    items = context.user_data.get("tr_cl") or []
    try:
        return items[int(args[0])]
    except (IndexError, ValueError):
        return None


@action("trclsel")
@owner_only
async def a_trclsel(update, context, args):
    d = _tracked_item(context, args)
    if not d:
        await a_trcl(update, context, [])
        return
    await show(update,
               f"🗑 <b>{_esc(d['dst_title'])}</b>\n\n"
               f"The bot copied <b>{d['n']}</b> messages here (from {_esc(d['src_title'])}).\n"
               "Delete exactly those - other messages in the chat are not touched.",
               M([[B(f"✅ Delete the {d['n']} copied files", f"adm:trcly:{args[0]}")],
                  [B("🧹 Wipe the WHOLE chat instead…", f"adm:trclw:{args[0]}")],
                  [B("🔙 Back", "adm:trcl")]]))


@action("trcly")
@owner_only
async def a_trcly(update, context, args):
    d = _tracked_item(context, args)
    if not d:
        await a_trcl(update, context, [])
        return
    if T.busy() or T.clean_busy():
        await show(update, "⚠️ Another job is running.", K.back("tr"))
        return
    cj = T.start_clean(T.CleanJob(dst=d["dst"], title=d["dst_title"], mode="tracked", src=d["src"]))
    await _watch(update, context, cj, _cstatus, _ckb)


@action("trdelj")
@owner_only
async def a_trdelj(update, context, args):
    """From the job screen: delete what this job copied."""
    job = T.CURRENT
    items = await T.tracked()
    context.user_data["tr_cl"] = items
    for i, d in enumerate(items):
        if job and d["src"] == job.src and d["dst"] == job.dst:
            await a_trclsel(update, context, [str(i)])
            return
    await show(update, "Nothing tracked for this transfer.", K.back("tr"))


# ---- wipe a whole chat
@action("trclw")
@owner_only
async def a_trclw(update, context, args):
    d = _tracked_item(context, args)
    if not d:
        await a_trcl(update, context, [])
        return
    context.user_data["tr_wp"] = (d["dst"], d["dst_title"])
    await _wipe_confirm(update, context)


@action("trwp")
@owner_only
async def a_trwp(update, context, args):
    page = int(args[0]) if args else 0
    items = await _dialogs(context, refresh=bool(len(args) > 1))
    context.user_data["state"] = "tr_wipe_in"
    rows = [[B("💾 Saved Messages", "adm:trwps:me")]]
    await show(update,
               "🧹 <b>Wipe a chat</b>\n\nTap the chat whose messages should ALL be deleted, "
               "or send its link, @username or ID.",
               _page(items, page, "trwps", "trwp", rows))


@action("trwps")
@owner_only
async def a_trwps(update, context, args):
    if args and args[0] == "me":
        context.user_data["tr_wp"] = (T.session.me.id, "Saved Messages")
    else:
        cid, title, _ = (await _dialogs(context))[int(args[0])]
        context.user_data["tr_wp"] = (cid, title)
    await _wipe_confirm(update, context)


@state("tr_wipe_in")
@owner_only
async def st_wipe_in(update, context):
    try:
        chat = await T.resolve_chat(await T.session.ensure(), update.effective_message.text)
    except Exception as e:
        await update.effective_chat.send_message(
            f"⚠️ Cannot open that chat (<code>{_esc(type(e).__name__)}</code>).", reply_markup=K.cancel())
        return
    title = "Saved Messages" if chat.id == T.session.me.id else T.chat_title(chat)
    context.user_data["tr_wp"] = (chat.id, title)
    await _wipe_confirm(update, context)


async def _wipe_confirm(update, context):
    context.user_data.pop("state", None)
    cid, title = context.user_data["tr_wp"]
    client = await T.session.ensure()
    n = await T.count_chat(client, cid)
    warn = ""
    try:
        chat = await client.get_chat(cid)
        if T.kind_icon(chat) == "👤" and cid != T.session.me.id:
            warn = "\n⚠️ This is a private chat - the messages are deleted for the other person too."
    except Exception:
        pass
    await show(update,
               f"🧹 <b>Wipe: {_esc(title)}</b>\n\n"
               f"ALL messages in this chat (about <b>{n}</b>) will be deleted. This cannot be undone.{warn}",
               M([[B("🧹 Yes, delete everything", "adm:trwpy")], [B("❌ No, keep it", "adm:trcl")]]))


@action("trwpy")
@owner_only
async def a_trwpy(update, context, args):
    tgt = context.user_data.get("tr_wp")
    if not tgt:
        await a_trcl(update, context, [])
        return
    if T.busy() or T.clean_busy():
        await show(update, "⚠️ Another job is running.", K.back("tr"))
        return
    cj = T.start_clean(T.CleanJob(dst=tgt[0], title=tgt[1], mode="all"))
    context.user_data.pop("tr_wp", None)
    await _watch(update, context, cj, _cstatus, _ckb)
