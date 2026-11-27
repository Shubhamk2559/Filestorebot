"""
handlers/localbackup.py - panel screens of Local Database Backup and Recover (owner only).

  Panel -> 💾 Backup -> 💽 Database Backup (Local)    scan -> confirm -> parts are built, sent to you, deleted, one by one
  Panel -> 💾 Backup -> ♻️ Recover From Zip            (also: Settings -> 📡 DB Channel -> ♻️ Recover)
        pick parts from the VPS restore folder, or upload them on the protected web page
Engines: utils/lbbackup.py, utils/lbrestore.py. Web pages: utils/lbweb.py.
"""
import asyncio
import logging
import re

import config
from database.db import settings as S
from handlers.admin import action, ask, bot_admin_status, show, state, warn
from telegram.error import TelegramError
from utils import keyboards as K
from utils import lbcore as C
from utils import lbpaths, lbweb
from utils.helpers import owner_only
from utils.keyboards import grid, kb, nm, ub
from utils.lbbackup import disk_free, part_limit, runner
from utils.lbrestore import analyze, restorer
from utils.ui import esc, head, hl

log = logging.getLogger(__name__)
_bot = None


def setup(bot):
    """Called once at start-up: lets the web page tell the owner that an upload finished."""
    global _bot
    _bot = bot
    lbweb.hooks["up_done"] = _uploaded


async def _uploaded(name: str, size: int):
    if _bot:
        try:
            await _bot.send_message(config.OWNER_ID, head("📥", "Upload Finished", f"<code>{esc(name)}</code> · {C.human(size)}"),
                                    reply_markup=kb([[ub("♻️ Open Recover", "adm:lrdir")]]))
        except TelegramError:
            pass


def _busy() -> str:
    return "BACKUP" if runner.running else ("RECOVER" if restorer.running else "")


def _mtproto_problem() -> str:
    if not config.API_ID or not config.API_HASH:
        return "API_ID / API_HASH ARE MISSING IN <code>.env</code>."
    return ""


# ===================================================================== backup
@action("lb")
@owner_only
async def a_lb(update, context, args):
    q = update.callback_query
    j = runner.job
    if runner.running and j:
        runner.attach(q.message.chat_id, q.message.message_id)
        text, markup = runner.render()
        await show(update, text, markup)
        return
    if restorer.running:
        await show(update, head("💽", "Database Backup", hl("A RECOVER IS RUNNING. WAIT FOR IT TO FINISH.")), K.back("bk"))
        return
    limit = part_limit()
    size_row = [ub(f"📦 Part Size: {C.human(limit)}", "adm:lbsize")]
    st = (j or {}).get("status")
    if st == "ready":
        free = disk_free()
        warn_txt = ""
        if free < 2 * limit:
            warn_txt = "\n\n" + hl(f"⚠️ ONLY <b>{C.human(free)}</b> FREE ON THE VPS. 2 × PART SIZE ({C.human(2 * limit)}) IS SAFER.")
        text = head("💽", "Backup Ready",
                    f"FILES ▸ <b>{j['total_files']:,}</b> · SIZE ▸ <b>{C.human(j['total_bytes'])}</b>\n"
                    f"PARTS ▸ <b>{j['total_parts']}</b> × {C.human(j['limit'])}\n"
                    f"FREE DISK ▸ <b>{C.human(free)}</b>"
                    + (f"\n⚠️ NOT BACKED UP ▸ <b>{j['skipped_n']}</b> (DELETED / NOT FILES)" if j.get("skipped_n") else "")
                    + warn_txt + "\n\nEACH PART IS SENT TO YOU AND DELETED FROM THE VPS BEFORE THE NEXT ONE STARTS.")
        markup = kb([[ub("▶️ Start Backup" if free >= 2 * limit else "▶️ Start Anyway", "adm:lbgo")], [ub("❌ Discard", "adm:lbnewyes")]],
                    ub("🔙 Back", "adm:bk"))
    elif st in ("stopped", "failed") and j:
        text = head("💽", "Backup Paused" if st == "stopped" else "Backup Stopped",
                    f"PARTS DELIVERED ▸ <b>{len(j['parts'])}/{j['total_parts']}</b>\n"
                    f"NEXT ▸ PART <b>{min(j['cur_part'], j['total_parts'])}</b>"
                    + (f"\n<code>{esc(j['error'])}</code>" if j.get("error") else ""))
        markup = kb([[ub("▶️ Resume Backup", "adm:lbresume")], [ub("🆕 New Backup", "adm:lbnew")], size_row], ub("🔙 Back", "adm:bk"))
    elif st == "done" and j:
        total = sum(p["size"] for p in j["parts"])
        text = head("✅", "Last Backup", f"PARTS ▸ <b>{len(j['parts'])}</b> · SIZE ▸ <b>{C.human(total)}</b>\n"
                    f"FILES ▸ <b>{j['total_files']:,}</b>")
        markup = kb([[ub("🆕 New Backup", "adm:lbscan")], size_row], ub("🔙 Back", "adm:bk"))
    else:
        text = head("💽", "Database Backup (Local)",
                    "PACKS EVERY FILE OF THE DATABASE CHANNEL INTO ZIP PARTS WITH CAPTIONS, LINKS, ORDER AND A FULL DATABASE EXPORT.\n"
                    "PARTS ARE SENT TO YOU ONE BY ONE AND DELETED FROM THE VPS AFTER DELIVERY.\n\n"
                    f"PART SIZE ▸ <b>{C.human(limit)}</b> · FREE DISK ▸ <b>{C.human(disk_free())}</b>")
        markup = kb([[ub("▶️ Start Backup", "adm:lbscan")], size_row], ub("🔙 Back", "adm:bk"))
    await show(update, text, markup)


