"""
handlers/transfer.py
🔄 Transfer screen of the admin panel (owner only).

  Panel -> Servers -> Transfer
      Login          phone + code (+ 2-step password) of YOUR Telegram account, or a session string
      New Transfer   source -> destination -> everything is copied with captions, no "Forwarded from" tag
      Job screen     pause / resume / stop / cancel, live speed, per-type counts, failed list + retry
      Cleanup        delete what was copied into a chat (or wipe a whole chat) in one go

The copy itself runs on Telegram's servers (see utils/transfer.py), so it is fast and uses almost no RAM.
"""
import asyncio
import logging
import re

from handlers.admin import action, ask, show, state
from utils import keyboards as K
from utils import transfer as T
from utils.helpers import owner_only
from utils.keyboards import grid, kb, nm, pager, ub
from utils.ui import bar as _bar, esc as _esc, head, hl

log = logging.getLogger(__name__)
B = ub          # CAPS panel button; chat titles use nm()

PER_PAGE = 8
_watchers: dict = {}


def _short(s: str, n: int = 34) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


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
        await show(update, head("🔄", "Transfer", f"⚠️ {_esc(bad)}"), K.back("servers"))
        return
    client = await T.session.ensure()
    job = T.CURRENT
    if client:
        me = T.session.me
        who = f"{_esc(me.first_name or '')} {('@' + _esc(me.username)) if me.username else ''}".strip()
        line = f"✅ LOGGED IN ▸ <b>{who}</b>"
        items = [("🚀 New Transfer", "adm:trnew"), ("🗑 Cleanup", "adm:trcl")]
        if job:
            items.append(("📊 Current Job", "adm:trjob"))
        items.append(("🚪 Logout", "adm:trout"))
    else:
        line = "❌ NOT LOGGED IN"
        items = [("📱 Phone Login", "adm:trlogin"), ("🔑 Session", "adm:trsess")]
    await show(update, head("🔄", "Transfer", f"COPY FILES BETWEEN CHATS, NO FORWARD TAG.\n\n{line}"),
               kb(grid(items), B("🔙 Back", "adm:servers")))


# ===================================================================== login
@action("trlogin")
@owner_only
async def a_trlogin(update, context, args):
    await ask(update, context, "tr_phone",
              head("📱", "Phone Login", "SEND THE PHONE NUMBER OF AN ACCOUNT THAT IS IN THE SOURCE CHAT,\n"
                                        "WITH COUNTRY CODE: <code>+919876543210</code>"))


