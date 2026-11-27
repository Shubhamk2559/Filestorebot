"""utils/health.py - tiny aiohttp server: GET / and /health (Koyeb / Render / Heroku / VPS health checks)."""
from aiohttp import web

from database.db import db


async def start_health_server(port: int):
    async def health(request):
        try:
            await db.command("ping")
            return web.json_response({"status": "ok"})
        except Exception as e:
            return web.json_response({"status": "db_error", "error": type(e).__name__}, status=503)

    async def root(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
