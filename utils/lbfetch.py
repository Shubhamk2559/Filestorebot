"""
utils/lbfetch.py - the parallel, bounded-RAM downloader of Local Database Backup.

The zip writer still writes ONE entry at a time in plan order. This module downloads AHEAD of it:

    messages   get_messages for the files of a part, 200 ids per call, loaded just before they are needed;
               a single message is refreshed only when Telegram says FILE_REFERENCE_EXPIRED
    stripes    every file is cut into stripes of LB_STRIPE MiB; one stripe = one stream_media(limit=N, offset=k*N)
               call (N sequential 1 MiB requests inside, no per-chunk call / semaphore / decode overhead)
    window     up to LB_FILES files are open at once and up to `transfers` stripes are in flight, spread over all
               clients of the pool (several MTProto connections); stripes are handed out STRICTLY in plan order
    buffer     downloaded-but-not-yet-written bytes are capped (LB_BUFFER_MB). A stripe's bytes are counted from the
               moment it is requested until the writer has written them, so RAM stays small even for a 1.5 GB file
               (only the next few stripes are fetched). Because dispatch is in order the writer's next stripe is
               always the first one to get budget, so the cap can never deadlock the writer.
    thumbs     the thumbnail of a file is fetched in parallel with its media

Writer side (see BackupRunner._add_file):

    async with Prefetcher(clients, chat_id, mids) as pf:
        fx = await pf.fetch(i)                   # waits until file i is open; fx.info / fx.err
        async for block in fx.blocks(): ...      # bytes in order, 4-8 MiB per block
        thumb = await fx.thumb()
        fx.close()                               # always; frees the budget and cancels what is still running
    retry of a failed file: fx = await pf.fetch(i, retry=True)   (fresh message, small sequential window)
"""
import asyncio
import logging
from contextlib import aclosing

from utils import lbclient as LB
from utils.lbclient import CHUNK, describe, download_thumb, flood_seconds, get_messages

log = logging.getLogger(__name__)

MiB = 1 << 20
REF_ERRORS = ("FileReferenceExpired", "FileReferenceInvalid", "FileReferenceEmpty")
STRIPE_TRIES = 4
REF_REFRESHES = 3
DIRECT_AHEAD = 4                       # stripes a retried file keeps in flight


def is_ref_error(e) -> bool:
    return type(e).__name__ in REF_ERRORS or "FILE_REFERENCE" in str(e).upper()


def _quiet(fut):
    """A stripe nobody awaits (the writer gave up on the file) must not log 'exception was never retrieved'."""
    if not fut.cancelled():
        fut.exception()