@state("tr_phone")
@owner_only
async def st_phone(update, context):
    phone = (update.effective_message.text or "").strip().replace(" ", "")
    await _wipe(update)
    if not re.fullmatch(r"\+?\d{8,15}", phone):
        await update.effective_chat.send_message("⚠️ INVALID NUMBER. EXAMPLE ▸ <code>+919876543210</code>",
                                                 reply_markup=K.cancel())
        return
    phone = phone if phone.startswith("+") else "+" + phone
    try:
        await T.session.send_code(phone)
    except Exception as e:
        log.exception("send_code failed")
        context.user_data.pop("state", None)
        await update.effective_chat.send_message(f"⚠️ CODE NOT SENT ▸ <code>{_esc(type(e).__name__)}</code>",
                                                 reply_markup=K.back("tr"))
        return
    context.user_data["state"] = "tr_code"
    await update.effective_chat.send_message(
        head("💬", "Login Code", "CHECK THE TELEGRAM APP, THEN SEND THE CODE\n"
                                 "<b>WITH SPACES</b> BETWEEN DIGITS: <code>1 2 3 4 5</code>\n"
                                 "<i>TELEGRAM BLOCKS A CODE SENT AS ONE NUMBER.</i>"),
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
            await update.effective_chat.send_message(head("🔒", "2-Step Password", "SEND THE ACCOUNT PASSWORD."),
                                                     reply_markup=K.cancel())
            return
        if name in ("PhoneCodeInvalid", "PhoneCodeEmpty"):
            await update.effective_chat.send_message("⚠️ WRONG CODE. TRY AGAIN.", reply_markup=K.cancel())
            return
        context.user_data.pop("state", None)
        msg = "⚠️ CODE EXPIRED. START THE LOGIN AGAIN." if name == "PhoneCodeExpired" else \
              f"⚠️ LOGIN FAILED ▸ <code>{_esc(name)}</code>"
        await update.effective_chat.send_message(msg, reply_markup=K.back("tr"))
        return
    context.user_data.pop("state", None)
    await update.effective_chat.send_message("✅ LOGGED IN.", reply_markup=K.back("tr"))


@state("tr_pw")
@owner_only
async def st_pw(update, context):
    pw = update.effective_message.text or ""
    await _wipe(update)
    try:
        await T.session.check_password(pw)
    except Exception as e:
        if type(e).__name__ == "PasswordHashInvalid":
            await update.effective_chat.send_message("⚠️ WRONG PASSWORD. TRY AGAIN.", reply_markup=K.cancel())
            return
        context.user_data.pop("state", None)
        await update.effective_chat.send_message(f"⚠️ LOGIN FAILED ▸ <code>{_esc(type(e).__name__)}</code>",
                                                 reply_markup=K.back("tr"))
        return
    context.user_data.pop("state", None)
    await update.effective_chat.send_message("✅ LOGGED IN.", reply_markup=K.back("tr"))


@action("trsess")
@owner_only
async def a_trsess(update, context, args):
    await ask(update, context, "tr_sess",
              head("🔑", "Session", "PASTE A <b>PYROGRAM SESSION STRING</b>.\n" + hl("THE MESSAGE IS DELETED RIGHT AFTER.")))


@state("tr_sess")
@owner_only
async def st_sess(update, context):
    s = (update.effective_message.text or "").strip()
    await _wipe(update)
    try:
        await T.session.start(s)
    except Exception as e:
        await update.effective_chat.send_message(f"⚠️ SESSION REJECTED ▸ <code>{_esc(type(e).__name__)}</code>",
                                                 reply_markup=K.cancel())
        return
    context.user_data.pop("state", None)
    await update.effective_chat.send_message("✅ LOGGED IN.", reply_markup=K.back("tr"))


@action("trout")
@owner_only
async def a_trout(update, context, args):
    await show(update, head("🚪", "Logout", "REMOVE THIS ACCOUNT FROM THE BOT?\n" + hl("THE SAVED SESSION IS DELETED.")),
               K.confirm("adm:trouty", "adm:tr", "✅ Yes, Logout", "❌ No"))


@action("trouty")
@owner_only
async def a_trouty(update, context, args):
    try:
        if T.busy():
            T.CURRENT.request_stop()
        cb = getattr(T, "clean_busy", None)      # older utils/transfer.py has no wipe/delete jobs
        if cb and cb():
            T.CLEAN.stop = True
    except Exception:
        log.exception("could not stop running job before logout")
    try:
        await T.session.logout()
    except Exception:
        log.exception("logout failed")
    await a_tr(update, context, args)


# ===================================================================== choose source / destination
def _page(items: list, page: int, pick: str, nav: str, extra: list):
    n = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, n - 1))
    rows = list(extra)
    for i in range(page * PER_PAGE, min(len(items), (page + 1) * PER_PAGE)):
        _id, title, icon = items[i]
        rows.append([nm(f"{icon} {_short(title)}", f"adm:{pick}:{i}")])
    rows.append(pager(page, n, f"adm:{nav}"))
    return kb(rows, B("🔙 Back", "adm:tr"))


async def _dialogs(context, refresh=False):
    if refresh or "tr_dl" not in context.user_data:
        client = await T.session.ensure()
        context.user_data["tr_dl"] = await T.list_dialogs(client) if client else []
    return context.user_data["tr_dl"]


