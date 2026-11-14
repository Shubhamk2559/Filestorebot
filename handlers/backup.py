"""
handlers/backup.py
Backup / Restore screen of the admin panel (owner only) + the receiver for backup files.

  Panel -> 💾 Backup / Restore
      📥 Backup now         sends the backup file(s) to this chat
      🔁 Auto-backup        one file every 24 h to the owner's chat
      📤 Restore            send a backup file -> choose  Replace everything / Merge

A backup file sent to the bot by the owner is recognised by its name (fsb-backup-*.json.gz) at any time,
so on a brand-new bot it is enough to send the file.
"""
import asyncio
import html
import logging
import time

from telegram import InlineKeyboardMarkup as M
from telegram.ext import filters

import config
from database.db import settings as S
from handlers.admin import B, action, ask, show, state
from utils import backup as BK
from utils import keyboards as K
from utils.helpers import owner_only

log = logging.getLogger(__name__)
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
_pending: dict = {}      # owner id -> {"id": backup id, "of": parts expected, "parts": {n: decoded part}}


class BackupDocFilter(filters.MessageFilter):
    """Documents named fsb-backup-*.json.gz that come from the owner."""

    def filter(self, message) -> bool:
        d, u = message.document, message.from_user
        return bool(d and u and u.id == config.OWNER_ID and BK.is_backup_name(d.file_name))


def _ago(ts) -> str:
    if not ts:
        return "never"
    d = int(time.time() - ts)
    return f"{d // 60} min ago" if d < 5400 else f"{d // 3600} h ago" if d < 172800 else f"{d // 86400} days ago"


# ===================================================================== main screen
@action("bk")
@owner_only
async def a_bk(update, context, args):
    on = S.get("auto_backup")
    await show(
        update,
        "💾 <b>Backup / Restore</b>\n\n"
        "Settings, links, files, users and Autoposter in one file.\n\n"
        f"Last backup: <b>{_ago(S.get('bk_last'))}</b>\n"
        f"Auto-backup (every 24 h): <b>{'ON' if on else 'OFF'}</b>\n\n"
        "To restore, send a backup file to this chat.",
        M([
            [B("📥 Backup now", "adm:bknow")],
            [B(f"🔁 Auto-backup: {'ON' if on else 'OFF'}", "adm:bkauto")],
            [B("📤 Restore", "adm:bkrestore")],
            [B("🔙 Back", "adm:home")],
        ]),
    )


@action("bkauto")
@owner_only
async def a_bkauto(update, context, args):
    await S.set("auto_backup", not S.get("auto_backup"))
    await a_bk(update, context, args)


@action("bknow")
@owner_only
async def a_bknow(update, context, args):
    await show(update, "⏳ Creating backup…")
    try:
        counts = await BK.send_backup(context.bot, update.effective_chat.id)
    except Exception as e:
        log.exception("Backup failed")
        await show(update, f"⚠️ Backup failed: <code>{html.escape(str(e))}</code>", K.back("bk"))
        return
    await show(
        update,
        "✅ <b>Backup sent</b>\n\n"
        f"👥 {counts['users']} users · 🔗 {counts['links']} links · 📁 {counts['files']} files\n\n"
        "Keep the file safe (forward it to Saved Messages).",
        K.back("bk"),
    )


@action("bkrestore")
@owner_only
async def a_bkrestore(update, context, args):
    _pending.pop(update.effective_user.id, None)
    await ask(update, context, "bk_wait",
              "📤 <b>Restore</b>\n\nSend the backup file (<code>fsb-backup-….json.gz</code>).\n"
              "If it came in several parts, send all of them.")


# ===================================================================== receiving the file
async def ingest(update, context):
    """A backup file (or part) arrived from the owner."""
    msg, uid = update.effective_message, update.effective_user.id
    doc = msg.document
    if not doc:
        await msg.reply_text("⚠️ Please send the backup file.", reply_markup=K.cancel())
        return
    context.user_data.pop("state", None)
    if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text("⚠️ File is larger than 20 MB - the Bot API cannot download it.")
        return
    status = await msg.reply_text("⏳ Reading backup…")
    try:
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        part = await asyncio.to_thread(BK.parse_part, data)
    except ValueError as e:
        await status.edit_text(f"⚠️ {html.escape(str(e)).capitalize()}.")
        return
    except Exception as e:
        log.exception("Backup file could not be read")
        await status.edit_text(f"⚠️ Could not read the file: <code>{html.escape(str(e))}</code>")
        return

    head, tail = part["head"], part["tail"]
    cur = _pending.get(uid)
    if not cur or cur["id"] != head["id"]:
        cur = _pending[uid] = {"id": head["id"], "of": int(tail.get("of", 1)), "parts": {}}
    cur["parts"][int(tail.get("part", 1))] = part

    if len(cur["parts"]) < cur["of"]:
        await status.edit_text(f"📦 Part {len(cur['parts'])} of {cur['of']} received. Send the remaining part(s).",
                               reply_markup=K.cancel())
        return

    info = BK.summarize([cur["parts"][k] for k in sorted(cur["parts"])])
    d = info["docs"]
    await status.edit_text(
        "💾 <b>Backup ready</b>\n\n"
        f"📅 {html.escape(head.get('created_str', '?'))} · @{html.escape(head.get('bot') or '?')}\n"
        f"👥 {len(d['users'])} users · 🔗 {len(d['links'])} links · 📁 {len(d['files'])} files\n"
        f"📅 {len(d['ap_channels'])} Autoposter channel(s), {len(d['ap_posts'])} post(s)\n\n"
        "<b>Replace everything</b> - the database becomes exactly this backup.\n"
        "<b>Merge</b> - adds only what is missing, existing data stays.",
        reply_markup=M([
            [B("♻️ Replace everything", "adm:bkgo:full")],
            [B("➕ Merge", "adm:bkgo:merge")],
            [B("❌ Cancel", "adm:bkx")],
        ]),
    )


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
        await show(update, "⚠️ Backup expired. Send the file again.", K.back("bk"))
        return
    await show(update, "⏳ Restoring…")
    try:
        report = await BK.restore(context.bot, update.effective_chat.id,
                                  [cur["parts"][k] for k in sorted(cur["parts"])], mode)
    except Exception as e:
        log.exception("Restore failed")
        await show(update, f"⚠️ Restore failed: <code>{html.escape(str(e))}</code>", K.back("bk"))
        return
    await show(update, report, K.back())