class Fetch:
    """One file of the part: its message, description and the stripes being downloaded."""

    def __init__(self, pf, i, mid, msg, info, err="", direct=False, held=False):
        self.pf, self.i, self.mid, self.msg, self.info, self.err = pf, i, mid, msg, info, err
        self.direct, self.held = direct, held
        self.closed = False
        self.gen = 0                                   # bumped on every message refresh
        self.lock = asyncio.Lock()
        self.size = int(info["size"]) if info else 0
        self.stream = bool(info) and self.size <= 0    # size unknown: one sequential stream, no read-ahead
        self.sb = pf.stripe * CHUNK
        self.n = 0 if (not info or self.stream) else -(-self.size // self.sb)
        loop = asyncio.get_running_loop()
        self.futs = [loop.create_future() for _ in range(self.n)]
        for f in self.futs:
            f.add_done_callback(_quiet)
        self.tasks = [None] * self.n
        self.res = [0] * self.n                        # bytes reserved in the buffer budget, per stripe
        self.next = 0                                  # next stripe to dispatch
        self.thumb_task = None

    def stripe_len(self, k: int) -> int:
        return min(self.sb, self.size - k * self.sb)

    # ---------------------------------------------------------- consumer side
    async def pump(self, upto: int):
        """Dispatch stripes up to index `upto` (retry mode only: the main dispatcher skips these files)."""
        while self.next < min(self.n, upto) and not self.closed:
            k = self.next
            self.next += 1
            await self.pf._dispatch(self, k, force=True)

    async def blocks(self):
        """The file's bytes in order, in blocks of about pf.block bytes (several ready stripes are joined)."""
        pf = self.pf
        if self.stream:
            async for b in self._stream_blocks():
                yield b
            return
        k = 0
        while k < self.n:
            if self.direct:
                await self.pump(k + DIRECT_AHEAD)
            data = await self.futs[k]
            self.futs[k] = None
            idx, parts, total = [k], [data], len(data)
            k += 1
            while total < pf.block and k < self.n:
                if self.direct:
                    await self.pump(k + DIRECT_AHEAD)
                f = self.futs[k]
                if not (f.done() and not f.cancelled() and f.exception() is None):
                    break
                d = f.result()
                self.futs[k] = None
                idx.append(k)
                parts.append(d)
                total += len(d)
                k += 1
            block = parts[0] if len(parts) == 1 else b"".join(parts)
            del parts, data
            yield block                                # the caller writes it; only then the budget is returned
            del block
            for j in idx:
                self._free(j)

    async def _stream_blocks(self):
        client = self.pf._pick()
        pieces, total = [], 0
        async with aclosing(client.stream_media(self.msg)) as it:
            async for c in it:
                if not c:
                    continue
                pieces.append(c)
                total += len(c)
                if total >= self.pf.block:
                    yield b"".join(pieces)
                    pieces, total = [], 0
        if pieces:
            yield b"".join(pieces)

    async def thumb(self):
        if self.thumb_task is None:
            return None
        try:
            return await self.thumb_task
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    # ---------------------------------------------------------- cleanup
    def _free(self, k: int):
        t = self.tasks[k]
        if t is not None and not t.done():
            t.cancel()
        f = self.futs[k]
        if f is not None and not f.done():
            f.cancel()
        self.tasks[k] = self.futs[k] = None
        if self.res[k]:
            self.pf._release(self.res[k])
            self.res[k] = 0

    def close(self):
        if self.closed:
            return
        self.closed = True
        for k in range(self.n):
            self._free(k)
        if self.thumb_task is not None and not self.thumb_task.done():
            self.thumb_task.cancel()
        if self.held:
            self.held = False
            self.pf._files.release()
        self.pf._open.discard(self)


class Prefetcher:
    def __init__(self, clients, chat_id, mids, *, files=None, transfers=None, buffer=None, stripe=None, block=None,
                 batch: int = 200):
        self.clients = list(clients)
        self.chat_id = chat_id
        self.mids = list(mids)
        self.files = max(1, files if files is not None else LB.FILES)
        self.transfers = max(1, transfers if transfers is not None else LB.TRANSFERS)
        self.stripe = max(1, stripe if stripe is not None else LB.STRIPE)             # chunks (MiB) per call
        self.limit = int(buffer if buffer is not None else LB.BUFFER_MB * MiB)         # bytes
        self.block = int(block if block is not None else LB.WRITE_MB * MiB)            # bytes
        self.batch = batch
        self.reserved = 0
        self.stats = {"files": 0, "bytes": 0, "peak_buffer": 0, "peak_inflight": 0, "peak_files": 0,
                      "refreshes": 0, "stripe_retries": 0, "batches": 0, "retried_files": 0}
        self._busy = {id(c): 0 for c in self.clients}
        self._inflight = 0
        self._open = set()
        self._msgs, self._loaded = {}, 0
        self._ready = []
        self._task = None
        self._files = self._slots = self._room = None

    # ---------------------------------------------------------- lifecycle
    async def __aenter__(self):
        self._files = asyncio.Semaphore(self.files)
        self._slots = asyncio.Semaphore(self.transfers)
        self._room = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._ready = [loop.create_future() for _ in self.mids]
        for f in self._ready:
            f.add_done_callback(_quiet)
        self._task = asyncio.ensure_future(self._run())
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        open_fx = list(self._open)
        tasks = [t for fx in open_fx for t in fx.tasks if t is not None]
        tasks += [fx.thumb_task for fx in open_fx if fx.thumb_task is not None]
        for fx in open_fx:
            fx.close()
        await asyncio.gather(self._task, *tasks, return_exceptions=True)
        for f in self._ready:
            if f is not None and not f.done():
                f.cancel()
        return False

    # ---------------------------------------------------------- the writer's door
    async def fetch(self, i: int, retry: bool = False) -> Fetch:
        """File `i` (in plan order). retry=True: the message is read again and the file is fetched with a small
        sequential window that ignores the shared budget (a failed file must never wait behind its successors)."""
        if not retry:
            fut = self._ready[i]
            if fut is not None:
                try:
                    return await fut
                finally:
                    self._ready[i] = None            # (the dispatcher has already used it)
        self.stats["retried_files"] += 1
        return await self._make(i, held=False, direct=True, fresh=True)

    # ---------------------------------------------------------- budget / slots
    def _pick(self):
        return min(self.clients, key=lambda c: self._busy[id(c)])

    async def _reserve(self, n: int, force: bool):
        if not force:
            while self.reserved > 0 and self.reserved + n > self.limit:   # one stripe always fits an empty buffer
                self._room.clear()
                await self._room.wait()
        self.reserved += n
        if self.reserved > self.stats["peak_buffer"]:
            self.stats["peak_buffer"] = self.reserved

    def _release(self, n: int):
        self.reserved -= n
        self._room.set()

    async def _dispatch(self, fx: Fetch, k: int, force: bool):
        n = fx.stripe_len(k)
        await self._reserve(n, force)
        try:
            await self._slots.acquire()
        except BaseException:
            self._release(n)
            raise
        if fx.closed:                                  # the writer gave up on this file meanwhile
            self._slots.release()
            self._release(n)
            return
        fx.res[k] = n
        client = self._pick()
        self._busy[id(client)] += 1
        self._inflight += 1
        if self._inflight > self.stats["peak_inflight"]:
            self.stats["peak_inflight"] = self._inflight
        fx.tasks[k] = asyncio.ensure_future(self._stripe(fx, k, client))

    # ---------------------------------------------------------- messages
    async def _load_batch(self, i: int):
        ids = self.mids[i:i + self.batch]
        self.stats["batches"] += 1
        try:
            msgs = await get_messages(self.clients[0], self.chat_id, ids)
        except asyncio.CancelledError:
            raise
        except Exception as e:                         # every file of the batch retries on its own later
            log.warning("get_messages failed for %d ids: %s: %s", len(ids), type(e).__name__, e)
            msgs = [e] * len(ids)
        for j, m in enumerate(msgs):
            self._msgs[i + j] = m
        self._loaded = i + len(ids)

    async def _make(self, i: int, held: bool, direct: bool = False, fresh: bool = False) -> Fetch:
        mid = self.mids[i]
        if fresh:
            msg = (await get_messages(self._pick(), self.chat_id, [mid]))[0]
        else:
            if i >= self._loaded:
                await self._load_batch(i)
            msg = self._msgs.pop(i)
            if isinstance(msg, BaseException):
                raise msg
        info, why = describe(msg)
        fx = Fetch(self, i, mid, msg, info, "" if info else why, direct=direct, held=held and bool(info))
        if info:
            self._open.add(fx)
            self.stats["files"] += 1
            self.stats["peak_files"] = max(self.stats["peak_files"], len(self._open))
            if info.get("thumb_fid"):
                fx.thumb_task = asyncio.ensure_future(download_thumb(self._pick(), info["thumb_fid"]))
        return fx

    async def _refresh(self, fx: Fetch, client, gen: int):
        """FILE_REFERENCE_EXPIRED: read the message again - once, however many stripes of the file failed."""
        async with fx.lock:
            if fx.gen != gen:
                return
            msg = (await get_messages(client, self.chat_id, [fx.mid]))[0]
            info, why = describe(msg)
            if not info:
                raise IOError(f"message is gone ({why})")
            fx.msg, fx.gen = msg, fx.gen + 1
            self.stats["refreshes"] += 1

    def _settle(self, i: int, fx=None, exc=None):
        fut = self._ready[i]
        if fut is None or fut.done():
            return
        fut.set_exception(exc) if exc is not None else fut.set_result(fx)

    # ---------------------------------------------------------- the dispatcher (strict plan order)
    async def _run(self):
        try:
            for i in range(len(self.mids)):
                await self._files.acquire()
                try:
                    fx = await self._make(i, held=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._files.release()
                    self._settle(i, exc=e)
                    continue
                if not fx.held:                        # nothing to download (deleted message / not a file)
                    self._files.release()
                self._settle(i, fx=fx)
                while fx.next < fx.n and not fx.closed:
                    k = fx.next
                    fx.next += 1
                    await self._dispatch(fx, k, force=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Backup prefetcher died")
            for f in self._ready:
                if f is not None and not f.done():
                    f.set_exception(e)

    # ---------------------------------------------------------- one stripe
    async def _stripe(self, fx: Fetch, k: int, client):
        fut = fx.futs[k]
        try:
            data = await self._get_stripe(fx, k, client)
            self.stats["bytes"] += len(data)
            if fut is not None and not fut.done():
                fut.set_result(data)
        except asyncio.CancelledError:
            if fut is not None and not fut.done():
                fut.cancel()
            raise
        except Exception as e:
            if fut is not None and not fut.done():
                fut.set_exception(e)
        finally:
            self._busy[id(client)] -= 1
            self._inflight -= 1
            self._slots.release()

    async def _get_stripe(self, fx: Fetch, k: int, client) -> bytes:
        want = fx.stripe_len(k)
        limit = -(-want // CHUNK)
        tries = refreshes = 0
        while True:
            gen = fx.gen
            try:
                pieces, got = [], 0
                async with aclosing(client.stream_media(fx.msg, limit=limit, offset=k * self.stripe)) as it:
                    async for c in it:
                        if c:
                            pieces.append(c)
                            got += len(c)
                if got != want:
                    raise IOError(f"Telegram returned {got} of {want} bytes (stripe {k} of message {fx.mid})")
                return pieces[0] if len(pieces) == 1 else b"".join(pieces)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                wait = flood_seconds(e)
                if wait is not None:                   # a flood wait is not a failure
                    log.warning("Flood wait: sleeping %.0fs", wait + 1)
                    await asyncio.sleep(wait + 1)
                    continue
                if is_ref_error(e) and refreshes < REF_REFRESHES:
                    refreshes += 1
                    await self._refresh(fx, client, gen)
                    continue
                tries += 1
                if tries >= STRIPE_TRIES:
                    raise
                self.stats["stripe_retries"] += 1
                await asyncio.sleep(0.5 * tries)
