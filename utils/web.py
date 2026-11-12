"""
utils/web.py - aiohttp server.

  GET /, /health          health checks (Koyeb / Render / Heroku / VPS)
  GET|HEAD /dl/<token>/<msg_id>[?d=1]
                          streams one stored file. Range supported (video seeking, resume).
                          ?d=1 forces "Save as" instead of playing inline.

A stream URL is valid only while its share link exists: revoking the link in the panel
also kills every Stream / Download URL that was handed out for it.
"""
import logging
import time
from urllib.parse import quote

from aiohttp import web

from database import db
from database.db import settings as S
from utils.streamer import RangeError, parse_range, streamer

log = logging.getLogger(__name__)
LINK_TTL = 30  # seconds: a video player fires many Range requests, don't hit Mongo for each
_link_cache: dict = {}


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


def _content_disposition(name: str, download: bool) -> str:
    ascii_name = name.encode("ascii", "ignore").decode().replace('"', "") or "file"
    kind = "attachment" if download else "inline"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


async def stream_file(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    try:
        msg_id = int(request.match_info["msg_id"])
    except ValueError:
        raise web.HTTPBadRequest(text="bad message id")

    link = await _valid_link(token)
    if not link or msg_id not in link["ids"]:
        raise web.HTTPNotFound(text="This link is invalid or has been revoked.")
    channel = S.get("db_channel")
    if not channel or not streamer.ready:
        raise web.HTTPServiceUnavailable(text="Streaming is not available right now.")

    try:
        info = await streamer.get_info(channel["id"], msg_id)
    except Exception:
        log.exception("get_info failed")
        raise web.HTTPBadGateway(text="Could not read the file from Telegram.")
    if not info:
        raise web.HTTPNotFound(text="File no longer available.")

    try:
        start, end, partial = parse_range(request.headers.get("Range"), info.size)
    except RangeError:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{info.size}"})

    headers = {
        "Content-Type": info.mime,
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(info.name, request.query.get("d") == "1"),
        "Cache-Control": "private, max-age=0",
        "X-Content-Type-Options": "nosniff",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{info.size}"
    resp = web.StreamResponse(status=206 if partial else 200, headers=headers)
    await resp.prepare(request)
    if request.method == "HEAD":
        return resp

    try:
        async for chunk in streamer.iter_range(info, start, end):
            await resp.write(chunk)
        await resp.write_eof()
    except (ConnectionResetError, ConnectionError):
        pass  # viewer closed the tab / seeked elsewhere: totally normal
    except Exception:
        log.exception("Streaming %s/%s failed", token, msg_id)
    return resp


async def start_web_server(port: int):
    async def health(request):
        try:
            await db.db.command("ping")
            return web.json_response({"status": "ok", "streaming": streamer.ready})
        except Exception as e:
            return web.json_response({"status": "db_error", "error": type(e).__name__}, status=503)

    async def root(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_route("GET", "/dl/{token}/{msg_id}", stream_file)
    app.router.add_route("HEAD", "/dl/{token}/{msg_id}", stream_file)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