@action("trnew")
@owner_only
async def a_trnew(update, context, args):
    if T.busy():
        await show(update, "⚠️ A TRANSFER IS ALREADY RUNNING.", kb([[B("📊 Open", "adm:trjob")]], B("🔙 Back", "adm:tr")))
        return
    if T.clean_busy():
        await show(update, "⚠️ A DELETE JOB IS RUNNING.", kb([[B("📊 Open", "adm:trclj")]], B("🔙 Back", "adm:tr")))
        return
    if not await T.session.ensure():
        await a_tr(update, context, args)
        return
    for k in ("tr_src", "tr_dst", "tr_from"):
        context.user_data.pop(k, None)
    await show(update, "⏳ LOADING YOUR CHATS…")
    await a_trsp(update, context, ["0", "1"])


@action("trsp")
@owner_only
async def a_trsp(update, context, args):
    page = int(args[0]) if args else 0
    items = await _dialogs(context, refresh=bool(len(args) > 1))
    context.user_data["state"] = "tr_src_in"
    await show(update,
               head("📤", "Source", "STEP 1/2 ▸ TAP THE CHAT TO COPY <b>FROM</b>, OR SEND ITS LINK, @USERNAME OR ID.\n"
                                    "<i>YOUR ACCOUNT MUST BE A MEMBER.</i>"),
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
            f"⚠️ CANNOT OPEN THAT CHAT ▸ <code>{_esc(type(e).__name__)}</code>. IS YOUR ACCOUNT A MEMBER?",
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
               head("📥", "Destination", f"STEP 2/2 ▸ TAP WHERE TO COPY <b>TO</b>, OR SEND ITS LINK, @USERNAME OR ID.\n"
                                         f"FROM ▸ <b>{_esc(src)}</b>"),
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
            f"⚠️ CANNOT OPEN THAT CHAT ▸ <code>{_esc(type(e).__name__)}</code>", reply_markup=K.cancel())
        return
    title = "Saved Messages" if chat.id == T.session.me.id else T.chat_title(chat)
    await _set_chat(update, context, "tr_dst", chat.id, title)
    await _confirm(update, context)


async def _confirm(update, context):
    context.user_data.pop("state", None)
    (sid, stitle), (did, dtitle) = context.user_data["tr_src"], context.user_data["tr_dst"]
    if sid == did:
        await show(update, "⚠️ SOURCE AND DESTINATION ARE THE SAME CHAT.", K.back("trnew"))
        return
    last = await T.last_progress(sid, did)
    items = [("🚀 Start", "adm:trgo:0"), ("🔢 From ID", "adm:trfrom")]
    note = ""
    if last:
        items.insert(0, (f"⏯ Resume #{last}", f"adm:trgo:{last + 1}"))
        note = f"\n⏯ LAST RUN REACHED MESSAGE <b>#{last}</b>."
    await show(update,
               head("🔄", "Ready", f"📤 FROM ▸ <b>{_esc(stitle)}</b>\n📥 TO ▸ <b>{_esc(dtitle)}</b>\n\n"
                                   "COPIED IN ORDER WITH CAPTIONS, NO FORWARD TAG." + note),
               kb(grid(items), B("❌ Cancel", "adm:cancel")))


@action("trfrom")
@owner_only
async def a_trfrom(update, context, args):
    await ask(update, context, "tr_from", head("🔢", "Start From ID", "SEND THE MESSAGE ID TO START FROM, E.G. <code>1250</code>."))


@state("tr_from")
@owner_only
async def st_from(update, context):
    txt = (update.effective_message.text or "").strip()
    m = re.search(r"(\d+)\s*$", txt)
    if not m:
        await update.effective_chat.send_message("⚠️ SEND A NUMBER.", reply_markup=K.cancel())
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
        await show(update, "⚠️ A TRANSFER IS ALREADY RUNNING.", kb([[B("📊 Open", "adm:trjob")]]))
        return
    if T.clean_busy():
        await show(update, "⚠️ A DELETE JOB IS RUNNING.", kb([[B("📊 Open", "adm:trclj")]]))
        return
    if "tr_src" not in context.user_data or "tr_dst" not in context.user_data:
        await show(update, "⚠️ SESSION EXPIRED. START AGAIN.", K.back("tr"))
        return
    (sid, stitle), (did, dtitle) = context.user_data["tr_src"], context.user_data["tr_dst"]
    job = T.start_job(T.Job(src=sid, dst=did, src_title=stitle, dst_title=dtitle, start_id=max(1, start_id)))
    await _watch(update, context, job, _status, _kb)