@action("lbsize")
@owner_only
async def a_lbsize(update, context, args):
    await ask(update, context, "lb_size",
              head("📦", "Part Size", f"SEND THE SIZE OF ONE ZIP PART, E.G. <code>1.5</code> (GB) OR <code>800MB</code>.\n"
                   f"MIN {C.MIN_PART_MB} MB · MAX {C.MAX_PART_MB / 1024:.2f} GB (TELEGRAM UPLOAD LIMIT).\n"
                   f"CURRENT ▸ <b>{C.human(part_limit())}</b>"))


@state("lb_size")
async def st_lbsize(update, context):
    text = (update.effective_message.text or "").strip().lower().replace(",", ".")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(gb|g|mb|m)?", text)
    if not m:
        await warn(update, "SEND A NUMBER, E.G. <code>1.5</code> OR <code>800MB</code>.")
        return
    val, unit = float(m.group(1)), m.group(2)
    mb = val * 1024 if (unit in ("gb", "g") or (unit is None and val <= 10)) else val
    if not C.MIN_PART_MB <= mb <= C.MAX_PART_MB:
        await warn(update, f"SIZE MUST BE BETWEEN {C.MIN_PART_MB} MB AND {C.MAX_PART_MB / 1024:.2f} GB.")
        return
    await S.set("lb_part_mb", int(mb))
    context.user_data.pop("state", None)
    await update.effective_message.reply_text(f"✅ PART SIZE ▸ <b>{C.human(int(mb) * C.MiB)}</b>", reply_markup=K.back("lb"))


@action("lbscan")
@owner_only
async def a_lbscan(update, context, args):
    q = update.callback_query
    if _busy():
        await show(update, f"⚠️ {_busy()} IS RUNNING.", K.back("lb"))
        return
    if not S.get("db_channel"):
        await show(update, "⚠️ SET THE DATABASE CHANNEL FIRST.", kb([[ub("📡 DB Channel", "adm:dbch")]], ub("🔙 Back", "adm:lb")))
        return
    if _mtproto_problem():
        await show(update, head("⚠️", "MTProto Needed", _mtproto_problem()), K.back("lb"))
        return
    await runner.start_scan(context.bot, q.message.chat_id, q.message.message_id)
    text, markup = runner.render()
    await show(update, text, markup)


@action("lbgo")
@owner_only
async def a_lbgo(update, context, args):
    q = update.callback_query
    if runner.running or not runner.job or runner.job.get("status") != "ready":
        await a_lb(update, context, args)
        return
    runner.attach(q.message.chat_id, q.message.message_id)
    await runner.start(context.bot)
    await asyncio.sleep(0.3)
    text, markup = runner.render()
    await show(update, text, markup)


@action("lbresume")
@owner_only
async def a_lbresume(update, context, args):
    q = update.callback_query
    if _busy() or not runner.job or runner.job.get("status") not in ("stopped", "failed"):
        await a_lb(update, context, args)
        return
    runner.attach(q.message.chat_id, q.message.message_id)
    await runner.start(context.bot)
    await asyncio.sleep(0.3)
    text, markup = runner.render()
    await show(update, text, markup)


@action("lbstop")
@owner_only
async def a_lbstop(update, context, args):
    runner.request_stop()
    await asyncio.sleep(0.3)
    text, markup = runner.render()
    await show(update, text, markup)


@action("lbretry")
@owner_only
async def a_lbretry(update, context, args):
    runner.decide("retry")
    await asyncio.sleep(0.3)
    text, markup = runner.render()
    await show(update, text, markup)


