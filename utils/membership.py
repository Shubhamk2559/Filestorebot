"""
utils/membership.py
Force-subscribe check for MANY channels using get_chat_member with a small cache.
 * Only positive results are cached (120 s) so the "I've Joined" button re-checks instantly.
 * If the bot lost admin rights / a channel was deleted, users are NOT blocked;
   the owner gets a throttled warning instead.
"""
import asyncio
import logging
import time

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from database.db import settings as S
from utils.helpers import notify_owner, retry_seconds

log = logging.getLogger(__name__)
CACHE_TTL = 120
_cache: dict = {}  # (user_id, chat_id) -> expiry timestamp


async def _is_member(bot, user_id: int, ch: dict) -> bool:
    key = (user_id, ch["id"])
    now = time.time()
    if _cache.get(key, 0) > now:
        return True
    for _ in range(2):
        try:
            m = await bot.get_chat_member(ch["id"], user_id)
            ok = m.status in ("member", "administrator", "creator") or (
                m.status == "restricted" and getattr(m, "is_member", False))
            if ok:
                if len(_cache) > 50000:  # keep memory bounded
                    _cache.clear()
                _cache[key] = now + CACHE_TTL
            return ok
        except RetryAfter as e:
            await asyncio.sleep(retry_seconds(e) + 1)
        except (BadRequest, Forbidden) as e:
            text = str(e).lower()
            if "user not found" in text or "participant_id_invalid" in text:
                return False
            # Bot not admin / channel removed -> don't lock users out, warn the owner.
            log.warning("Force-sub check failed for %s: %s", ch["id"], e)
            await notify_owner(
                bot, f"fsub{ch['id']}",
                f"⚠️ Force-sub problem with \"{ch.get('title')}\" ({ch['id']}): {e}\n"
                "Make sure the bot is an admin there, or remove the channel from the panel. "
                "Users are NOT blocked meanwhile.")
            return True
        except TelegramError as e:
            log.warning("Membership check error: %s", e)
            return True
    return True


async def get_missing(bot, user_id: int) -> list:
    """Return the force-sub channels the user has NOT joined (empty list = all good)."""
    if not S.get("fsub_enabled"):
        return []
    channels = S.get("fsub_channels")
    if not channels:
        return []
    results = await asyncio.gather(*(_is_member(bot, user_id, c) for c in channels))
    return [c for c, ok in zip(channels, results) if not ok]