@action("trjob")
@owner_only
async def a_trjob(update, context, args):
    job = T.CURRENT
    if not job:
        await show(update, "📭 NO TRANSFER YET.", K.back("tr"))
        return
    await _watch(update, context, job, _status, _kb)


def _link(src: int, mid: int) -> str:
    s = str(src)
    return f"https://t.me/c/{s[4:]}/{mid}" if s.startswith("-100") else ""


def _status(job) -> str:
    if job.paused:
        icon, label = "⏸", "PAUSED"
    else:
        icon = {"done": "✅", "stopped": "⏹", "cancelled": "❌", "error": "⚠️"}.get(job.state, "🔄")
        label = {"starting": "STARTING", "scanning": "READING SOURCE", "running": "COPYING", "done": "DONE",
                 "stopped": "STOPPED (CAN RESUME)", "cancelled": "CANCELLED", "error": "ERROR"}.get(job.state, job.state.upper())
    txt = (f"{icon} <b>{'RETRY' if job.only_items is not None else 'TRANSFER'}</b> ▸ {label}\n\n"
           f"📤 {_esc(job.src_title)}\n📥 {_esc(job.dst_title)}\n\n"
           f"<code>{_bar(job.percent)}</code> <b>{job.percent:.0f}%</b>\n")
    if job.phase == "scan" and job.running:
        txt += f"🔍 COUNTING SOURCE ▸ <b>{job.total}</b> MESSAGES SO FAR\n"
    rows = []
    for k, ic, name in T.KINDS:
        seen = job.seen.get(k, 0)
        if not seen:
            continue
        line = f"{ic} {name.upper()} ▸ <b>{job.copied_by.get(k, 0)}</b> / {seen}"
        if job.failed_by.get(k):
            line += f" · ⚠️ {job.failed_by[k]} FAILED"
        rows.append(line)
    if rows:
        txt += "\n" + "\n".join(rows) + "\n"
    txt += (f"\n📦 SOURCE ▸ <b>{job.total}</b> · ✅ COPIED ▸ <b>{job.copied}</b>\n"
            f"🚫 FAILED ▸ <b>{job.failed}</b> · ⏳ LEFT ▸ <b>{job.left}</b> · ⏭ SKIPPED ▸ {job.skipped}\n")
    if job.fail_reasons:
        txt += "❓ WHY ▸ " + ", ".join(f"{_esc(r)} ×{n}" for r, n in job.fail_reasons.most_common(3)) + "\n"
    if job.running:
        txt += f"⚡ <b>{job.speed:.1f}</b> MSG/S · ⏱ LEFT <b>{_t(job.eta)}</b> · 🎚 {T.SPEEDS[job.speed_key][0].upper()}"
        if job.flood_left and not job.paused:
            txt += f"\n⏳ TELEGRAM LIMIT, WAITING {job.flood_left}S"
    else:
        txt += f"⏱ TOOK ▸ <b>{_t(job.elapsed())}</b>"
    if job.sent_n:
        txt += f"\n🗑 {job.sent_n} COPIED MESSAGES ARE TRACKED AND CAN BE DELETED IN ONE TAP."
    if job.error:
        txt += f"\n\n⚠️ {_esc(job.error)}"
    if job.state == "done":
        txt += "\n\n🎉 ALL DONE." if not job.failed else "\n\nDONE. SOME MESSAGES COULD NOT BE COPIED."
    if job.state == "stopped":
        txt += "\n\nPROGRESS IS SAVED. START A NEW TRANSFER WITH THE SAME CHATS TO RESUME."
    return txt


