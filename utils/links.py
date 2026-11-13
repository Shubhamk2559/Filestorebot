"""
utils/links.py - permanent share links.

With WORKER_URL set, every link is  <WORKER_URL>/<token>  (a Cloudflare Worker that instantly redirects to the
CURRENT bot). The bot registers its username with the worker on every start, so after a ban you only change
BOT_TOKEN: all old and new links keep working.
Without WORKER_URL but with BASE_URL (https), links are  <BASE_URL>/<token>  and the bot's own web server
redirects them to the current bot (no Cloudflare needed; same permanence after a bot change).
Otherwise the plain t.me link is used.
"""
import asyncio
import logging

import aiohttp

import config

log = logging.getLogger(__name__)


def share_link(bot_username: str, token: str) -> str:
    if config.WORKER_URL:
        return f"{config.WORKER_URL}/{token}"
    if config.BASE_URL.startswith("https://"):   # own domain: https://yourdomain/<token> -> redirects to the CURRENT bot
        return f"{config.BASE_URL}/{token}"
    return f"https://t.me/{bot_username}?start={token}"


async def register_bot(username: str):
    """Tell the worker which bot the links must open. Runs in the background; never blocks or crashes startup."""
    if not (config.WORKER_URL and config.WORKER_SECRET):
        return
    for attempt in range(5):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(f"{config.WORKER_URL}/_register", json={"username": username},
                                        headers={"X-Secret": config.WORKER_SECRET,
                                                 "User-Agent": "FileStoreBot/1.0"}) as r:
                    if r.status == 200:
                        log.info("Worker now redirects links to @%s", username)
                        return
                    log.warning("Worker register failed: HTTP %s %s", r.status, (await r.text())[:100])
        except Exception as e:
            log.warning("Worker register failed: %s", type(e).__name__)
        await asyncio.sleep(5 * (attempt + 1))
