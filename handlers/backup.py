"""
handlers/backup.py
Backup / Restore screen of the admin panel (owner only) + the receiver for backup files.

  Panel -> 💾 Backup
      📥 Backup Now       sends the backup file(s) to this chat
      🔁 Auto-Backup      one file every 24 h to the owner's chat
      📤 Restore          send a backup file -> Replace everything / Merge

A backup file sent to the bot by the owner is recognised by its name (fsb-backup-*.json.gz) at any time,
so on a brand-new bot it is enough to send the file.
"""
import asyncio
import logging

from telegram.ext import filters

import config
from database.db import settings as S
from handlers.admin import action, ask, show, state, warn
from utils import backup as BK
from utils import keyboards as K
from utils.helpers import owner_only
from utils.keyboards import grid, kb, ub
from utils.ui import ago, esc, head, hl, onoff

log = logging.getLogger(__name__)
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
_pending: dict = {}      # owner id -> {"id": backup id, "of": parts expected, "parts": {n: decoded part}}


class BackupDocFilter(filters.MessageFilter):
    """Documents named fsb-backup-*.json.gz that come from the owner."""

    def filter(self, message) -> bool:
        d, u = message.document, message.from_user
        return bool(d and u and u.id == config.OWNER_ID and BK.is_backup_name(d.file_name))


def _parts(cur):
    return [cur["parts"][k] for k in sorted(cur["parts"])]


# ===================================================================== main screen
@action("bk")
@owner_only
async def a_bk(update, context, args):
    on = S.get("auto_backup")
    await show(update,
               head("💾", "Backup", "SETTINGS, LINKS, FILES, USERS AND AUTOPOSTER IN ONE FILE.\n\n"
                                    f"LAST BACKUP ▸ <b>{ago(S.get('bk_last'))}</b>\n"
                                    f"AUTO-BACKUP ▸ <b>{onoff(on)}</b> (EVERY 24 H)"),
               kb(grid([("📥 Backup Now", "adm:bknow"), ("📤 Restore", "adm:bkrestore"),
                        (f"🔁 Auto-Backup: {onoff(on)}", "adm:bkauto")])
                  + [[ub("💽 Database Backup (Local)", "adm:lb")], [ub("♻️ Recover From Zip", "adm:lr")]],
                  ub("🔙 Back", "adm:home")))


@action("bkauto")
@owner_only
async def a_bkauto(update, context, args):
    await S.set("auto_backup", not S.get("auto_backup"))
    await a_bk(update, context, args)


@action("bknow")
@owner_only
async def a_bknow(update, context, args):
    await show(update, "⏳ CREATING BACKUP…")
    try:
        counts = await BK.send_backup(context.bot, update.effective_chat.id)
    except Exception as e:
        log.exception("Backup failed")
        await show(update, f"⚠️ BACKUP FAILED ▸ <code>{esc(e)}</code>", K.back("bk"))
        return
    await show(update,
               head("✅", "Backup Sent", f"👥 <b>{counts['users']}</b> USERS · 🔗 <b>{counts['links']}</b> LINKS · "
                                         f"📁 <b>{counts['files']}</b> FILES\n\n" + hl("KEEP THE FILE SAFE.")),
               K.back("bk"))


@action("bkrestore")
@owner_only
async def a_bkrestore(update, context, args):
    _pending.pop(update.effective_user.id, None)
    await ask(update, context, "bk_wait",
              head("📤", "Restore", "SEND THE BACKUP FILE (<code>fsb-backup-….json.gz</code>).\n"
                                    "IF IT CAME IN PARTS, SEND ALL OF THEM."))


# ===================================================================== receiving the file
async def ingest(update, context):
    """A backup file (or part) arrived from the owner."""
    msg, uid = update.effective_message, update.effective_user.id
    doc = msg.document
    if not doc:
        await warn(update, "SEND THE BACKUP FILE.")
        return
    context.user_data.pop("state", None)
    if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text("⚠️ FILE IS OVER 20 MB. THE BOT API CANNOT DOWNLOAD IT.", reply_markup=K.back("bk"))
        return
    status = await msg.reply_text("⏳ READING BACKUP…")
    try:
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        part = await asyncio.to_thread(BK.parse_part, data)
    except ValueError as e:
        await status.edit_text(f"⚠️ {esc(e).upper()}.", reply_markup=K.back("bk"))
        return
    except Exception as e:
        log.exception("Backup file could not be read")
        await status.edit_text(f"⚠️ COULD NOT READ THE FILE ▸ <code>{esc(e)}</code>", reply_markup=K.back("bk"))
        return

    head_, tail = part["head"], part["tail"]
    cur = _pending.get(uid)
    if not cur or cur["id"] != head_["id"]:
        cur = _pending[uid] = {"id": head_["id"], "of": int(tail.get("of", 1)), "parts": {}}
    cur["parts"][int(tail.get("part", 1))] = part

    if len(cur["parts"]) < cur["of"]:
        await status.edit_text(f"📦 PART <b>{len(cur['parts'])}/{cur['of']}</b> RECEIVED. SEND THE REST.",
                               reply_markup=K.cancel())
        return

    d = BK.summarize(_parts(cur))["docs"]
    await status.edit_text(
        head("💾", "Backup Ready",
             f"📅 {esc(head_.get('created_str', '?'))} · @{esc(head_.get('bot') or '?')}\n"
             f"👥 <b>{len(d['users'])}</b> USERS · 🔗 <b>{len(d['links'])}</b> LINKS · 📁 <b>{len(d['files'])}</b> FILES\n"
             f"📅 AUTOPOSTER ▸ <b>{len(d['ap_channels'])}</b> CHANNELS · <b>{len(d['ap_posts'])}</b> POSTS\n\n"
             "<b>MERGE</b> ▸ ADDS ONLY WHAT IS MISSING.\n\n"
             + hl("<b>REPLACE</b> ▸ DATABASE BECOMES THIS BACKUP. CURRENT DATA IS OVERWRITTEN.")),
        reply_markup=kb(grid([("♻️ Replace", "adm:bkgo:full"), ("➕ Merge", "adm:bkgo:merge")]),
                        ub("❌ Cancel", "adm:bkx")))


state("bk_wait")(ingest)


@action("bkx")
@owner_only
async def a_bkx(update, context, args):
    _pending.pop(update.effective_user.id, None)
    await a_bk(update, context, args)


@action("bkgo")
@owner_only
async def a_bkgo(update, context, args):
    mode = args[0] if args and args[0] in ("full", "merge") else "merge"
    cur = _pending.pop(update.effective_user.id, None)
    if not cur:
        await show(update, "⚠️ BACKUP EXPIRED. SEND THE FILE AGAIN.", K.back("bk"))
        return
    await show(update, "⏳ RESTORING…")
    try:
        report = await BK.restore(context.bot, update.effective_chat.id, _parts(cur), mode)
    except Exception as e:
        log.exception("Restore failed")
        await show(update, f"⚠️ RESTORE FAILED ▸ <code>{esc(e)}</code>", K.back("bk"))
        return
    await show(update, report, K.back("bk"))
