"""
utils/scheduler.py
Background loop that deletes delivered files after the auto-delete delay.
Pending deletions live in MongoDB, so they survive restarts.
"""
import asyncio
import logging

from telegram.error import RetryAfter, TelegramError

from database import db
from utils.helpers import retry_seconds

log = logging.getLogger(__name__)


async def autodelete_loop(bot):
    while True:
        try:
            for doc in await db.due_deletes(100):
                try:
                    await bot.delete_messages(doc["chat_id"], doc["ids"])
                except RetryAfter as e:
                    await asyncio.sleep(retry_seconds(e) + 1)
                    break  # try again on the next pass; do not drop the job
                except TelegramError as e:  # already deleted / user blocked -> drop job
                    log.debug("Auto-delete skipped: %s", e)
                await db.remove_pending(doc["_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Auto-delete loop error")
        await asyncio.sleep(15)
