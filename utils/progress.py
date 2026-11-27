"""
utils/progress.py - live task status helpers (stdlib only).

  Tracker   bytes done / total, smoothed speed (MB/s), ETA, elapsed
  bar()     ▰▰▰▰▱▱▱▱▱▱ style progress bar
  JOBS      running ZIP jobs, so the 🛑 Stop button can cancel one
"""
import time
from collections import deque

JOBS: dict = {}          # job id -> {"task": asyncio.Task, "by": user id, "cid": channel id, "cancelled": bool}


def size(n: float) -> str:
    n = float(n or 0)
    if n >= 2 ** 30:
        return f"{n / 2 ** 30:.2f} GB"
    if n >= 2 ** 20:
        return f"{n / 2 ** 20:.1f} MB"
    return f"{n / 1024:.0f} KB"


def clock(sec: float) -> str:
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    full = int(round(width * pct / 100))
    return "▰" * full + "▱" * (width - full)


class Tracker:
    """Feed it (current, total) bytes; ask it for speed / ETA. Speed is averaged over the last few seconds."""

    def __init__(self, window: float = 6.0):
        self.t0 = time.time()
        self.cur = 0
        self.tot = 0
        self.window = window
        self._s: deque = deque()

    def reset(self):
        self.__init__(self.window)

    def update(self, cur: int, tot: int = 0):
        now = time.time()
        self.cur, self.tot = cur, tot or self.tot
        self._s.append((now, cur))
        while len(self._s) > 2 and now - self._s[0][0] > self.window:
            self._s.popleft()

    @property
    def pct(self) -> float:
        return self.cur * 100 / self.tot if self.tot else 0.0

    @property
    def speed(self) -> float:
        if len(self._s) < 2:
            return 0.0
        (t1, c1), (t2, c2) = self._s[0], self._s[-1]
        return max(0.0, (c2 - c1) / (t2 - t1)) if t2 > t1 else 0.0

    @property
    def eta(self) -> float:
        sp = self.speed
        return (self.tot - self.cur) / sp if sp > 0 and self.tot else 0.0

    @property
    def elapsed(self) -> float:
        return time.time() - self.t0

    def line(self) -> str:
        """⚡ 18.4 MB/s · ⏱ ETA 0:11 · 🕐 0:02"""
        sp = self.speed
        return (f"⚡ {size(sp)}/s · ⏱ ETA {clock(self.eta) if sp else '--'} · 🕐 {clock(self.elapsed)}")

    def card(self) -> str:
        """bar + sizes + speed line"""
        return (f"{bar(self.pct)} <b>{self.pct:.0f}%</b>\n"
                f"📊 {size(self.cur)} / {size(self.tot)}\n{self.line()}")