@action("lbdone")
@owner_only
async def a_lbdone(update, context, args):
    runner.decide("confirm")
    await asyncio.sleep(0.3)
    text, markup = runner.render()
    await show(update, text, markup)


@action("lbnew")
@owner_only
async def a_lbnew(update, context, args):
    await show(update, head("🆕", "New Backup", hl("THE UNFINISHED BACKUP IS FORGOTTEN. PARTS YOU ALREADY RECEIVED STAY VALID.")),
               K.confirm("adm:lbnewyes", "adm:lb", "✅ Yes, New", "❌ No"))


@action("lbnewyes")
@owner_only
async def a_lbnewyes(update, context, args):
    if not await runner.discard():
        await show(update, "⚠️ A BACKUP IS RUNNING. STOP IT FIRST.", K.back("lb"))
        return
    await a_lb(update, context, args)


# ===================================================================== recover
def _sel(context, info) -> set:
    nums = {r["manifest"]["part"] for r in info["parts"]}
    cur = context.user_data.get("lr_sel")
    if cur is None:
        cur = context.user_data["lr_sel"] = set(nums)
    cur &= nums
    return cur


@action("lr")
@owner_only
async def a_lr(update, context, args):
    q = update.callback_query
    if restorer.running:
        restorer.attach(q.message.chat_id, q.message.message_id)
        text, markup = restorer.render()
        await show(update, text, markup)
        return
    chan = S.get("db_channel")
    if not chan:
        await show(update, head("♻️", "Recover From Zip", hl("⚠️ CREATE A NEW CHANNEL, ADD THE BOT AS ADMIN, THEN SET IT AS DATABASE CHANNEL FIRST.")),
                   kb([[ub("📡 DB Channel", "adm:dbch")]], ub("🔙 Back", "adm:bk")))
        return
    ok, why = True, ""
    try:
        ok, why = await bot_admin_status(context.bot, chan["id"], True)
    except TelegramError as e:
        ok, why = False, f"CANNOT ACCESS THE CHANNEL ({esc(e)})."
    text = head("♻️", "Recover From Zip",
                f"DATABASE CHANNEL ▸ <b>{esc(chan.get('title') or chan['id'])}</b>\n"
                f"RESTORE FOLDER ▸ <code>{esc(lbpaths.restore_dir())}</code>\n\n"
                "PUT THE ZIP PARTS IN THE FOLDER, OR UPLOAD THEM ON THE WEB PAGE. EVERY FILE COMES BACK WITH ITS CAPTION, "
                "ORDER AND OLD SHARE LINK.")
    if not ok:
        text += "\n\n" + hl(f"⚠️ {why}")
    j = restorer.job
    if j and j.get("status") in ("stopped", "failed") and j.get("channel", {}).get("id") == chan["id"]:
        text += "\n\nAN UNFINISHED RECOVER FOR THIS CHANNEL EXISTS. OPEN THE LIST AND START AGAIN TO CONTINUE."
    markup = kb([[ub("📁 Pick From Folder", "adm:lrdir")], [ub("🌐 Upload Page", "adm:lrup")]], ub("🔙 Back", "adm:bk"))
    await show(update, text, markup)


@action("lrdir")
@owner_only
async def a_lrdir(update, context, args):
    if restorer.running:
        await a_lr(update, context, args)
        return
    info = await asyncio.to_thread(analyze, lbpaths.restore_dir())
    if not info["parts"]:
        extra = ""
        if info["unreadable"]:
            extra = "\n" + "\n".join(f"⚠️ <code>{esc(r['name'])}</code> — {esc(r['error'])}" for r in info["unreadable"][:5])
        await show(update, head("📁", "Restore Folder", f"NO BACKUP PARTS FOUND IN <code>{esc(lbpaths.restore_dir())}</code>.{extra}"),
                   kb([[ub("🔄 Refresh", "adm:lrdir")], [ub("🌐 Upload Page", "adm:lrup")]], ub("🔙 Back", "adm:lr")))
        return
    sel = _sel(context, info)
    parts = info["parts"]
    total = parts[0]["manifest"].get("total_parts", len(parts))
    lines = [f"SET ▸ <code>{esc(info['set_id'])}</code>",
             f"PARTS FOUND ▸ <b>{len(parts)}/{total}</b> · SELECTED ▸ <b>{len(sel)}</b>",
             f"FILES ▸ <b>{sum(len(r['manifest']['files']) for r in parts):,}</b>"]
    if not info["index"]:
        lines.append("ℹ️ <code>dbbackup_index.json</code> NOT FOUND (PART NUMBERS ARE USED TO FIND GAPS).")
    if info["other_sets"]:
        lines.append(f"ℹ️ PARTS OF {len(info['other_sets'])} OLDER SET(S) IGNORED.")
    text = head("📁", "Restore Folder", "\n".join(lines))
    if info["missing"]:
        text += "\n\n" + hl("⚠️ MISSING PARTS ▸ " + ", ".join(f"<b>{n:03d}</b>" for n in info["missing"][:30]))
    if info["unreadable"]:
        text += "\n\n" + "\n".join(f"⚠️ <code>{esc(r['name'])}</code> — {esc(r['error'])}" for r in info["unreadable"][:5])
    btns = [nm(f"{'✅' if r['manifest']['part'] in sel else '⬜'} part{r['manifest']['part']:03d} · {C.human(r['size'])}",
               f"adm:lrsel:{r['manifest']['part']}") for r in parts[:40]]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([ub("☑️ Select All / None", "adm:lrall")])
    rows.append([ub("▶️ Start Recover", "adm:lrgo"), ub("🔄 Refresh", "adm:lrdir")])
    await show(update, text, kb(rows, ub("🔙 Back", "adm:lr")))


