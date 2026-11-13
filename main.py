"""
main.py - entry point.
Starts: MongoDB + settings cache, health endpoint (/health), auto-delete worker, Telegram polling.
"""
import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, Defaults, MessageHandler, filters)

import config
from database import db
from handlers import admin, start, upload
from handlers import autoposter as _autoposter  # noqa: F401  (registers the Autoposter panel screens)
from utils.links import register_bot
from utils.streamer import streamer
from utils.web import start_web_server
from utils.scheduler import autodelete_loop
from utils.autoposter import autopost_loop

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=config.LOG_LEVEL)
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every Bot API call (leaks the token)
log = logging.getLogger("bot")


async def post_init(app):
    await db.init_db()                                   # connect, indexes, load cached settings
    app.bot_data["health"] = await start_web_server(config.PORT, app.bot)   # /health + /dl/<token>/<id> streaming
    await streamer.start()                               # MTProto engine (needs API_ID, API_HASH, BASE_URL)
    app.bot_data["gc"] = asyncio.create_task(autodelete_loop(app.bot))
    app.bot_data["ap"] = asyncio.create_task(autopost_loop(app.bot))     # Autoposter scheduler
    app.bot_data["reg"] = asyncio.create_task(register_bot(app.bot.username))   # links -> this bot (Cloudflare Worker)
    log.info("Bot @%s started (web on :%s, streaming %s)", app.bot.username, config.PORT,
             "ON" if streamer.ready else f"OFF ({streamer.error})")


async def post_shutdown(app):
    await streamer.stop()
    for key in ("gc", "ap"):
        task = app.bot_data.get(key)
        if task:
            task.cancel()
    runner = app.bot_data.get("health")
    if runner:
        await runner.cleanup()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        # Another instance is polling with the same token (old deploy still shutting down,
        # a local run, or another server). Polling keeps retrying and recovers once it stops.
        log.warning("Conflict: another instance is using this BOT_TOKEN - stop it (only one may run)")
        return
    if isinstance(context.error, NetworkError):
        log.warning("Network error: %s", context.error)  # transient, polling retries by itself
        return
    log.error("Unhandled error", exc_info=context.error)


def main():
    app = (ApplicationBuilder()
           .token(config.BOT_TOKEN)
           .defaults(Defaults(parse_mode=ParseMode.HTML))
           .concurrent_updates(True)   # many users can receive files at the same time
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start.start_cmd, filters=private))        # the ONLY command
    app.add_handler(CallbackQueryHandler(admin.callback_router, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(start.check_callback, pattern=r"^chk:"))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, admin.message_router))
    # Files forwarded into the Database Channel are re-linked from their caption code (disaster recovery)
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & ~filters.COMMAND, upload.channel_restore))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
