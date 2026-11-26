"""
utils/web.py - aiohttp server.

  GET /ad, POST /api/ad/*, GET /monetag/postback   Monetag rewarded-ad Mini App (utils/ads.py)
  GET /<token>            permanent share link -> redirects to the current bot on Telegram
  GET /, /health          health checks (Koyeb / Render / Heroku / VPS)

Files are never served from here: users receive them by forwarding inside Telegram.
"""
import logging
import time

from aiohttp import web

from database import db
from utils import ads
from utils import home

log = logging.getLogger(__name__)
LINK_TTL = 30  # seconds: cache link lookups so the redirect doesn't hit Mongo every time
_link_cache = db.LINK_CACHE   # shared with database.db: purge_link() clears it, so a deleted link dies instantly


async def _valid_link(token: str):
    hit = _link_cache.get(token)
    now = time.time()
    if hit and now - hit[0] < LINK_TTL:
        return hit[1]
    link = await db.get_link(token)
    _link_cache[token] = (now, link)
    if len(_link_cache) > 5000:
        _link_cache.clear()
    return link


async def open_link(request: web.Request) -> web.StreamResponse:
    """https://yourdomain/<token>  ->  the CURRENT bot (permanent link, survives a bot change)."""
    token = request.match_info["token"]
    link = await _valid_link(token)
    if not link:
        raise web.HTTPNotFound(text="This link is invalid or has been revoked.")
    bot = request.app.get("bot")
    if not bot or not bot.username:
        raise web.HTTPServiceUnavailable(text="Please try again in a moment.")
    raise web.HTTPFound(f"https://t.me/{bot.username}?start={token}",
                        headers={"Cache-Control": "no-store"})


async def open_support(request: web.Request) -> web.StreamResponse:
    """https://yourdomain/support  ->  the CURRENT Support Bot (permanent link, survives a Support Bot change)."""
    from utils import support as SUP
    target = SUP.url()
    if not target:
        raise web.HTTPNotFound(text="Support is not available right now.")
    raise web.HTTPFound(target, headers={"Cache-Control": "no-store"})


async def start_web_server(port: int, bot=None):
    async def health(request):
        try:
            await db.db.command("ping")
            return web.json_response({"status": "ok"})
        except Exception as e:
            return web.json_response({"status": "db_error", "error": type(e).__name__}, status=503)

    async def root(request):
        return web.Response(text="OK")

    app = web.Application()
    app["bot"] = bot
    ads.setup_routes(app)   # /ad (Mini App), /api/ad/*, /monetag/postback
    app.router.add_get("/", home.page)                       # landing page (utils/home.py)
    app.router.add_get("/assets/home-image", home.image)
    app.router.add_get("/health", health)
    app.router.add_get("/support", open_support)             # permanent link to the Support Bot
    app.router.add_get("/{token:[A-Za-z0-9_-]{6,64}}", open_link)   # keep LAST: fixed routes above win
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
