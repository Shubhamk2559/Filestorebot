"""
utils/preview.py
Image + Video posts: the free preview image is hosted on imgbb, embedded in a Telegraph article, and the
Telegraph link becomes the "Preview" button (opens directly, the bot is not involved).

  make_preview_url(db_channel_id, msg_id, title)  ->  "https://telegra.ph/..." or None

Needs the imgbb API key (set from the admin panel: Autoposter -> channel -> Caption & Button -> imgbb API Key) and the MTProto engine (API_ID / API_HASH) to read the image back
from the Database Channel. Any failure returns None, and the caller falls back to the normal bot link.
"""
import logging

import aiohttp

from database.db import settings as S
from utils.streamer import streamer

log = logging.getLogger(__name__)

IMGBB_URL = "https://api.imgbb.com/1/upload"
TELEGRAPH = "https://api.telegra.ph"
_TIMEOUT = aiohttp.ClientTimeout(total=60)


def enabled() -> bool:
    from utils import batch               # imgbb keys (one or several, rotation) live in utils/batch.py
    return batch.enabled()


async def _download(chat_id: int, msg_id: int) -> bytes:
    """Read the image bytes of a Database Channel message through the MTProto client."""
    if not streamer.ready:
        raise RuntimeError(f"MTProto engine not running ({streamer.error or 'API_ID / API_HASH missing'})")
    msg = await streamer.client.get_messages(chat_id, msg_id)
    buf = await streamer.client.download_media(msg, in_memory=True)
    return bytes(buf.getbuffer())


async def _imgbb_upload(session: aiohttp.ClientSession, data: bytes) -> str:
    from utils import batch
    return await batch.upload(session, data)      # direct image link (i.ibb.co/...), several keys in rotation


async def _telegraph_token(session: aiohttp.ClientSession) -> str:
    token = S.get("telegraph_token")
    if token:
        return token
    async with session.post(f"{TELEGRAPH}/createAccount",
                            data={"short_name": "FileBot", "author_name": "FileBot"}) as r:
        js = await r.json(content_type=None)
    if not js.get("ok"):
        raise RuntimeError(f"telegraph: {js.get('error')}")
    token = js["result"]["access_token"]
    await S.set("telegraph_token", token)
    return token


async def _telegraph_page(session: aiohttp.ClientSession, title: str, image_url: str) -> str:
    import json
    token = await _telegraph_token(session)
    content = [{"tag": "img", "attrs": {"src": image_url}}]
    async with session.post(f"{TELEGRAPH}/createPage", data={
            "access_token": token, "title": (title or "Preview")[:256],
            "content": json.dumps(content), "return_content": "false"}) as r:
        js = await r.json(content_type=None)
    if not js.get("ok"):
        raise RuntimeError(f"telegraph: {js.get('error')}")
    return js["result"]["url"]


async def make_preview_url(chat_id: int, msg_id: int, title: str):
    """imgbb -> Telegraph. Returns (article URL, imgbb image URL), or (None, None) (not configured / any error)."""
    if not enabled():
        return None, None
    try:
        data = await _download(chat_id, msg_id)
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            image_url = await _imgbb_upload(session, data)
            return await _telegraph_page(session, title, image_url), image_url
    except Exception as e:
        log.warning("Preview page failed (%s): %s", type(e).__name__, e)
        return None, None


async def alive(*urls) -> bool:
    """False only when a link is DEFINITELY gone (HTTP 404/410 etc.). A network hiccup counts as alive,
    so a temporary problem never throws away a good Telegraph link."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for u in urls:
                if not u:
                    continue
                async with session.get(u, allow_redirects=True) as r:
                    if r.status in (404, 410) or 400 <= r.status < 500 and r.status != 429:
                        return False
    except Exception:
        return True
    return True