def _kb(job):
    if not job.running:
        items = []
        if job.failed_items:
            items += [(f"📋 Failed ({job.failed})", "adm:trfail"), ("🔁 Retry", "adm:trretry")]
        if job.sent_n:
            items.append((f"🗑 Delete {job.sent_n} Copied", "adm:trdelj"))
        return kb(grid(items), B("🔙 Back", "adm:tr"))
    main = [("▶️ Resume", "adm:trresume") if job.paused else ("⏸ Pause", "adm:trpause"), ("⏹ Stop", "adm:trstop")]
    speed = [(("✅ " if k == job.speed_key else "") + name, f"adm:trspd:{k}") for k, (name, _d) in T.SPEEDS.items()]
    items = main + speed
    if job.failed_items:
        items.append((f"📋 Failed ({job.failed})", "adm:trfail"))
    return kb(grid(items), B("❌ Cancel", "adm:trcancel"))


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
            txt, markup = render(obj), kbf(obj)
            sig = txt + "|" + ",".join(b.callback_data for r in markup.inline_keyboard for b in r)
            if sig != last:
                try:
                    await msg.edit_text(txt, reply_markup=markup)
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
               head("❌", "Cancel Transfer", "FILES ALREADY COPIED STAY IN THE DESTINATION.\n" + hl("THE RESUME POINT IS DISCARDED.")),
               K.confirm("adm:trcancely", "adm:trjob", "✅ Yes, Cancel", "↩️ Keep Going"))


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
        await show(update, "✅ NOTHING FAILED.", K.back("trjob"))
        return
    lines = []
    for mid, kind, why in job.failed_items[:25]:
        ic = T.KIND_INFO.get(kind, ("📎", ""))[0]
        url = _link(job.src, mid)
        ref = f'<a href="{url}">#{mid}</a>' if url else f"#{mid}"
        lines.append(f"{ic} {ref} - {_esc(why)}")
    more = job.failed - min(len(job.failed_items), 25)
    why = ", ".join(f"{_esc(r)} ×{n}" for r, n in job.fail_reasons.most_common(5))
    rows = [[B("🔁 Retry", "adm:trretry")]] if (job.failed_items and not job.running) else []
    await show(update,
               head("🚫", "Failed", f"TOTAL ▸ <b>{job.failed}</b>\n{why}\n\n") + "\n".join(lines)
               + (f"\n… AND {more} MORE" if more > 0 else ""),
               kb(rows, B("🔙 Back", "adm:trjob")))


@action("trretry")
@owner_only
async def a_trretry(update, context, args):
    if T.busy() or T.clean_busy():
        await show(update, "⚠️ ANOTHER JOB IS RUNNING.", kb([[B("📊 Open", "adm:trjob")]]))
        return
    nj = T.retry_job(T.CURRENT) if T.CURRENT else None
    if not nj:
        await show(update, "📭 NOTHING TO RETRY.", K.back("tr"))
        return
    await _watch(update, context, T.start_job(nj), _status, _kb)


# ===================================================================== one-shot delete
def _cstatus(cj) -> str:
    icon = {"done": "✅", "stopped": "⏹", "error": "⚠️"}.get(cj.state, "🗑")
    label = {"starting": "STARTING", "scanning": "FINDING MESSAGES", "running": "DELETING",
             "done": "DONE", "stopped": "STOPPED", "error": "ERROR"}.get(cj.state, cj.state.upper())
    what = "EVERYTHING IN THE CHAT" if cj.mode == "all" else "FILES COPIED BY THE TRANSFER"
    txt = (f"{icon} <b>DELETE</b> ▸ {label}\n\n📥 {_esc(cj.title)}\n<i>{what}</i>\n\n"
           f"<code>{_bar(cj.percent)}</code> <b>{cj.percent:.0f}%</b>\n"
           f"🗑 DELETED ▸ <b>{cj.deleted}</b> / {cj.total}")
    if cj.missing:
        txt += f"\n👻 ALREADY GONE ▸ {cj.missing}"
    if cj.failed:
        txt += f"\n⚠️ FAILED ▸ {cj.failed}"
    if cj.error:
        txt += f"\n\n⚠️ {_esc(cj.error)}"
    if cj.state == "done":
        txt += "\n\n🎉 ALL DELETED."
    return txt


