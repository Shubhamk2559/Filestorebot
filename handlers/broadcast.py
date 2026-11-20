"""
handlers/broadcast.py
Broadcast with live progress. Rate-limited (~25 msgs/sec, under Telegram's ~30/sec limit),
honours RetryAfter, and flags users who blocked the bot so they are skipped next time.
"""
import asyncio
import logging
import time

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from database import db
from utils.helpers import retry_seconds

log = logging.getLogger(__name__)
BATCH = 25


async def run_broadcast(bot, from_chat: int, msg_id: int, status_chat: int, status_msg: int):
    total = await db.count_broadcastable()
    counts = {"sent": 0, "blocked": 0, "failed": 0}
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
        if not final and time.time() - last_edit < 3:
            return
        last_edit = time.time()
        pct = int(done * 100 / total) if total else 100
        title = "✅ <b>Broadcast finished</b>" if final else "📢 <b>Broadcasting…</b>"
        text = (f"{title}\n\n👥 Progress: {done}/{total} ({pct}%)\n"
                f"✅ Sent: {counts['sent']}\n🚫 Blocked/unreachable: {counts['blocked']}\n"
                f"⚠️ Failed: {counts['failed']}")
        try:
            await bot.edit_message_text(text, chat_id=status_chat, message_id=status_msg)
        except RetryAfter:
            pass
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
