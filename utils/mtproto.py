"""
utils/mtproto.py
Small optional MTProto (Pyrogram) session using the SAME bot token.

It is NOT used for delivering files (that is plain forwarding via the Bot API). It is only needed for two
admin-side features that must read channel history, which the Bot API cannot do:
  * Autoposter -> "Range" (read a range of messages from a source channel)
  * Duo Reel preview image (read the image back from the Database Channel)

Needs API_ID + API_HASH. Without them the bot still works fully (forwarding), only those two features are off.
"""
import logging
import os

import config

log = logging.getLogger(__name__)


class MTProto:
    def __init__(self):
        self.client = None
        self.error: str = ""        # why the session is unavailable (shown in the admin panel)

    @property
    def ready(self) -> bool:
        return self.client is not None

    async def start(self):
        if not config.API_ID or not config.API_HASH:
            self.error = "missing env: API_ID / API_HASH"
            log.info("MTProto session off (%s). Forwarding works normally.", self.error)
            return
        try:
            from pyrogram import Client  # imported here so setups without it never need it
        except ImportError:
            self.error = "pyrofork not installed"
            log.warning("MTProto session off: pyrofork is not installed.")
            return
        try:
            # Created inside the running loop on purpose (Pyrogram binds to the current loop).
            client = Client("mtproto", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN,
                            in_memory=True, no_updates=True, sleep_threshold=60,
                            # parallel chunk transfers = the big speed-up for download / upload (default is 1)
                            max_concurrent_transmissions=int(os.getenv("MTPROTO_PARALLEL", "8") or 8))
            try:
                import tgcrypto  # noqa: F401
            except ImportError:
                log.warning("tgcrypto is NOT installed -> MTProto is 5-10x slower. pip install tgcrypto")
            await client.start()
            self.client = client
            self.error = ""
            log.info("MTProto session started.")
        except Exception as e:  # bad API_ID/HASH, network...: never take the whole bot down
            self.error = f"MTProto start failed: {type(e).__name__}"
            log.exception("Could not start the MTProto session")

    async def stop(self):
        if self.client:
            try:
                await self.client.stop()
            except Exception:
                pass
            self.client = None


mtproto = MTProto()