def _ckb(cj):
    return kb([], B("⏹ Stop", "adm:trclstop")) if cj.running else K.back("tr")


@action("trclj")
@owner_only
async def a_trclj(update, context, args):
    if not T.CLEAN:
        await show(update, "📭 NO DELETE JOB YET.", K.back("tr"))
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
        await show(update, "⚠️ A TRANSFER IS RUNNING. STOP OR CANCEL IT FIRST.", kb([[B("📊 Open", "adm:trjob")]]))
        return
    if T.clean_busy():
        await a_trclj(update, context, args)
        return
    if not await T.session.ensure():
        await a_tr(update, context, args)
        return
    items = await T.tracked()
    context.user_data["tr_cl"] = items
    rows = [[nm(f"🗑 {_short(d['dst_title'], 24)} · {d['n']} files", f"adm:trclsel:{i}")] for i, d in enumerate(items)]
    rows.append([B("🧹 Wipe Chat", "adm:trwp")])
    await show(update,
               head("🗑", "Cleanup", ("TAP A DESTINATION TO DELETE <b>EVERYTHING THE BOT COPIED INTO IT</b>." if items
                                      else "NOTHING COPIED BY THE BOT IS TRACKED YET.")
                    + "\n🧹 <b>WIPE CHAT</b> DELETES ALL MESSAGES OF A CHAT."),
               kb(rows, B("🔙 Back", "adm:tr")))


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
               f"COPIED BY THE BOT ▸ <b>{d['n']}</b> MESSAGES (FROM {_esc(d['src_title'])}).\n"
               "ONLY THOSE ARE DELETED. OTHER MESSAGES STAY.",
               kb(grid([(f"✅ Delete {d['n']}", f"adm:trcly:{args[0]}"), ("🧹 Wipe Chat", f"adm:trclw:{args[0]}")]),
                  B("🔙 Back", "adm:trcl")))


@action("trcly")
@owner_only
async def a_trcly(update, context, args):
    d = _tracked_item(context, args)
    if not d:
        await a_trcl(update, context, [])
        return
    if T.busy() or T.clean_busy():
        await show(update, "⚠️ ANOTHER JOB IS RUNNING.", K.back("tr"))
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
    await show(update, "📭 NOTHING TRACKED FOR THIS TRANSFER.", K.back("tr"))


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
               head("🧹", "Wipe Chat", "TAP THE CHAT TO EMPTY, OR SEND ITS LINK, @USERNAME OR ID."),
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
            f"⚠️ CANNOT OPEN THAT CHAT ▸ <code>{_esc(type(e).__name__)}</code>", reply_markup=K.cancel())
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
            warn = "\n⚠️ PRIVATE CHAT ▸ MESSAGES ARE DELETED FOR THE OTHER PERSON TOO."
    except Exception:
        pass
    await show(update,
               head("🧹", "Wipe Chat", f"<b>{_esc(title)}</b>\n\nALL MESSAGES (ABOUT <b>{n}</b>) WILL BE DELETED.\n"
                                       + hl(f"THIS CANNOT BE UNDONE.{warn}")),
               K.confirm("adm:trwpy", "adm:trcl", "🧹 Yes, Delete", "❌ No"))


@action("trwpy")
@owner_only
async def a_trwpy(update, context, args):
    tgt = context.user_data.get("tr_wp")
    if not tgt:
        await a_trcl(update, context, [])
        return
    if T.busy() or T.clean_busy():
        await show(update, "⚠️ ANOTHER JOB IS RUNNING.", K.back("tr"))
        return
    cj = T.start_clean(T.CleanJob(dst=tgt[0], title=tgt[1], mode="all"))
    context.user_data.pop("tr_wp", None)
    await _watch(update, context, cj, _cstatus, _ckb)
