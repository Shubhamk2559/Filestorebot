"""
handlers/broadcast.py
Broadcast with live progress (edits one status message; `live` also feeds the web panel). Rate-limited (~25 msgs/sec, under Telegram's ~30/sec limit),
honours RetryAfter, and flags users who blocked the bot so they are skipped next time.
"""
import asyncio
import logging
import time

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from database import db
from utils import keyboards as K
from utils.helpers import retry_seconds
from utils.ui import bar, head

log = logging.getLogger(__name__)
BATCH = 25


async def run_broadcast(bot, from_chat: int, msg_id: int, status_chat: int, status_msg: int, live: dict = None):
    total = await db.count_broadcastable()
    counts = {"sent": 0, "blocked": 0, "failed": 0}
    if live is not None:
        live["total"] = total
    done = 0
    last_edit = 0.0

    async def send(uid: int):
        for _ in range(3):
            try:
                await bot.copy_message(uid, from_chat, msg_id)
                counts["sent"] += 1
                return
            except RetryAfter as e:
                await asyncio.sleep(retry_seconds(e) + 1)
            except Forbidden as e:
                # user blocked the bot / deleted account -> skip them next time
                counts["blocked"] += 1
                await db.set_blocked(uid, True)
                log.debug("Broadcast skip %s: %s", uid, e)
                return
            except BadRequest as e:
                if any(w in str(e).lower() for w in ("chat not found", "deactivated", "user is deleted")):
                    counts["blocked"] += 1
                    await db.set_blocked(uid, True)
                else:                         # e.g. a message problem: do NOT flag the user as blocked
                    counts["failed"] += 1
                log.debug("Broadcast skip %s: %s", uid, e)
                return
            except TelegramError as e:
                log.warning("Broadcast error for %s: %s", uid, e)
                counts["failed"] += 1
                return
        counts["failed"] += 1

    async def progress(final=False):
        nonlocal last_edit
        if live is not None:
            live.update(done=done, **counts)
        if not final and time.time() - last_edit < 3:
            return
        last_edit = time.time()
        pct = int(done * 100 / total) if total else 100
        text = head("✅" if final else "📢", "Broadcast Done" if final else "Broadcasting",
                    f"<code>{bar(pct)}</code> <b>{pct}%</b>\n\n"
                    f"👥 DONE ▸ <b>{done}/{total}</b>\n"
                    f"✅ SENT ▸ <b>{counts['sent']}</b>\n"
                    f"🚫 BLOCKED ▸ <b>{counts['blocked']}</b>\n"
                    f"⚠️ FAILED ▸ <b>{counts['failed']}</b>")
        try:
            await bot.edit_message_text(text, chat_id=status_chat, message_id=status_msg,
                                        reply_markup=K.back() if final else None)
        except TelegramError:
            pass

    batch = []
    async for uid in db.iter_broadcast_ids():
        batch.append(uid)
        if len(batch) >= BATCH:
            await asyncio.gather(*(send(u) for u in batch))
            done += len(batch)
            batch = []
            await asyncio.sleep(1)
            await progress()
    if batch:
        await asyncio.gather(*(send(u) for u in batch))
        done += len(batch)
    await progress(final=True)
