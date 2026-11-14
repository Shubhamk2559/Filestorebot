"""
main.py - entry point.
Starts: MongoDB + settings cache, health endpoint (/health), auto-delete worker, Telegram polling.
"""
import asyncio
import logging

try:                                   # uvloop = much faster event loop (Linux/VPS). Optional.
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:                      # not installed / not supported -> normal asyncio
    pass

from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, Defaults, MessageHandler, filters)

import config
from database import db
from handlers import admin, start, upload
from handlers import autoposter as _autoposter  # noqa: F401  (registers the Autoposter panel screens)
from handlers import backup as _backup          # registers the Backup / Restore screens
from utils.backup import backup_loop
from utils import padding
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
    app.bot_data["bk"] = asyncio.create_task(backup_loop(app.bot))      # daily auto-backup to the owner
    app.bot_data["reg"] = asyncio.create_task(register_bot(app.bot.username))   # links -> this bot (Cloudflare Worker)
    log.info("Bot @%s started (web on :%s, streaming %s)", app.bot.username, config.PORT,
             "ON" if streamer.ready else f"OFF ({streamer.error})")
    if not config.BASE_URL.startswith("https://"):
        log.warning("BASE_URL is not a public https URL -> Monetag ads (Mini App) and Stream/Download links will not work.")


async def post_shutdown(app):
    await streamer.stop()
    for key in ("gc", "ap", "bk"):
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
    asyncio.set_event_loop(asyncio.new_event_loop())   # Python 3.12 + uvloop: run_polling needs a current loop
    padding.install()           # every message with inline buttons gets as wide as its keyboard
    app = (ApplicationBuilder()
           .token(config.BOT_TOKEN)
           .defaults(Defaults(parse_mode=ParseMode.HTML))
           .concurrent_updates(256)    # many users can receive files at the same time
           # Bot API connection pool: must be big, otherwise concurrent users queue up behind each other
           .connection_pool_size(256)
           .pool_timeout(15)
           .connect_timeout(10)
           .read_timeout(30)
           .write_timeout(30)
           .get_updates_connection_pool_size(4)
           .get_updates_read_timeout(40)
           .get_updates_connect_timeout(10)
           .get_updates_pool_timeout(15)
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start.start_cmd, filters=private))        # the ONLY command
    app.add_handler(CallbackQueryHandler(admin.callback_router, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(start.check_callback, pattern=r"^chk:"))
    # a backup file (fsb-backup-*.json.gz) sent by the owner -> restore flow (must come BEFORE the generic router)
    app.add_handler(MessageHandler(private & filters.Document.ALL & _backup.BackupDocFilter(), _backup.ingest))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, admin.message_router))
    # Files forwarded into the Database Channel are re-linked from their caption code (disaster recovery)
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & ~filters.COMMAND, upload.channel_restore))
    app.add_error_handler(on_error)

    # only the update types this bot really uses (less traffic, less CPU)
    app.run_polling(allowed_updates=["message", "callback_query", "channel_post"],
                    drop_pending_updates=True, timeout=30)


if __name__ == "__main__":
    main()