@action("lrsel")
@owner_only
async def a_lrsel(update, context, args):
    info = await asyncio.to_thread(analyze, lbpaths.restore_dir())
    sel = _sel(context, info)
    if args and args[0].isdigit():
        n = int(args[0])
        sel.symmetric_difference_update({n})
    await a_lrdir(update, context, [])


@action("lrall")
@owner_only
async def a_lrall(update, context, args):
    info = await asyncio.to_thread(analyze, lbpaths.restore_dir())
    sel = _sel(context, info)
    nums = {r["manifest"]["part"] for r in info["parts"]}
    sel.clear() if sel == nums else sel.update(nums)
    await a_lrdir(update, context, [])


@action("lrup")
@owner_only
async def a_lrup(update, context, args):
    tok = await lbweb.create("up", lbweb.UP_TTL)
    url = lbweb.url(f"/bkup/{tok}")
    if not url:
        await lbweb.drop(tok)
        await show(update, head("⚠️", "Upload Page", "BASE_URL IS NOT A PUBLIC HTTPS ADDRESS, SO THE PAGE CANNOT BE SERVED.\n"
                                f"COPY THE PARTS INTO <code>{esc(lbpaths.restore_dir())}</code> INSTEAD."), K.back("lr"))
        return
    await show(update, head("🌐", "Upload Page", f"<a href=\"{url}\">{esc(url)}</a>\n\n"
                            "UPLOADS ARE CHUNKED AND RESUME AFTER A DROPPED CONNECTION. KEEP THE TAB OPEN.\n"
                            "THE LINK WORKS FOR 12 HOURS. WHEN DONE, TAP <b>REFRESH</b>.\n\n" +
                            hl("ONLY YOU HAVE THIS LINK. DO NOT SHARE IT.")),
               kb([[K.link("🌐 Open Page", url)], [ub("🔄 Refresh", "adm:lrdir")]], ub("🔙 Back", "adm:lr")))


@action("lrgo")
@owner_only
async def a_lrgo(update, context, args):
    q = update.callback_query
    if _busy():
        await show(update, f"⚠️ {_busy()} IS RUNNING.", K.back("lr"))
        return
    if _mtproto_problem():
        await show(update, head("⚠️", "MTProto Needed", _mtproto_problem()), K.back("lr"))
        return
    info = await asyncio.to_thread(analyze, lbpaths.restore_dir())
    sel = _sel(context, info)
    names = [r["name"] for r in info["parts"] if r["manifest"]["part"] in sel]
    if not names:
        await show(update, "⚠️ SELECT AT LEAST ONE PART.", K.back("lrdir"))
        return
    if info["missing"] and args != ["1"]:
        await show(update, head("⚠️", "Parts Missing", "MISSING ▸ " + ", ".join(f"<b>{n:03d}</b>" for n in info["missing"][:30]) +
                                "\n\n" + hl("YOU CAN START NOW AND RUN RECOVER AGAIN WHEN THE REST ARRIVES. DONE FILES ARE SKIPPED.")),
                   kb([[ub("▶️ Start Anyway", "adm:lrgo:1")]], ub("🔙 Back", "adm:lrdir")))
        return
    try:
        await restorer.start(context.bot, q.message.chat_id, q.message.message_id, names)
    except RuntimeError as e:
        await show(update, f"⚠️ {esc(e).upper()}", K.back("lr"))
        return
    context.user_data.pop("lr_sel", None)
    await asyncio.sleep(0.3)
    text, markup = restorer.render()
    await show(update, text, markup)


@action("lrstop")
@owner_only
async def a_lrstop(update, context, args):
    restorer.request_stop()
    await asyncio.sleep(0.3)
    text, markup = restorer.render()
    await show(update, text, markup)
