"""
utils/preview.py
Image + Video posts: the free preview image is hosted on imgbb, embedded in a Telegraph article, and the
Telegraph link becomes the "Preview" button (opens directly, the bot is not involved).

  make_preview_url(db_channel_id, msg_id, title)  ->  "https://telegra.ph/..." or None

Needs IMGBB_API_KEY in the environment and the MTProto engine (API_ID / API_HASH) to read the image back
from the Database Channel. Any failure returns None, and the caller falls back to the normal bot link.
"""
import base64
import logging

import aiohttp

import config
from database.db import settings as S
from utils.streamer import streamer

log = logging.getLogger(__name__)

IMGBB_URL = "https://api.imgbb.com/1/upload"
TELEGRAPH = "https://api.telegra.ph"
_TIMEOUT = aiohttp.ClientTimeout(total=60)


def enabled() -> bool:
    return bool(config.IMGBB_API_KEY)


async def _download(chat_id: int, msg_id: int) -> bytes:
    """Read the image bytes of a Database Channel message through the MTProto client."""
    if not streamer.ready:
        raise RuntimeError(f"MTProto engine not running ({streamer.error or 'API_ID / API_HASH missing'})")
    msg = await streamer.client.get_messages(chat_id, msg_id)
    buf = await streamer.client.download_media(msg, in_memory=True)
    return bytes(buf.getbuffer())


async def _imgbb_upload(session: aiohttp.ClientSession, data: bytes) -> str:
    form = aiohttp.FormData()
    form.add_field("key", config.IMGBB_API_KEY)
    form.add_field("image", base64.b64encode(data).decode())
    async with session.post(IMGBB_URL, data=form) as r:
        js = await r.json(content_type=None)
    if not js.get("success"):
        raise RuntimeError(f"imgbb: {(js.get('error') or {}).get('message', js)}")
    return js["data"]["url"]                      # direct image link (i.ibb.co/...)


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
    """imgbb -> Telegraph. Returns the article URL, or None (not configured / any error)."""
    if not enabled():
        return None
    try:
        data = await _download(chat_id, msg_id)
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            image_url = await _imgbb_upload(session, data)
            return await _telegraph_page(session, title, image_url)
    except Exception as e:
        log.warning("Preview page failed (%s): %s", type(e).__name__, e)
        return None
