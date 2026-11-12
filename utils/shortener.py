"""utils/shortener.py - optional link shortener (GPLinks / LinkShortify style APIs)."""
import json
import logging
from urllib.parse import quote

import aiohttp

from database.db import settings as S

log = logging.getLogger(__name__)


async def shorten(url: str):
    """Return a short URL, or None if the shortener is off / failed."""
    if not (S.get("shortener_enabled") and S.get("shortener_url") and S.get("shortener_key")):
        return None
    api = S.get("shortener_url").strip().rstrip("?&")
    sep = "&" if "?" in api else "?"
    request = f"{api}{sep}api={S.get('shortener_key')}&url={quote(url, safe='')}&format=text"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(request) as resp:
                body = (await resp.text()).strip()
        if body.startswith("http"):
            return body.splitlines()[0].strip()
        data = json.loads(body)
        for key in ("shortenedUrl", "shortlink", "short_url", "shorturl", "result_url"):
            if isinstance(data, dict) and data.get(key):
                return data[key]
    except Exception as e:  # network/JSON errors must never break link creation
        log.warning("Shortener failed: %s", type(e).__name__)
    return None
